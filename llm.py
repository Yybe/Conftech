"""llm.py - Shared OpenRouter (Gemma) helper + robust JSON parsing.

All agent prompts live in /prompts/*.md and are loaded by name.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"  # gemma-2-9b-it:free is deprecated (404)
MAX_RETRIES = 3


def load_prompt(name: str) -> str:
    """Load a prompt file from /prompts, e.g. load_prompt('scorer.md')."""
    if not name.endswith(".md"):
        name += ".md"
    path = Path(__file__).parent / "prompts" / name
    return path.read_text(encoding="utf-8")


def chat(system: str, user: str, max_tokens: int = 700) -> str:
    """Call OpenRouter with the configured model. Returns raw text content."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing in .env")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=90,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            last_err = RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
        except (requests.RequestException, KeyError) as exc:
            last_err = exc
        time.sleep(2 * (attempt + 1))  # backoff for free-tier rate limits

    raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts: {last_err}")


def parse_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from an LLM reply.

    Handles markdown fences, stray prose, and trailing commas.
    """
    text = text.strip()
    # Strip markdown fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # Find the outermost balanced {...} block
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in LLM output: {text[:200]!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # tolerate trailing commas
                    cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
                    return json.loads(cleaned)

    raise ValueError(f"Unbalanced JSON in LLM output: {text[:200]!r}")
