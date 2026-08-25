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
import urllib.parse
from typing import Any

import requests
from dotenv import load_dotenv
import cache
import database as db
from llm import chat, load_prompt, parse_json
from search import (
    extract_emails_from_text,
    resolve_official_website,
    search_contacts_for_domain,
    web_search,
)

load_dotenv()


class NoNewLeadsError(RuntimeError):
    """Raised when prospect() found companies but every one of them is already
    in the database. The web layer surfaces this as 'no new leads for
    {target_product}' instead of pretending the run was a success."""

THEIRSTACK_URL = "https://api.theirstack.com/v1/companies/search"
SERPER_URL = "https://google.serper.dev/search"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.IGNORECASE)
serper_search = web_search
extract_emails = extract_emails_from_text



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
    # Cybersecurity / compliance providers - sell what a "Cybersecurity &
    # Compliance" service sells, never buyers of it.
    "cybersecurity company", "cybersecurity services", "cyber security services",
    "cybersecurity consulting", "cyber security consulting",
    "managed security", "mssp", "managed security service provider",
    "security operations center", "soc as a service", "soc services",
    "security monitoring services", "managed detection and response",
    "penetration testing", "pentest", "offensive security", "red team",
    "security consulting", "information security consulting",
    "compliance consulting", "compliance services",
    "iso 27001 consulting", "gdpr consulting", "hipaa consulting",
    "data privacy consulting", "data protection consulting",
    "identity and access management solutions", "iam solutions",
    "identity management solutions", "access management solutions",
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
    "computer and network security",
    "security and investigations",
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


# --- Web search fallback discovery (zero-auth DuckDuckGo + Serper) -----------

#
# Google SERP cannot match structured tech stacks the way TheirStack does, so
# this is a lower-precision path: it extracts LinkedIn company pages for
# BUYER-indicator queries (industry + "family owned"/"founded in", never the
# service topic itself, which only surfaces providers), then resolves each
# company's real website domain so research/score can still crawl it. The
# provider filter runs on whatever metadata we got (name, domain, snippet).

def _serper_discover(target_product: str, limit: int) -> list[dict[str, Any]]:
    """Discovery-only fallback. Returns TheirStack-shaped company dicts
    (name/domain/linkedin_url/long_description) built from SERP results."""
    buyer_indicators = ["family owned", "founded in", "employee owned", "established in"]
    industries = ["manufacturing", "logistics", "distribution", "wholesale",
                  "retail", "food and beverage", "healthcare", "pharmaceuticals"]
    queries: list[str] = []
    for ind in industries:
        for hint in buyer_indicators:
            queries.append(f'site:linkedin.com/company {ind} "{hint}" -staffing -recruiting -consulting')
    # Cap discovery work: 6 queries x 10 results is plenty for limit leads.
    queries = queries[:6]

    seen_slugs: set[str] = set()
    companies: list[dict[str, Any]] = []
    for q in queries:
        if len(companies) >= limit * 3:
            break
        try:
            sr = serper_search(q)
        except Exception as exc:
            print(f"[prospector] Serper discovery query failed ({q}): {exc}")
            continue
        for item in sr.get("organic", []) or []:
            link = str(item.get("link") or "")
            if "linkedin.com/company/" not in link:
                continue
            slug = link.rstrip("/").split("/company/")[-1].split("?")[0]
            if not slug or slug.lower() in ("company",) or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            name = (str(item.get("title") or "").split(" | ")[0]
                    .split(" - ")[0].split(" | LinkedIn")[0].strip())
            companies.append({
                "name": name or slug,
                "domain": "",  # resolved below
                "linkedin_url": f"https://www.linkedin.com/company/{slug}",
                "long_description": str(item.get("snippet") or ""),
                "seo_description": str(item.get("snippet") or ""),
            })

    # Resolve each company's real website domain (skip social/blog/directory
    # sites, and reject domains that don't correlate with the company name -
    # e.g. a Zoominfo/Crunchbase page is NOT the company's own website).
    resolved: list[dict[str, Any]] = []
    for co in companies:
        name = co.get("name") or ""
        domain = resolve_official_website(name)
        if not domain:
            print(f"[prospector] Web search could not resolve domain for {name}; skipping")
            continue

        # Reject a resolved domain that has zero textual overlap with the
        # company name - it's almost certainly a directory/aggregator page
        # (e.g. "Pillar Manufacturing" resolving to zoominfo.com). Family
        # names like "Smith & Sons" legitimately diverge, so this is a soft
        # check: keep if EITHER a name token appears in the domain OR the
        # domain host itself contains a word from the name.
        name_words = set(re.findall(r"[a-z0-9]{3,}", name.lower()))
        host_base = domain.split(".")[0]
        overlap = name_words & {host_base} or any(w in host_base for w in name_words)
        # Substring fallback: "bemismfg.com" vs "Bemis Manufacturing".
        substring_match = any(w and w[:5] in host_base for w in name_words if len(w) >= 5)
        if not (overlap or substring_match):
            print(
                f"[prospector] Domain {domain} does not match company {name!r}; "
                f"skipping (likely directory page)"
            )
            continue
        co["domain"] = domain
        resolved.append(co)

    # Dedupe by domain.
    by_domain: dict[str, dict] = {}
    for co in resolved:
        d = (co.get("domain") or "").strip().lower()
        if d:
            by_domain.setdefault(d, co)
    return list(by_domain.values())


# --- Query building ---------------------------------------------------------

def build_queries(target_product: str, buyer_persona: str = "") -> dict[str, list[str]]:
    """Use the LLM (prompts/prospector.md) to plan TheirStack tech filters +
    description patterns + Serper queries. Falls back to a target-aware
    keyword plan on any LLM failure so different inputs do not collapse into
    the same cached TheirStack payload.

    `buyer_persona` is a free-form description of the IDEAL buyer (industry,
    size, existing tech posture). It steers the plan toward under-instrumented
    prospects instead of saturated incumbents or competitors.
    """
    try:
        user_parts = [f"Target Product: {target_product}"]
        if buyer_persona.strip():
            user_parts.append(f"Buyer Persona: {buyer_persona.strip()}")
        plan = parse_json(
            chat(
                load_prompt("prospector.md"),
                "\n\n".join(user_parts),
                max_tokens=900,
            )
        )
        return {
            "technology_slugs": list(plan.get("technology_slugs", [])),
            "description_patterns": list(plan.get("description_patterns", [])),
            "serper_queries": list(plan.get("serper_queries", [])),
        }
    except Exception as exc:  # LLM down -> deterministic fallback
        print(f"[prospector] LLM query planning failed ({exc}); using keyword fallback.")
        return _keyword_fallback_plan(target_product, buyer_persona)


# --- Target-aware keyword fallback ------------------------------------------
# Different target products map to different tech slugs and description
# patterns so the same hardcoded "cloud migration" payload is not returned
# for every input. We always pair legacy/buyer tech with buyer-industry
# description patterns (manufacturing, logistics, distribution, healthcare,
# retail) - never with the target product name itself, which for service
# targets only surfaces competing providers.

_BUYER_DESCRIPTION_PATTERNS = (
    "manufacturing", "logistics", "distribution", "wholesale",
    "retail chain", "healthcare provider", "family owned",
    "enterprise resource planning", "on-premise",
)

# Keyword -> tech slugs to query. Slugs come from TheirStack's catalog and
# correspond to actual technology installations - the classifier uses them
# to surface real on-prem enterprise systems.
_TARGET_TECH_HINTS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("cloud", "migration", "moderniz", "lift and shift", "lift & shift"),
     ("vmware-vsphere", "microsoft-hyperv", "oracle-database", "sap",
      "windows-server", "microsoft-sql-server")),
    (("cybersecurity", "security", "compliance", "iso 27001", "soc 2", "gdpr", "hipaa"),
     ("cisco", "fortinet", "palo-alto-networks", "okta", "crowdstrike",
      "sentinelone", "rapid7", "splunk")),
    (("license", "cost optim", "software asset", "sam ", "spend"),
     ("sap", "oracle-database", "microsoft-365", "salesforce", "workday",
      "servicenow", "jira", "adobe")),
    (("data", "analytics", "warehouse", "bi "),
     ("snowflake", "tableau", "looker", "power-bi", "redshift",
      "teradata", "databricks")),
    (("devops", "ci/cd", "kubernetes", "container"),
     ("kubernetes", "docker", "jenkins", "gitlab", "github", "argo-cd",
      "terraform", "ansible")),
    (("crm", "sales"),
     ("salesforce", "hubspot", "zoho-crm", "microsoft-dynamics-365",
      "pipedrive")),
]


def _persona_employee_cap(buyer_persona: str) -> int | None:
    """Extract a maximum employee count from a free-form buyer persona.

    Examples that match:
      "mid-market wholesale distributors (50-2000 employees)" -> 2000
      "manufacturing companies with 500 to 5,000 employees"  -> 5000
      "under 1000 employees"                                 -> 1000
      "less than 250 staff"                                  -> 250

    Returns None when no number is mentioned - caller falls back to the
    global ENTERPRISE_EMPLOYEE_MIN ceiling (10k).
    """
    import re as _re

    text = (buyer_persona or "").lower()
    # 1) "N to M employees" / "N-M employees" / "N - M employees"
    m = _re.search(r"(\d[\d,]*)\s*(?:to|-|–)\s*(\d[\d,]*)\s*(?:employees|staff|people|headcount)", text)
    if m:
        return int(m.group(2).replace(",", ""))
    # 2) "under N" / "less than N" / "fewer than N" / "< N"
    m = _re.search(r"(?:under|less than|fewer than|<)\s*(\d[\d,]*)\s*(?:employees|staff|people|headcount)?", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


# Persona hints that should add an EXPLICIT exclude for competitors. The
# prospector already filters competitors via the `classify_company` rules, but
# the persona is the most reliable place to encode "is the company a vendor
# of the same category" when the category is ambiguous from the target text.
_COMPETITOR_EXCLUDE_HINTS = (
    "software", "service", "provider", "platform", "consulting",
    "consultancy", "saas", "vendor", "msp", "agency",
)


def _keyword_fallback_plan(target_product: str, buyer_persona: str = "") -> dict[str, list[str]]:
    """Build a target-aware TheirStack plan when the LLM is unavailable.

    The plan's tech slugs + Serper queries are derived from the target text
    itself, so different products no longer collide into one cached payload.
    The description patterns always come from the buyer-industry set
    regardless of the target (we want BUYERS, not providers).
    """
    low = (target_product or "").lower()
    chosen: tuple[str, ...] = ()
    for keys, slugs in _TARGET_TECH_HINTS:
        if any(k in low for k in keys):
            chosen = slugs
            break
    if not chosen:
        # Generic buyer-tech fallback (still better than the prior hardcoded
        # migration-only set because it stays out of the cloud-migration cache
        # namespace entirely via a different first slug).
        chosen = ("sap", "oracle-database", "microsoft-365", "salesforce",
                  "windows-server", "vmware-vsphere")

    quoted_target = (target_product or "this service").strip()
    serper = [
        f'site:linkedin.com/company manufacturing "{chosen[0]}"',
        f'"{chosen[0]}" "family owned" -staffing -recruiting',
        f'logistics OR distribution company "{chosen[0]}"',
        f'site:linkedin.com/company "{quoted_target}" buyer -provider -consulting',
    ]
    return {
        "technology_slugs": list(chosen),
        "description_patterns": list(_BUYER_DESCRIPTION_PATTERNS),
        "serper_queries": serper,
    }


# --- Core flow --------------------------------------------------------------

def prospect(
    target_product: str,
    limit: int = 10,
    buyer_persona: str = "",
) -> dict[str, Any]:
    """Run the full prospecting flow. Returns:

        {
            "new":     [lead row dicts that were just inserted as status=raw],
            "known":   [existing lead row dicts (any status) for the same
                        domain that were NOT re-inserted],
            "excluded": [(name, reasons), ...]   # dropped by the classifier
        }

    Raises:
        NoNewLeadsError: when upstream search returned companies but every
        one of them was already in the database. The web layer surfaces this
        as an honest "no new leads" message instead of a fake success.

    `buyer_persona` is a free-form description of the IDEAL buyer. It
    steers the search plan and is also stored on each inserted lead
    (`leads.buyer_persona`) so the scorer can use it to enforce the
    saturated-stack / competitor caps in the rubric.
    """
    plan = build_queries(target_product, buyer_persona)
    print(f"[prospector] Target: {target_product!r}")
    if buyer_persona:
        print(f"[prospector] Buyer persona: {buyer_persona!r}")
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

    # Employee-count ceiling: prefer the persona's number (e.g. "50-2000")
    # when one is mentioned; otherwise fall back to the global 10k cap
    # that the classifier uses. `_or_null` keeps companies whose size
    # TheirStack doesn't know so the classifier still evaluates them.
    persona_cap = _persona_employee_cap(buyer_persona)
    employee_cap = min(persona_cap, ENTERPRISE_EMPLOYEE_MIN - 1) if persona_cap else ENTERPRISE_EMPLOYEE_MIN - 1
    for payload in theirstack_payloads:
        payload["max_employee_count_or_null"] = employee_cap

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

    # 1b) Free fallback when TheirStack is unavailable (credits exhausted /
    #     outage): Serper-based discovery of LinkedIn company pages for
    #     buyer-indicator queries, with website-domain resolution. Lower
    #     precision than TheirStack (no tech-stack matching), so the quality
    #     filter + research/score still gate what enters the pipeline.
    if not companies:
        print("[prospector] TheirStack gave nothing; trying Serper fallback discovery.")
        companies = _serper_discover(target_product, limit)
        print(f"[prospector] Serper fallback returned {len(companies)} companies")

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

    # 2) Split new vs already-known leads BEFORE inserting, so the web layer
    #    gets an honest count of how many are actually new for this run.
    new_leads: list[dict[str, Any]] = []
    known_leads: list[dict[str, Any]] = []
    for co, cls in kept[:limit]:
        domain = (co.get("domain") or "").strip().lower()
        name = co.get("name") or co.get("domain") or "unknown"
        website = f"https://{domain}" if domain else None
        if not website:
            continue

        existing = db.find_lead_by_website(website)
        if existing:
            known_leads.append(existing)
            print(
                f"[prospector] DUPLICATE {name} ({website}) - "
                f"already in DB as status={existing.get('status')}"
            )
            continue

        emails: set[str] = set(search_contacts_for_domain(domain, name))
        tech_stack = (co.get("technology_names") or co.get("technologies_found") or [])
        lead = db.insert_lead(
            company_name=name,
            website=website,
            tech_stack=list(tech_stack) if isinstance(tech_stack, list) else [],
            source="theirstack",
            target_product=target_product,
            contact_email=sorted(emails)[0] if emails else None,
            company_profile=cls["profile"],
            buyer_persona=buyer_persona,
        )
        new_leads.append(lead)
        print(f"[prospector] NEW {name} ({website}) status=raw emails={sorted(emails)}")

    if kept and not new_leads:
        # Upstream search actually found companies, but every one is a
        # duplicate. Surface this honestly instead of pretending the run
        # succeeded.
        raise NoNewLeadsError(
            f"All {len(known_leads)} companies found for '{target_product}' "
            "already exist in the database. Try a different target product "
            "or increase the limit to get a wider search."
        )

    return {"new": new_leads, "known": known_leads, "excluded": excluded}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prospect for a target product")
    parser.add_argument("--target", required=True, help="Target product, e.g. 'Cloud Migration Services'")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    db.init_schema()
    leads = prospect(args.target, args.limit)
    print(f"\nDone. {len(leads)} leads in pipeline (status=raw).")
