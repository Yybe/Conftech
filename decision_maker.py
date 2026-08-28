"""decision_maker.py - Find the actual PERSON to email at each company.

This is the module that fixes the biggest flaw in the original pipeline:
the original returned companies, not people. This module:

  1. Resolves a company name -> website domain (if not already known)
  2. Crawls the company's /about, /team, /leadership, /contact pages
  3. Extracts names + titles from those pages
  4. Filters for decision-makers (CTO, CIO, VP Eng, Head of Infra, etc.)
  5. Generates email patterns (first@, first.last@, flast@, f.last@)
  6. Optionally verifies emails via SMTP RCPT-TO (free, no API key)

Output: a DecisionMaker dataclass with name, title, linkedin, email,
email_confidence, and the page URL we found them on.
"""

from __future__ import annotations

import json
import re
import smtplib
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

import search as search_lib
from llm import chat, parse_json

load_dotenv()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Pages to look for team/leadership info, in priority order
_TEAM_PATHS = [
    "/about", "/about-us", "/about/team", "/team", "/our-team",
    "/leadership", "/leadership-team", "/people",
    "/about/leadership", "/company", "/company/team",
    "/management", "/executives", "/board",
    "/contact", "/contact-us",
]

# Title patterns that indicate a DECISION MAKER for technical services
# (CTO, CIO, VP Engineering, Head of Cloud, etc.)
_DECISION_MAKER_TITLE_PATTERNS = [
    r"\bchief\s+technology\s+officer\b", r"\bcto\b",
    r"\bchief\s+information\s+officer\b", r"\bcio\b",
    r"\bchief\s+digital\s+officer\b", r"\bcdio\b",
    r"\bvp\s+(?:of\s+)?(?:engineering|infrastructure|technology|cloud|platform|data)\b",
    r"\bvice\s+president\s+(?:of\s+)?(?:engineering|infrastructure|technology|cloud|platform|data)\b",
    r"\bhead\s+of\s+(?:engineering|infrastructure|technology|cloud|platform|data|it|devops|sre)\b",
    r"\bdirector\s+(?:of\s+)?(?:engineering|infrastructure|technology|cloud|platform|data|it|devops|sre)\b",
    r"\bfounder\b", r"\bco-?founder\b",
    r"\bceo\b", r"\bchief\s+executive\s+officer\b",
    r"\bcoo\b", r"\bchief\s+operating\s+officer\b",
    r"\bpresident\b",
    r"\bcloud\s+architect\b", r"\bsolutions?\s+architect\b",
    r"\bdevops\s+lead\b", r"\bsre\s+lead\b",
    r"\bprincipal\s+engineer\b",
]

# Title patterns that indicate decision makers for non-technical services
_BUSINESS_DECISION_MAKER_TITLE_PATTERNS = [
    r"\bowner\b", r"\bco-?owner\b",
    r"\bgeneral\s+manager\b", r"\bgm\b",
    r"\boperations\s+manager\b", r"\bops\s+manager\b",
    r"\bhead\s+of\s+(?:operations|ops|it|finance|procurement|purchasing)\b",
    r"\bdirector\s+of\s+(?:operations|ops|it|finance|procurement|purchasing)\b",
    r"\bvp\s+(?:of\s+)?(?:operations|ops|it|finance|procurement|purchasing)\b",
]

# Titles to SKIP — these people can't buy the kind of services we sell
_EXCLUDE_TITLE_PATTERNS = [
    r"\bsales\s+(?:rep|representative|manager|director)\b",
    r"\bmarketing\s+(?:manager|director|coordinator|specialist)\b",
    r"\bhr\s+(?:manager|director|coordinator|specialist)\b",
    r"\brecruiter\b", r"\brecruitment\b", r"\btalent\s+acquisition\b",
    r"\bcustomer\s+service\b", r"\bcustomer\s+support\b",
    r"\bcashier\b", r"\bserver\b", r"\bbarista\b", r"\bdriver\b",
    r"\bintern\b", r"\bjunior\b", r"\bassistant\b",
    r"\bcounsel\b", r"\blegal\b",
    r"\boffice\s+manager\b", r"\breceptionist\b",
]

# Email pattern generators, in order of commonness
# Result: a list of patterns to try, in priority order
_EMAIL_PATTERN_FUNCTIONS = [
    # first@domain
    lambda f, l, d: f"{f}@{d}",
    # first.last@domain
    lambda f, l, d: f"{f}.{l}@{d}",
    # flast@domain
    lambda f, l, d: f"{f[0]}{l}@{d}",
    # f.last@domain
    lambda f, l, d: f"{f[0]}.{l}@{d}",
    # firstl@domain
    lambda f, l, d: f"{f}{l[0]}@{d}",
    # lastf@domain
    lambda f, l, d: f"{l}{f[0]}@{d}",
    # last@domain
    lambda f, l, d: f"{l}@{d}",
]


@dataclass
class DecisionMaker:
    """A real person at a company we can email."""
    name: str
    title: str = ""
    company: str = ""
    domain: str = ""
    linkedin_url: str = ""
    source_url: str = ""  # Page where we found their name
    candidate_emails: list[str] = field(default_factory=list)
    verified_email: str = ""
    email_confidence: float = 0.0  # 0-1
    title_match_score: float = 0.0  # 0-1, how well title matches a decision maker role
    # Why we picked them
    notes: str = ""


# ─── Domain resolution ───────────────────────────────────────────────────────

def resolve_domain(company_name: str) -> str | None:
    """Find a company's official website domain. Free, no API key."""
    if not company_name:
        return None
    # If it already looks like a domain, use it
    if "." in company_name and " " not in company_name and "/" not in company_name:
        return company_name.lower().replace("https://", "").replace("http://", "").rstrip("/")
    return search_lib.resolve_official_website(company_name)


# ─── Page crawling ────────────────────────────────────────────────────────────

def _fetch_page(url: str, timeout: int = 12) -> str | None:
    """Fetch a URL and return its HTML."""
    try:
        r = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=timeout, allow_redirects=True,
        )
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        return None
    return None


def _extract_text_from_html(html: str) -> str:
    """Lightweight HTML → text, no dependencies."""
    # Remove scripts, styles, noscript
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<noscript\b[^>]*>.*?</noscript>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _find_team_pages(base_url: str) -> list[str]:
    """Find URLs of team/leadership/about pages on a company website."""
    found: list[str] = []
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in _TEAM_PATHS:
        candidate = urljoin(base, path)
        html = _fetch_page(candidate, timeout=8)
        if html and len(html) > 200:
            # Check if it looks like a real page (not a 404 with weird body)
            text = _extract_text_from_html(html)
            if 200 < len(text) < 200_000 and "404" not in text[:2000].lower() and "not found" not in text[:2000].lower():
                found.append(candidate)
                if len(found) >= 4:  # We don't need 20 pages, just enough
                    break
    return found


# ─── Name + title extraction ─────────────────────────────────────────────────

# Common name patterns: "John Smith", "John A. Smith", "Dr. Jane Doe", etc.
_NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]{1,20}(?:\s+[A-Z]\.)?)\s+([A-Z][a-z]{1,20})\b"
)

_TITLE_KEYWORDS = re.compile(
    r"\b(chief|cto|cio|ceo|coo|cdo|vp|vice\s+president|director|head|"
    r"founder|co-?founder|president|manager|lead|principal|senior|architect|"
    r"engineer|owner|partner)\b",
    re.IGNORECASE,
)


def _extract_names_and_titles(text: str) -> list[tuple[str, str, str]]:
    """Extract (name, title, source_sentence) tuples from page text.

    Looks for patterns like:
      "John Smith, CTO"
      "John Smith — VP of Engineering"
      "CTO John Smith"
      "Jane Doe (Director of Cloud)"
    """
    results: list[tuple[str, str, str]] = []
    seen_names: set[str] = set()

    # Split into sentences
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 5 or len(sent) > 300:
            continue
        if not _TITLE_KEYWORDS.search(sent):
            continue

        # Try to find name + title in this sentence
        name_matches = _NAME_PATTERN.findall(sent)
        if not name_matches:
            continue

        for first, last in name_matches:
            name = f"{first} {last}".strip()
            if name.lower() in seen_names:
                continue
            # Heuristic: skip obvious false positives
            if any(stop in name.lower() for stop in (
                "the ", " and ", " of ", " for ", " to ", " with ", " on ",
                " our ", " your ", " new ", " all ", " read ", " more ",
            )):
                continue
            # Look for a title in the same sentence
            title_match = re.search(
                r"(chief\s+\w+\s+officer|cto|cio|ceo|coo|"
                r"vp\s+of\s+[a-z\s]+|vice\s+president(?:\s+of\s+[a-z\s]+)?|"
                r"head\s+of\s+[a-z\s]+|director\s+of\s+[a-z\s]+|"
                r"founder|co-?founder|president|principal\s+engineer|"
                r"solutions?\s+architect|devops\s+lead|sre\s+lead|"
                r"cloud\s+architect|general\s+manager|operations\s+manager)",
                sent, re.IGNORECASE,
            )
            if title_match:
                title = title_match.group(0).strip()
                seen_names.add(name.lower())
                results.append((name, title, sent))
                if len(results) >= 30:
                    return results
    return results


# ─── Title filtering ──────────────────────────────────────────────────────────

def _title_score(title: str) -> tuple[float, bool]:
    """Score a title: (match_score 0-1, is_excluded)."""
    title_low = title.lower()
    if any(re.search(p, title_low) for p in _EXCLUDE_TITLE_PATTERNS):
        return 0.0, True
    for pattern in _DECISION_MAKER_TITLE_PATTERNS:
        if re.search(pattern, title_low):
            return 1.0, False
    for pattern in _BUSINESS_DECISION_MAKER_TITLE_PATTERNS:
        if re.search(pattern, title_low):
            return 0.7, False
    return 0.3, False


# ─── Email pattern generation ────────────────────────────────────────────────

def _generate_email_patterns(first_name: str, last_name: str, domain: str) -> list[str]:
    """Generate candidate emails for a person. Lowercase, no spaces in domain."""
    domain = domain.lower().replace("https://", "").replace("http://", "").rstrip("/")
    if "/" in domain:
        domain = domain.split("/")[0]
    f = first_name.lower().replace(" ", "").replace(".", "")
    l = last_name.lower().replace(" ", "").replace(".", "")
    if not f or not l:
        return []
    candidates = []
    for fn in _EMAIL_PATTERN_FUNCTIONS:
        try:
            candidates.append(fn(f, l, domain))
        except Exception:
            continue
    # Dedupe, preserve order
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ─── SMTP verification ───────────────────────────────────────────────────────

def _verify_email_smtp(email: str, timeout: int = 5) -> tuple[bool, str]:
    """Try SMTP RCPT-TO to verify an email is deliverable.

    Returns (verified, status_note). Many mail servers intentionally
    reject all RCPT-TO probes (catch-all), in which case we report
    'catch_all' rather than 'invalid' — caller decides what to do.
    """
    if "@" not in email:
        return False, "invalid_format"
    domain = email.split("@")[1]
    try:
        # MX lookup
        import dns.resolver  # type: ignore
        try:
            mx_records = dns.resolver.resolve(domain, "MX")
        except Exception:
            return False, "no_mx_record"
        mx_host = str(mx_records[0].exchange).rstrip(".")
    except ImportError:
        # No dnspython — fall back to guessing the MX host
        mx_host = f"mail.{domain}"

    try:
        with smtplib.SMTP(timeout=timeout) as smtp:
            smtp.connect(mx_host, 25)
            smtp.helo("verify.local")
            smtp.mail("verify@local")
            code, _ = smtp.rcpt(email)
            if code == 250:
                return True, "verified"
            elif code in (550, 551, 553):
                return False, f"rejected_{code}"
            else:
                return False, f"code_{code}"
    except (smtplib.SMTPServerDisconnected, socket.timeout, ConnectionRefusedError) as exc:
        return False, f"connection_{type(exc).__name__}"
    except Exception as exc:
        return False, f"error_{type(exc).__name__}"


# ─── LLM fallback for tricky pages ──────────────────────────────────────────

def _llm_extract_team(page_text: str, company_name: str) -> list[dict[str, str]]:
    """When regex extraction misses, ask the LLM to extract names + titles."""
    from llm import load_prompt
    try:
        prompt = load_prompt("team_extract.md")
        reply = chat(prompt, f"Company: {company_name}\n\nPage text:\n{page_text[:6000]}", max_tokens=800)
        data = parse_json(reply)
        people = data.get("people", [])
        return [p for p in people if isinstance(p, dict) and p.get("name")]
    except Exception:
        return []


# ─── Main entry point ────────────────────────────────────────────────────────

def find_decision_makers(
    company: str,
    domain: str = "",
    max_results: int = 3,
    verify_emails: bool = False,
) -> list[DecisionMaker]:
    """Find decision-makers at a company.

    Args:
        company: Company name
        domain: Optional pre-known website domain. If empty, will resolve.
        max_results: Max number of decision-makers to return
        verify_emails: Whether to SMTP-verify candidate emails (slow)

    Returns:
        List of DecisionMaker, sorted by title match score.
    """
    # 1. Resolve domain if needed
    if not domain:
        domain = resolve_domain(company) or ""
    if not domain:
        return []

    base_url = f"https://{domain}"
    homepage = _fetch_page(base_url, timeout=10)
    if not homepage:
        return []

    # 2. Find team pages
    team_pages = _find_team_pages(base_url)
    if not team_pages:
        # Try homepage directly
        team_pages = [base_url]

    # 3. Extract names + titles from each team page
    candidates: dict[str, DecisionMaker] = {}  # dedupe by lowercase name
    for page_url in team_pages:
        html = _fetch_page(page_url, timeout=10)
        if not html:
            continue
        text = _extract_text_from_html(html)
        # Cap text size to avoid LLM cost blowups
        text = text[:25_000]

        # Try regex first
        extracted = _extract_names_and_titles(text)
        for name, title, sentence in extracted:
            if name.lower() in candidates:
                continue
            title_score, excluded = _title_score(title)
            if excluded:
                continue
            first, last = name.split(" ", 1) if " " in name else (name, "")
            dms = DecisionMaker(
                name=name,
                title=title,
                company=company,
                domain=domain,
                source_url=page_url,
                title_match_score=title_score,
                notes=f"Found on page: {sentence[:200]}",
            )
            dms.candidate_emails = _generate_email_patterns(first, last, domain)
            candidates[name.lower()] = dms

        # If regex got nothing, try LLM
        if not candidates:
            people = _llm_extract_team(text, company)
            for p in people:
                name = p.get("name", "").strip()
                title = p.get("title", "").strip()
                if not name or name.lower() in candidates:
                    continue
                title_score, excluded = _title_score(title)
                if excluded:
                    continue
                first, last = name.split(" ", 1) if " " in name else (name, "")
                dms = DecisionMaker(
                    name=name, title=title, company=company, domain=domain,
                    source_url=page_url, title_match_score=title_score,
                    notes=f"LLM-extracted: {p.get('context', '')[:200]}",
                )
                dms.candidate_emails = _generate_email_patterns(first, last, domain)
                candidates[name.lower()] = dms

    # 4. Rank by title_match_score, optionally verify emails
    ranked = sorted(candidates.values(), key=lambda d: d.title_match_score, reverse=True)
    for dm in ranked[:max_results]:
        if verify_emails and dm.candidate_emails:
            for email in dm.candidate_emails:
                ok, status = _verify_email_smtp(email)
                if ok:
                    dm.verified_email = email
                    dm.email_confidence = 0.95
                    break
            if not dm.verified_email:
                # Couldn't verify — pick the most likely pattern
                dm.verified_email = dm.candidate_emails[0] if dm.candidate_emails else ""
                dm.email_confidence = 0.4
        else:
            # No verification: pick first@domain (most common in SMBs)
            dm.verified_email = dm.candidate_emails[0] if dm.candidate_emails else ""
            dm.email_confidence = 0.6 if dm.verified_email else 0.0

    return ranked[:max_results]


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Find decision-makers at a company")
    parser.add_argument("--company", required=True)
    parser.add_argument("--domain", default="")
    parser.add_argument("--max", type=int, default=3)
    parser.add_argument("--verify", action="store_true", help="SMTP-verify emails")
    args = parser.parse_args()

    dms = find_decision_makers(args.company, args.domain, args.max, args.verify)
    print(f"\n=== {len(dms)} decision-makers at {args.company} ===\n")
    for dm in dms:
        print(f"{dm.name} — {dm.title}")
        print(f"  Domain: {dm.domain}")
        print(f"  Source: {dm.source_url}")
        print(f"  Email: {dm.verified_email} (confidence: {dm.email_confidence:.0%})")
        print(f"  Other candidates: {dm.candidate_emails[1:4]}")
        print(f"  Title match: {dm.title_match_score:.0%}")
        print()
