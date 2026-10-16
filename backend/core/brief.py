"""AI Shift Brief: an optional explanation layer on top of the deterministic ranking.

The LLM only rewrites the already-ranked Top 3 into a short brief. It cannot change
scores, priorities or order. Its output is validated against the supplied facts; any
violation -> deterministic fallback brief.
"""
import json
import re

from . import analysis
from .services import ai

MAX_CHARS = 900

SYSTEM = (
    "You write a short shift brief for a fleet Maintenance Lead. "
    "Use ONLY the facts in the JSON you are given. Do not add vehicles, numbers, codes, work orders, "
    "causes or priorities that are not in the input. Do not change the order or priorities. "
    "Write 2-4 sentences: what needs attention tonight, the most important evidence, and the immediate actions. "
    'Return JSON: {"brief": "<text>"}'
)


def top3_facts(result):
    facts = []
    for a in result["actions"][:3]:
        facts.append({
            "rank": a["rank"], "vehicle_id": a["vehicle_id"], "priority": a["priority"],
            "depot": a["depot"], "reason": a["reason"], "recommended_action": a["recommended_action"],
            "evidence": [{k: e[k] for k in ("detail", "event_time_local") if e.get(k)} for e in a["evidence"]],
        })
    return facts


def _numbers(text):
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def validate(brief, facts):
    """Return a list of violations (empty = passed)."""
    errors = []
    if not isinstance(brief, str) or not brief.strip():
        return ["empty brief"]
    if len(brief) > MAX_CHARS:
        errors.append(f"too long ({len(brief)} chars)")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", brief.strip()) if s]
    if not 1 <= len(sentences) <= 5:
        errors.append(f"{len(sentences)} sentences (expected 2-4)")

    source = json.dumps(facts)
    allowed_vehicles = {f["vehicle_id"] for f in facts}
    for vid in set(re.findall(r"\bV-\d+\b", brief)) - allowed_vehicles:
        errors.append(f"vehicle not in top 3: {vid}")
    for code in set(re.findall(r"\bP\d{4}\b", brief)) - set(re.findall(r"\bP\d{4}\b", source)):
        errors.append(f"DTC not in evidence: {code}")
    for wo in set(re.findall(r"\bWO-\d+\b", brief)) - set(re.findall(r"\bWO-\d+\b", source)):
        errors.append(f"work order not in evidence: {wo}")
    for n in _numbers(brief) - _numbers(source) - {"1", "2", "3", "4"}:
        errors.append(f"number not in evidence: {n}")
    # Priority claims must match: e.g. "V-151 ... Critical" is rejected if V-151 is High.
    prio = {f["vehicle_id"]: f["priority"] for f in facts}
    for sentence in sentences:
        ids = set(re.findall(r"\bV-\d+\b", sentence)) & allowed_vehicles
        for word in ("Critical", "High", "Medium"):
            if re.search(rf"\b{word}\b", sentence) and ids and all(prio[i] != word for i in ids):
                errors.append(f"priority '{word}' does not match {sorted(ids)}")
    return errors


def fallback_brief(facts):
    parts = []
    crit = [f for f in facts if f["priority"] == "Critical"]
    if crit:
        parts.append(f"{len(crit)} Critical vehicle{'s' if len(crit) > 1 else ''} tonight: "
                     + "; ".join(f'{f["vehicle_id"]} ({f["depot"]}) - {f["reason"].split(";")[0]}' for f in crit) + ".")
    for f in facts:
        parts.append(f'{f["vehicle_id"]}: {f["recommended_action"]}')
    return " ".join(parts)


def build_brief():
    result = analysis.analyze()          # same deterministic ranking as /api/actions
    facts = top3_facts(result)
    base = {"generated_from": [f["vehicle_id"] for f in facts], "as_of_local": result["as_of_local"]}

    ai_out = ai.call_llm_json(SYSTEM, json.dumps({"top3": facts}), required_keys=["brief"], fallback={"brief": None})
    if ai_out.get("_ai_error"):
        return {**base, "source": "fallback", "brief": fallback_brief(facts),
                "validation": {"passed": None, "errors": [f"AI unavailable: {ai_out['_ai_error'][:120]}"]}}

    errors = validate(ai_out["brief"], facts)
    if errors:
        return {**base, "source": "fallback", "brief": fallback_brief(facts),
                "validation": {"passed": False, "errors": errors, "rejected_ai_brief": ai_out["brief"]}}
    return {**base, "source": "ai", "model": ai.MODEL, "brief": ai_out["brief"].strip(),
            "validation": {"passed": True, "errors": []}}
