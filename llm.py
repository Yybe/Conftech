"""llm.py - Shared multi-gateway LLM helper + JSON parsing.

Model chain: PRIMARY step (URL + key + model) is tried first; on any failure
(5xx, rate limit, empty content, malformed JSON) the call automatically falls
back through the configured fallback steps. Each step carries its own
base URL and API key, so the chain can span multiple gateways (GMI Cloud,
OpenCode Zen, ...). All steps are environment-overridable.

All agent prompts live in /prompts/*.md and are loaded by name.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import NamedTuple

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_RETRIES = 3

# CRITICAL: reasoning models on the free tier (mimo-v2.5-free, ...) burn the
# entire token budget on hidden reasoning_content unless reasoning_effort is
# "none". With reasoning on, every free-tier call returns empty content and
# the chain keeps falling through. DO NOT set this to "low" / "high" /
# "medium" without revisiting this comment and the chosen primary model.
REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "none")

# Minimum token budget we send on the first attempt. The internal
# budget-doubling on finish_reason="length" still kicks in if a model needs
# more, but starting at 1500 avoids an extra round-trip for structured JSON
# plans (prompts/prospector.md / researcher.md / scorer.md / ghostwriter.md).
MIN_FIRST_ATTEMPT_TOKENS = 1500


class LLMStep(NamedTuple):
    """One entry in the LLM chain: a specific gateway + model + key."""
    url: str
    key: str
    model: str


# --- Default chain ----------------------------------------------------------
# Defaults are only used when the corresponding env vars are missing. The
# primary step points at GMI Cloud's gmi-serving.com (Anthropic-compatible,
# also served at /v1/chat/completions for OpenAI-shape requests) and uses
# the MiniMaxAI/MiniMax-M3 model. Fallback steps default to the OpenCode Zen
# free-tier models that the same key can also hit.

DEFAULT_PRIMARY_STEP = LLMStep(
    url="https://api.gmi-serving.com/v1/chat/completions",
    key="",  # filled from GMI_API_KEY
    model="MiniMaxAI/MiniMax-M3",
)
DEFAULT_FALLBACK_STEPS: tuple[LLMStep, ...] = (
    LLMStep(
        url="https://opencode.ai/zen/v1/chat/completions",
        key="",  # filled from OPENROUTER_API_KEY
        model="mimo-v2.5-free",
    ),
    LLMStep(
        url="https://opencode.ai/zen/v1/chat/completions",
        key="",
        model="hy3-free",
    ),
    LLMStep(
        url="https://opencode.ai/zen/v1/chat/completions",
        key="",
        model="nemotron-3-ultra-free",
    ),
)


def _resolve_step(
    url_env: str,
    url_default: str,
    key_env: str,
    key_default: str,
    model_env: str,
    model_default: str,
) -> LLMStep | None:
    """Build one chain step from env. Returns None when the model is empty
    (used to skip unconfigured fallbacks silently)."""
    url = (os.getenv(url_env, "") or url_default).rstrip("/") + "/chat/completions"
    key = (os.getenv(key_env, "") or key_default).strip()
    model = (os.getenv(model_env, "") or model_default).strip()
    if not model:
        return None
    return LLMStep(url=url, key=key, model=model)


def resolve_models() -> list[LLMStep]:
    """Return the ordered chain of (url, key, model) steps to try.

    Step 1 (primary) is the GMI Cloud endpoint with the MiniMax-M3 model.
    Steps 2-4 default to the OpenCode Zen free-tier chain, but every step is
    env-overridable so a different gateway / model can be slotted in at any
    position. Steps with an empty model are dropped (skip unconfigured
    fallbacks silently instead of failing the whole chain).
    """
    steps: list[LLMStep] = []
    primary = _resolve_step(
        "GMI_BASE_URL", DEFAULT_PRIMARY_STEP.url.replace("/chat/completions", ""),
        "GMI_API_KEY", DEFAULT_PRIMARY_STEP.key,
        "LLM_MODEL", DEFAULT_PRIMARY_STEP.model,
    )
    if primary:
        steps.append(primary)

    fallback_defaults = DEFAULT_FALLBACK_STEPS
    fallback_envs = (
        ("LLM_FALLBACK_MODEL", "LLM_FALLBACK_KEY", "LLM_FALLBACK_URL"),
        ("LLM_FALLBACK_MODEL_2", "LLM_FALLBACK_2_KEY", "LLM_FALLBACK_2_URL"),
        ("LLM_FALLBACK_MODEL_3", "LLM_FALLBACK_3_KEY", "LLM_FALLBACK_3_URL"),
    )
    for (m_env, k_env, u_env), default in zip(fallback_envs, fallback_defaults):
        # If the user set a custom model name for the slot, build the step;
        # otherwise fall back to the default. Always use the OpenCode URL /
        # OpenCode key for the default models (so the chain works out of the
        # box on a fresh checkout).
        if os.getenv(m_env):
            step = _resolve_step(
                u_env, default.url.replace("/chat/completions", ""),
                k_env, "",
                m_env, default.model,
            )
        else:
            # Default step: use the default URL, but pull the key from the
            # user's OPENROUTER_API_KEY (which is the OpenCode Zen key) so
            # the default chain is wired up with one key per gateway.
            step = _resolve_step(
                u_env, default.url.replace("/chat/completions", ""),
                k_env, os.getenv("OPENROUTER_API_KEY", ""),
                m_env, default.model,
            )
        if step:
            steps.append(step)
    return steps


# Backwards-compatible module-level constants used by older call sites.
OPENROUTER_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://opencode.ai/zen/v1"
).rstrip("/") + "/chat/completions"


def load_prompt(name: str) -> str:
    """Load a prompt file from /prompts, e.g. load_prompt('scorer.md')."""
    if not name.endswith(".md"):
        name += ".md"
    path = Path(__file__).parent / "prompts" / name
    return path.read_text(encoding="utf-8")


def _call_step(
    step: LLMStep, system: str, user: str, max_tokens: int, response_format: dict | None
) -> str:
    """Call one (url, key, model) step. Returns raw text content.

    Retries up to MAX_RETRIES with linear backoff (2s, 4s, 6s) so transient
    429/5xx clear. The first attempt uses the larger of `max_tokens` and
    `MIN_FIRST_ATTEMPT_TOKENS` so structured-JSON plans get a real budget.
    Length-truncated replies double the budget on the next attempt.
    """
    if not step.key:
        raise RuntimeError(
            f"LLM step {step.model} has no API key (set GMI_API_KEY or "
            "OPENROUTER_API_KEY in .env)"
        )

    body = {
        "model": step.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max(max_tokens, MIN_FIRST_ATTEMPT_TOKENS),
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
                step.url,
                headers={
                    "Authorization": f"Bearer {step.key}",
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
                # Reasoning models with hidden reasoning on can spend the
                # whole budget on reasoning_content and finish empty. Double
                # the budget so the retry can actually emit the answer.
                if choice.get("finish_reason") == "length" or message.get("reasoning_content"):
                    body["max_tokens"] = min(max(body["max_tokens"], MIN_FIRST_ATTEMPT_TOKENS) * 2, 4096)
                    last_err = RuntimeError(f"{step.model} returned empty content (reasoning budget exhausted)")
                else:
                    last_err = RuntimeError(f"{step.model} returned empty content")
            else:
                # Pass the status code up so callers can distinguish 5xx
                # (gateway down), 429 (rate limit) and 4xx (bad request) when
                # surfacing errors to the UI.
                snippet = resp.text[:300]
                last_err = RuntimeError(f"LLM gateway {resp.status_code}: {snippet}")
        except (requests.RequestException, KeyError) as exc:
            last_err = exc
        time.sleep(2 * (attempt + 1))  # backoff for free-tier rate limits

    raise RuntimeError(f"LLM call failed for {step.model} after {MAX_RETRIES} attempts: {last_err}")


def chat(
    system: str,
    user: str,
    max_tokens: int = 700,
    response_format: dict | None = None,
    steps: list[LLMStep] | None = None,
) -> str:
    """Call the LLM chain (primary + fallbacks). Returns raw text content.

    Tries the first step; on any failure (5xx, rate limit, empty content) it
    moves to the next step. Each step keeps its own retry budget.
    """
    chain = steps or resolve_models()
    if not chain:
        raise RuntimeError("LLM chain is empty - set GMI_API_KEY and/or LLM_MODEL in .env")
    last_err: Exception | None = None
    for step in chain:
        try:
            return _call_step(step, system, user, max_tokens, response_format)
        except Exception as exc:  # noqa: BLE001 - any failure moves to next step
            last_err = exc
            print(f"[llm] step {step.model} ({step.url}) failed ({exc}); trying next step")
    raise RuntimeError(f"LLM call failed after {len(chain)} step(s): {last_err}")


def chat_json(
    system: str,
    user: str,
    max_tokens: int = 700,
    response_format: dict | None = None,
    steps: list[LLMStep] | None = None,
) -> dict:
    """Like chat(), but ALSO falls back when the reply is not valid JSON.

    The JSON parsing/validation logic is unchanged (parse_json below) - the
    chain just gets another chance when a step returns malformed JSON.
    """
    chain = steps or resolve_models()
    if not chain:
        raise RuntimeError("LLM chain is empty - set GMI_API_KEY and/or LLM_MODEL in .env")
    last_err: Exception | None = None
    for step in chain:
        try:
            reply = _call_step(step, system, user, max_tokens, response_format)
            return parse_json(reply)
        except Exception as exc:  # noqa: BLE001 - failure/rate-limit/bad JSON -> next step
            last_err = exc
            print(f"[llm] step {step.model} failed or returned invalid JSON ({exc}); trying next step")
    raise RuntimeError(f"chat_json failed after {len(chain)} step(s): {last_err}")


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
