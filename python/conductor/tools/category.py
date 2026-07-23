"""classify_category — the LLM structured-output tool.

Maps the title + description to an O*NET-style code + label. Uses the LLM's constrained
JSON output (reliable even on small local models) and validates the result into a
`Category`. A parse failure degrades to Unclassified with confidence 0.0 rather than
raising — the confidence gate then routes the run to human review."""

from __future__ import annotations

from ..llm.base import LLMClient
from ..models import Category, RawPosting

_SYSTEM = (
    "You are a job-taxonomy classifier. Given a job title and description, return the single "
    "best O*NET-SOC occupation as JSON with fields: onet_code (string like '15-1252.00'), "
    "label (occupation name), confidence (0..1). Be decisive; pick the closest match."
)


def classify_category(posting: RawPosting, llm: LLMClient) -> Category:
    user = f"title: {posting.title}\ndescription: {posting.description[:2000]}"
    parsed, _ = llm.generate_json(_SYSTEM, user, Category.model_json_schema(), max_tokens=200)
    if not parsed:
        return Category()  # Unclassified, confidence 0.0 -> pushes toward human review
    try:
        return Category.model_validate(parsed)
    except Exception:
        return Category()
