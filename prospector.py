"""prospector.py - The Prospector agent.

Finds high-potential companies for a Target Product:
  1. Queries TheirStack for companies using related/competing technologies.
  2. Enriches with Serper (websites, LinkedIn profiles, contact emails).
  3. Saves companies to Supabase with status = 'raw'.

IMPORTANT: TheirStack requires `Authorization: Bearer <key>` (X-API-KEY returns 401).
All TheirStack/Serper responses are disk-cached (see cache.py).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests
from dotenv import load_dotenv

import cache
import database as db
from llm import chat, load_prompt, parse_json

load_dotenv()

THEIRSTACK_URL = "https://api.theirstack.com/v1/companies/search"
SERPER_URL = "https://google.serper.dev/search"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)


# --- API clients ------------------------------------------------------------

@cache.cached("theirstack", ttl_days=14)
def search_theirstack(payload: dict) -> dict:
    """POST /v1/companies/search. Payload cached by hash."""
    resp = requests.post(
        THEIRSTACK_URL,
        headers={
            "Authorization": f"Bearer {os.getenv('THEIRSTACK_API_KEY', '')}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"TheirStack {resp.status_code}: {resp.text[:300]}")
    return resp.json()


@cache.cached("serper", ttl_days=30)
def serper_search(query: str) -> dict:
    """Google search via Serper. `query` is string -> hashed payload."""
    payload = {"q": query, "num": 10}
    resp = requests.post(
        SERPER_URL,
        headers={
            "X-API-KEY": os.getenv("SERPER_API_KEY", ""),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Serper {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def extract_emails(text: str) -> list[str]:
    """Deduplicated emails from an arbitrary text blob."""
    seen: set[str] = set()
    for m in EMAIL_RE.findall(text):
        lower = m.lower()
        if not lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            seen.add(lower)
    return sorted(seen)


# --- Query building ---------------------------------------------------------

def build_queries(target_product: str) -> dict[str, list[str]]:
    """Use Gemma (prompts/prospector.md) to plan TheirStack tech filters +
    description patterns + Serper queries. Falls back to keyword-based plans
    on any LLM failure."""
    try:
        reply = chat(
            load_prompt("prospector.md"),
            f"Target Product: {target_product}",
            max_tokens=400,
        )
        plan = parse_json(reply)
        return {
            "technology_slugs": list(plan.get("technology_slugs", [])),
            "description_patterns": list(plan.get("description_patterns", [])),
            "serper_queries": list(plan.get("serper_queries", [])),
        }
    except Exception as exc:  # LLM down -> deterministic fallback
        print(f"[prospector] LLM query planning failed ({exc}); using keyword fallback.")
        return {
            "technology_slugs": [],
            "description_patterns": [target_product],
            "serper_queries": [
                f'"{target_product}" company site:linkedin.com/company',
                f'"{target_product}" company',
            ],
        }


# --- Core flow --------------------------------------------------------------

def prospect(target_product: str, limit: int = 10) -> list[dict[str, Any]]:
    """Run the full prospecting flow. Returns inserted/known leads."""
    plan = build_queries(target_product)
    print(f"[prospector] Target: {target_product!r}")
    print(f"[prospector] Tech filters: {plan['technology_slugs'] or '(none)'}")
    print(f"[prospector] Description patterns: {plan['description_patterns'] or '(none)'}")
    print(f"[prospector] Serper queries: {plan['serper_queries'] or '(none)'}")

    companies: list[dict[str, Any]] = []

    # 1) TheirStack: tech-filtered searches (technology_slug_or) + description
    #    pattern searches (company_description_pattern_or). NOTE: never send
    #    page:0 (TheirStack 422s); omit page entirely. Each result costs 3 API
    #    credits, so stop once we have enough unique companies.
    theirstack_payloads: list[dict] = [
        {"tech_filters": {"technology_slug_or": [tech]}, "limit": limit}
        for tech in plan["technology_slugs"]
    ]
    theirstack_payloads += [
        {"company_description_pattern_or": [pat], "limit": limit}
        for pat in plan["description_patterns"]
    ]
    if not theirstack_payloads:
        theirstack_payloads.append({"company_description_pattern_or": [target_product], "limit": limit})

    for payload in theirstack_payloads:
        if len(companies) >= limit:
            break
        try:
            result = search_theirstack(payload)
            for co in result.get("data", []) or []:
                companies.append(co)
        except Exception as exc:
            print(f"[prospector] TheirStack query failed ({payload}): {exc}")

    # Dedupe by domain
    by_domain: dict[str, dict] = {}
    for co in companies:
        domain = (co.get("domain") or "").strip().lower()
        if domain:
            by_domain.setdefault(domain, co)
    companies = list(by_domain.values())
    print(f"[prospector] TheirStack returned {len(companies)} unique companies")

    # 2) Serper enrichment for each company
    inserted: list[dict[str, Any]] = []
    for co in companies[:limit]:
        domain = (co.get("domain") or "").strip().lower()
        name = co.get("name") or co.get("domain") or "unknown"
        website = f"https://{domain}" if domain else None
        if not website:
            continue

        emails: set[str] = set()
        linkedin = co.get("linkedin_url")
        for q in (f'"{domain}" email contact',):
            try:
                sr = serper_search(q)
                for item in sr.get("organic", []) or []:
                    emails.update(extract_emails(item.get("title", "") + " " + item.get("snippet", "")))
            except Exception as exc:
                print(f"[prospector] Serper failed for {domain}: {exc}")

        tech_stack = (co.get("technology_names") or co.get("technologies_found") or [])
        lead = db.insert_lead(
            company_name=name,
            website=website,
            tech_stack=list(tech_stack) if isinstance(tech_stack, list) else [],
            source="theirstack",
            target_product=target_product,
            contact_email=sorted(emails)[0] if emails else None,
        )
        inserted.append(lead)
        print(f"[prospector] saved {name} ({website}) status=raw emails={sorted(emails)}")

    return inserted


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prospect for a target product")
    parser.add_argument("--target", required=True, help="Target product, e.g. 'Cloud Migration Services'")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    db.init_schema()
    leads = prospect(args.target, args.limit)
    print(f"\nDone. {len(leads)} leads in pipeline (status=raw).")
