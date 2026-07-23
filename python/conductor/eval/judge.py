"""Optional LLM-as-judge (only runs with --judge and a configured judge model). Scores
each enriched record's quality 0..1; the mean becomes `llm_judge_quality`, which the gate
enforces only when it was actually computed (a judge-less run still gates on the
deterministic metrics)."""

from __future__ import annotations

from ..llm.factory import get_judge_llm

_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "reasoning": {"type": "string"}},
    "required": ["score", "reasoning"],
}
_SYSTEM = (
    "You are a strict data-quality judge for enriched job postings. Given the original title and the "
    "enriched record, score overall enrichment quality from 0 (wrong/unusable) to 1 (clean and correct). "
    "Return JSON {score, reasoning}."
)


def judge_quality(results: list[dict]) -> float | None:
    llm = get_judge_llm()
    scores: list[float] = []
    for r in results:
        rec = r["record"]
        user = f"title: {rec.get('title')}\nenriched: {rec}"
        parsed, _ = llm.generate_json(_SYSTEM, user, _SCHEMA, max_tokens=200)
        if parsed and isinstance(parsed.get("score"), (int, float)):
            s = float(parsed["score"])
            # Drop NaN/inf defensively (min(1.0, nan) == 1.0 would silently inflate the mean).
            if s == s and -1e9 < s < 1e9:
                scores.append(max(0.0, min(1.0, s)))
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)
