"""cache.py - Disk-based JSON cache for expensive API calls.

Caches TheirStack, Serper, and ScrapeGraphAI responses keyed by a hash of the
request, with a TTL, to cut API costs and avoid rate limits.

Layout: <CACHE_DIR>/<namespace>/<sha256>.json
Each entry: {"ts": <unix timestamp>, "data": <api payload>}
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache"))
DEFAULT_TTL_DAYS = float(os.getenv("CACHE_TTL_DAYS", "7"))


def _key(namespace: str, payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path(namespace: str, payload: dict) -> Path:
    p = CACHE_DIR / namespace
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{_key(namespace, payload)}.json"


def get(namespace: str, payload: dict, ttl_days: float | None = None) -> dict | None:
    """Return cached payload or None if missing/expired."""
    ttl = DEFAULT_TTL_DAYS if ttl_days is None else ttl_days
    fp = _path(namespace, payload)
    if not fp.exists():
        return None
    try:
        entry = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    age_days = (time.time() - entry.get("ts", 0)) / 86400.0
    if age_days > ttl:
        return None
    return entry.get("data")


def set(namespace: str, payload: dict, data) -> None:
    """Store the response for a request payload."""
    fp = _path(namespace, payload)
    try:
        fp.write_text(
            json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        # Cache must never break the pipeline.
        pass


def cached(namespace: str, ttl_days: float | None = None):
    """Decorator: `@cached("theirstack")` around fn(payload: dict) -> response dict."""

    def decorator(fn):
        def wrapper(payload: dict, **kwargs):
            hit = get(namespace, payload, ttl_days)
            if hit is not None:
                return hit
            data = fn(payload, **kwargs)
            set(namespace, payload, data)
            return data

        return wrapper

    return decorator


def clear(namespace: str | None = None) -> None:
    """Delete cached entries, optionally scoped to one namespace."""
    if namespace is None:
        for p in CACHE_DIR.glob("*/*.json"):
            p.unlink(missing_ok=True)
    else:
        for p in (CACHE_DIR / namespace).glob("*.json"):
            p.unlink(missing_ok=True)
