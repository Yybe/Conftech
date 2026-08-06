"""llm.py - Shared OpenCode Zen (OpenRouter-compatible) helper + JSON parsing.

Model chain: DeepSeek V4 Flash is the PRIMARY model; if it fails, gets rate
limited, or returns invalid JSON, the call automatically retries with the
fallback model (MiMo V2.5 Free). Both are environment-overridable and served
through the OpenCode Zen gateway at opencode.ai (OpenAI-compatible API).

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

# OpenAI-compatible base URL. OPENROUTER_BASE_URL is kept as the env override
# name for backward compatibility with existing .env files.
OPENROUTER_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://opencode.ai/zen/v1"
).rstrip("/") + "/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash-free"  # primary (fast, strong JSON)
FALLBACK_MODEL = "mimo-v2.5-free"  # fallback (fast MoE, structured outputs)
MAX_RETRIES = 3

# DeepSeek V4 Flash is a reasoning model: unless told otherwise it spends tokens
# on hidden reasoning (reasoning_content) and can exhaust max_tokens before
# producing any visible content. `reasoning_effort: "none"` disables that hidden
# thinking so every token goes into the answer. Override with
# LLM_REASONING_EFFORT (e.g. "low" / "high" to re-enable reasoning).
REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none")


def load_prompt(name: str) -> str:
    """Load a prompt file from /prompts, e.g. load_prompt('scorer.md')."""
    if not name.endswith(".md"):
        name += ".md"
    path = Path(__file__).parent / "prompts" / name
    return path.read_text(encoding="utf-8")


def resolve_models() -> list[str]:
    """Primary then fallback model, de-duplicated, env-overridable.

    - LLM_MODEL            -> primary model (default: DeepSeek V4 Flash Free)
    - LLM_FALLBACK_MODEL   -> fallback model (default: MiMo V2.5 Free)
    """
    primary = (os.getenv("LLM_MODEL", "") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallback = (os.getenv("LLM_FALLBACK_MODEL", "") or FALLBACK_MODEL).strip() or FALLBACK_MODEL
    models: list[str] = []
    for m in (primary, fallback):
        if m and m not in models:
            models.append(m)
    return models


def _call_model(
    model: str, system: str, user: str, max_tokens: int, response_format: dict | None
) -> str:
    """Call the LLM gateway with ONE model. Returns raw text content.

    Preserves the existing retry logic: MAX_RETRIES attempts with linear
    backoff (2s, 4s, 6s) for free-tier rate limits.
    """
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY missing in .env")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        # Disable hidden reasoning by default so free-tier models emit their
        # answer within max_tokens instead of burning it on reasoning_content.
        "reasoning_effort": REASONING_EFFORT,
    }
    if response_format:
        body["response_format"] = response_format

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
                choice = resp.json()["choices"][0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                if content and content.strip():
                    return content
                # Reasoning models (e.g. deepseek-v4-flash-free) can spend the
                # entire token budget on reasoning_content and finish with
                # finish_reason="length" and empty content. Escalate the budget
                # so the retry can actually emit the answer.
                if choice.get("finish_reason") == "length" or message.get("reasoning_content"):
                    body["max_tokens"] = min(max(body["max_tokens"], 700) * 2, 4096)
                    last_err = RuntimeError(f"{model} returned empty content (reasoning budget exhausted)")
                else:
                    last_err = RuntimeError(f"{model} returned empty content")
            else:
                last_err = RuntimeError(f"LLM gateway {resp.status_code}: {resp.text[:300]}")
        except (requests.RequestException, KeyError) as exc:
            last_err = exc
        time.sleep(2 * (attempt + 1))  # backoff for free-tier rate limits

    raise RuntimeError(f"LLM call failed for {model} after {MAX_RETRIES} attempts: {last_err}")


def chat(
    system: str,
    user: str,
    max_tokens: int = 700,
    response_format: dict | None = None,
    models: list[str] | None = None,
) -> str:
    """Call the LLM gateway with the configured model(s). Returns raw text content.

    Tries the primary model first (DeepSeek V4 Flash Free); on failure / rate
    limit / empty reply it falls back to the fallback model. Every model keeps
    its own retries.
    """
    model_list = models or resolve_models()
    last_err: Exception | None = None
    for model in model_list:
        try:
            return _call_model(model, system, user, max_tokens, response_format)
        except Exception as exc:  # noqa: BLE001 - any failure moves to next model
            last_err = exc
            print(f"[llm] model {model} failed ({exc}); trying next model")
    raise RuntimeError(f"LLM call failed after {len(model_list)} model(s): {last_err}")


def chat_json(
    system: str,
    user: str,
    max_tokens: int = 700,
    response_format: dict | None = None,
    models: list[str] | None = None,
) -> dict:
    """Like chat(), but ALSO falls back when the reply is not valid JSON.

    The JSON parsing/validation logic is unchanged (parse_json below) - the
    model chain just gets another chance when the primary model returns
    malformed JSON.
    """
    model_list = models or resolve_models()
    last_err: Exception | None = None
    for model in model_list:
        try:
            reply = _call_model(model, system, user, max_tokens, response_format)
            return parse_json(reply)
        except Exception as exc:  # noqa: BLE001 - failure/rate-limit/bad JSON -> next model
            last_err = exc
            print(f"[llm] model {model} failed or returned invalid JSON ({exc}); trying next model")
    raise RuntimeError(f"chat_json failed after {len(model_list)} model(s): {last_err}")


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
