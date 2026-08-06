"""prospector.py - The Prospector agent.

Finds high-potential companies for a Target Product:
  1. Queries TheirStack for companies using related/competing technologies.
  2. Filters out poor cold-outreach targets (enterprise / public sector /
     defense) using public metadata - decided deterministically, no hardcoded
     company names.
  3. Enriches with Serper (websites, LinkedIn profiles, contact emails).
  4. Saves companies to Supabase with status = 'raw' + company_profile metadata
     the scorer uses to apply size/segment penalties.

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

# --- Company classification (poor cold-outreach targets) ---------------------
# Decided ONLY from publicly available metadata TheirStack returns (employee
# count/range, public listing, industry, description, tags, domain). No company
# names are hardcoded - the rules fit any enterprise/gov/defense profile.

# Employee count at or above this = "extremely large multinational enterprise",
# which is out of scope for an SMB/mid-market cold outreach product.
ENTERPRISE_EMPLOYEE_MIN = 10_000

# Directory-level public-sector/defense signals are decisive regardless of size.
_PUBLIC_SECTOR_PHRASES = (
    "public sector", "government agency", "defense", "defence", "military",
    "armed forces", "army", "navy", "air force", "homeland security",
    "national security", "department of defense", "space force", "ministry",
)
# Weaker: a vendor may simply *sell to* government customers. Penalize, don't exclude.
_GOVERNMENT_ADJACENT_PHRASES = (
    "government", "federal", "municipal", "state agency", "publicly-funded",
)

# Decisive "service provider" signals. These companies SELL the kind of services
# the target product provides (IT services, consulting, cloud migration, managed
# services, ...) - they are competitors, not buyers. A company whose public
# description/tags contain these phrases is excluded regardless of size, because
# cold outreach to a provider of the same service is wasted effort.
_SERVICE_PROVIDER_PHRASES = (
    "it services", "it solutions", "it consulting", "it consultancy",
    "managed services", "managed it services", "managed service provider", "msp",
    "cloud migration", "cloud migration services", "migration services",
    "cloud consulting", "cloud services provider", "cloud solutions provider",
    "cloud managed services", "cloud adoption", "cloud transformation",
    "digital transformation services", "digital transformation company",
    "technology consulting", "technology services", "tech consulting",
    "software development company", "software development services",
    "software solutions", "software services", "custom software development",
    "application development", "software house", "development agency",
    "systems integration", "system integration", "systems integrator",
    "solutions provider", "solution provider", "technology solutions",
    "outsourcing", "staff augmentation", "it outsourcing",
    "consulting services", "consultancy", "professional services",
    "bpo", "business process outsourcing",
    "data center services", "data centre services", "colocation",
    "internet service provider", "isp services",
    "hosting provider", "web hosting company",
)

_DOMAIN_EXCLUDE_ENDINGS = (".gov", ".mil")

# Industry-taxonomy signals (TheirStack/LinkedIn industry names): dev shops,
# consultancies, MSPs - they SELL what we sell, so they are never buyers.
# Checked against the `industry` field only (bare "software development" in a
# company description is too ambiguous to exclude on).
_PROVIDER_INDUSTRY_PHRASES = (
    "it services and it consulting",
    "business consulting and services",
    "outsourcing and offshoring consulting",
    "it system custom software development",
    "software development",
)


def _parse_employee_min(range_str: str) -> int | None:
    """First number from TheirStack ranges ('10,000+' -> 10000)."""
    nums = re.findall(r"\d[\d,]*", range_str or "")
    if not nums:
        return None
    return int(nums[0].replace(",", ""))


def classify_company(co: dict) -> dict:
    """Classify a TheirStack company row for cold-outreach suitability.

    Returns {
      "action": "exclude" | "keep",
      "segment": "public_sector" | "large_enterprise" | "government_adjacent"
                | "listing" | "smb_mid",
      "reasons": [human-readable flags to show the review team],
      "profile": {public metadata saved to leads.company_profile},
    }
    """
    name = str(co.get("name") or "").lower()
    domain = str(co.get("domain") or "").lower()
    tags = " ".join(str(t) for t in (co.get("company_tags") or []))
    keywords = " ".join(str(k) for k in (co.get("company_keywords") or []))
    blob = " ".join(
        filter(
            None,
            [
                name,
                domain,
                str(co.get("long_description") or ""),
                str(co.get("seo_description") or ""),
                str(co.get("industry") or ""),
                tags,
                keywords,
            ],
        )
    ).lower()

    traded_symbol = str(co.get("publicly_traded_symbol") or "").strip()
    emp_count = co.get("employee_count")
    emp_num = emp_count if isinstance(emp_count, (int, float)) else _parse_employee_min(co.get("employee_count_range"))
    emp_block = co.get("employee_count_range") or (f"{int(emp_count):,}" if isinstance(emp_count, (int, float)) else "")

    reasons: list[str] = []
    profile: dict = {
        "employee_count": emp_count if isinstance(emp_count, (int, float)) else None,
        "employee_range": co.get("employee_count_range"),
        "industry": co.get("industry"),
        "publicly_traded_symbol": traded_symbol or None,
        "founded_year": co.get("founded_year"),
        "annual_revenue_usd_readable": co.get("annual_revenue_usd_readable"),
        "total_funding_usd": co.get("total_funding_usd"),
        "alexa_ranking": co.get("alexa_ranking"),
        "num_jobs": co.get("num_jobs"),
        "seo_description": co.get("seo_description"),
        "long_description": co.get("long_description"),
        "company_tags": co.get("company_tags"),
        "keywords": co.get("company_keywords"),
        "segment": "smb_mid",
    }

    def done(action: str, segment: str, extra_reasons: list[str]) -> dict:
        profile["segment"] = segment
        return {
            "action": action,
            "segment": segment,
            "reasons": [*extra_reasons, *reasons],
            "profile": profile,
        }

    # 1) Government / military / defense domains -> always out of scope.
    if domain.endswith(_DOMAIN_EXCLUDE_ENDINGS) or ".gov." in domain or ".mil." in domain:
        return done("exclude", "public_sector", [f"public-sector domain ({domain})"])

    # 2) Unambiguous public-sector / defense business (name, description, tags).
    if any(p in blob for p in _PUBLIC_SECTOR_PHRASES):
        hit = next(p for p in _PUBLIC_SECTOR_PHRASES if p in blob)
        return done("exclude", "public_sector", [f"{hit!r} in public metadata"])

    # 3) Extremely large multinational enterprise (employee count).
    if emp_num is not None and emp_num >= ENTERPRISE_EMPLOYEE_MIN:
        return done("exclude", "large_enterprise", [f"{emp_block} employees"])

    # 4) Service providers (IT/consulting/cloud/migration/MSP...) sell what we
    #    sell - they are competitors, not buyers. Exclude decisively.
    #    Check both free-text metadata and the industry-taxonomy name.
    blob_hit = next((p for p in _SERVICE_PROVIDER_PHRASES if p in blob), None)
    ind_hit = next(
        (p for p in _PROVIDER_INDUSTRY_PHRASES if p in str(co.get("industry") or "").lower()),
        None,
    )
    if blob_hit or ind_hit:
        hit, source = (ind_hit, "industry") if ind_hit else (blob_hit, "public metadata")
        return done("exclude", "service_provider", [f"service-provider signal {hit!r} in {source}"])

    # 5) Publicly traded listings -> large-company territory; penalize, keep if useful.
    if traded_symbol:
        return done("keep", "listing", [f"publicly traded ({traded_symbol}) on {co.get('publicly_traded_exchange') or '?'}"])

    # 6) Government-adjacent but not a public-sector target (vendors/suppliers).
    if any(p in blob for p in _GOVERNMENT_ADJACENT_PHRASES):
        return done("keep", "government_adjacent", ["government-adjacent keyword in public metadata"])

    return done("keep", "smb_mid", [])


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
    """Use the LLM (prompts/prospector.md) to plan TheirStack tech filters +
    description patterns + Serper queries. Falls back to keyword-based plans
    on any LLM failure (DeepSeek -> Gemma -> deterministic)."""
    try:
        plan = parse_json(
            chat(
                load_prompt("prospector.md"),
                f"Target Product: {target_product}",
                max_tokens=400,
            )
        )
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

    # 1) TheirStack searches. Query strategy is crucial: tech-only queries
    #    (vmware/oracle/sap) surface mostly large enterprises and defense firms,
    #    which the quality filter then rejects - starving the pool. So we COMBINE
    #    buyer-industry description patterns with legacy-tech filters in one
    #    payload (AND semantics): "manufacturing company running VMware/SAP".
    #    Tech-only payloads run second as a safety net. NOTE: never send page:0
    #    (TheirStack 422s); omit page entirely. Each result costs 3 API credits,
    #    so we cap per-payload results and stop once we have enough unique
    #    companies to survive the quality filter (target pool > limit).
    tech_slugs = plan["technology_slugs"]
    desc_pats = plan["description_patterns"]
    per_payload = max(2, min(limit, limit // 3 or 1))
    target_pool = max(limit, limit * 2)

    theirstack_payloads: list[dict] = []
    if desc_pats:
        theirstack_payloads = [
            {
                "company_description_pattern_or": [pat],
                "tech_filters": {"technology_slug_or": tech_slugs},
                "limit": per_payload,
            }
            for pat in desc_pats
        ]
    if tech_slugs:
        theirstack_payloads += [
            {"tech_filters": {"technology_slug_or": [tech]}, "limit": per_payload}
            for tech in tech_slugs
        ]
    if not theirstack_payloads:
        theirstack_payloads.append(
            {"company_description_pattern_or": [target_product], "limit": per_payload}
        )

    # Exclude 10k+ enterprises at query level (matches the classifier's
    # ENTERPRISE_EMPLOYEE_MIN) so we don't burn 3 credits per giant we'd
    # reject anyway. `_or_null` keeps companies whose size TheirStack
    # doesn't know - the classifier still evaluates them locally.
    for payload in theirstack_payloads:
        payload["max_employee_count_or_null"] = ENTERPRISE_EMPLOYEE_MIN - 1

    for payload in theirstack_payloads:
        if len(companies) >= target_pool:
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

    # Quality filter: drop poor cold-outreach targets (enterprise / public
    # sector / defense) BEFORE they enter the pipeline - metadata-based, no
    # hardcoded company names.
    kept: list[tuple[dict, dict]] = []  # (company, classification)
    excluded: list[tuple[str, list[str]]] = []
    for co in companies:
        cls = classify_company(co)
        if cls["action"] == "exclude":
            name = co.get("name") or co.get("domain") or "unknown"
            domain = (co.get("domain") or "").strip().lower()
            excluded.append((name, cls["reasons"]))
            print(
                f"[prospector] EXCLUDED {name} ({domain}) - "
                + "; ".join(cls["reasons"])
            )
            continue
        kept.append((co, cls))
    print(
        f"[prospector] after filtering: {len(kept)} kept, "
        f"{len(excluded)} excluded (not SMB/mid-market targets)"
    )

    # 2) Serper enrichment for each company
    inserted: list[dict[str, Any]] = []
    for co, cls in kept[:limit]:
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
            company_profile=cls["profile"],
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
