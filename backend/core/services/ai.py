"""Single home for every LLM call.

Pattern: one function per AI task -> call_llm_json(...) -> validated dict.
Never raises to the view: on any failure it returns the caller's fallback
with "_ai_error" set, so the UI can show a degraded-but-working result.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()  # "anthropic" | "openai"
MODEL = os.getenv(
    "LLM_MODEL",
    "claude-sonnet-5-5" if PROVIDER == "anthropic" else "gpt-4o-mini",
)
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "30"))
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))


def _raw_completion(system: str, user: str) -> str:
    if PROVIDER == "openai":
        from openai import OpenAI

        client = OpenAI(timeout=TIMEOUT_S)  # reads OPENAI_API_KEY
        resp = client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    import anthropic

    client = anthropic.Anthropic(timeout=TIMEOUT_S)  # reads ANTHROPIC_API_KEY
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _extract_json(text: str) -> dict:
    """Parse JSON even if the model wrapped it in prose or ```json fences."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def call_llm_json(system: str, user: str, required_keys: list[str], fallback: dict) -> dict:
    """Ask the model for a JSON object; validate required keys; never raise."""
    system = system + "\n\nRespond with ONLY a valid JSON object. No prose, no markdown."
    try:
        data = _extract_json(_raw_completion(system, user))
        missing = [k for k in required_keys if k not in data]
        if missing:
            raise ValueError(f"missing keys: {missing}")
        data["_ai_error"] = None
        return data
    except Exception as exc:  # network, auth, parse, validation
        logger.warning("LLM call failed: %s", exc)
        return {**fallback, "_ai_error": str(exc)}


# ---- Task functions (add one per AI feature) -------------------------------

def ping() -> dict:
    """Smoke test: proves key, model, and JSON parsing all work."""
    return call_llm_json(
        system="You are a health-check endpoint.",
        user='Return {"status": "ok", "message": "<one short friendly sentence>"}',
        required_keys=["status", "message"],
        fallback={"status": "fallback", "message": "AI unavailable; using fallback."},
    )
