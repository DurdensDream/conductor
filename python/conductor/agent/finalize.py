"""Pure assembly + confidence logic. No LLM, no IO — deterministic functions over the
tool observations, so both the in-process runner and the Temporal workflow call the exact
same code (and the workflow stays deterministic; NFR-1).

Confidence is a formula over concrete tool signals, NOT a number the LLM invents — a
model-asserted 'confidence' isn't calibrated; a formula over 'did location resolve? did
salary parse? how sure is the category? is there a near-duplicate?' is auditable and
testable."""

from __future__ import annotations

from ..models import (
    Category,
    DuplicateHit,
    EnrichedPosting,
    Location,
    PolicySeverity,
    PolicyVerdict,
    RawPosting,
    RemoteType,
    SalaryRange,
)


def _loc(obs: dict) -> Location:
    raw = obs.get("normalize_location")
    return Location.model_validate(raw) if raw else Location()


def _salary(obs: dict) -> SalaryRange | None:
    raw = obs.get("parse_salary")
    if not raw:
        return None
    return SalaryRange.model_validate(raw)


def _category(obs: dict) -> Category:
    raw = obs.get("classify_category")
    return Category.model_validate(raw) if raw else Category()


def _duplicates(obs: dict) -> list[DuplicateHit]:
    raw = obs.get("search_duplicates")
    if not raw:
        return []
    return [DuplicateHit.model_validate(h) for h in raw.get("hits", [])]


def _policy(obs: dict) -> PolicyVerdict:
    raw = obs.get("check_policy")
    return PolicyVerdict.model_validate(raw) if raw else PolicyVerdict()


def compute_confidence(posting: RawPosting, obs: dict) -> float:
    """Weighted mean of per-signal quality in [0,1], with a WARN policy discount. Signals
    that don't apply to this posting (no salary text) are omitted, not scored zero."""
    loc = _loc(obs)
    cat = _category(obs)
    dups = _duplicates(obs)
    policy = _policy(obs)

    components: list[float] = []
    # Location: resolved to at least a city/country (or a clear REMOTE).
    components.append(1.0 if (loc.resolved or loc.remote_type == RemoteType.REMOTE) else 0.0)
    # Salary: only scored when the posting actually had salary text.
    if posting.salary_raw.strip():
        components.append(1.0 if (_salary(obs) and _salary(obs).parsed) else 0.0)
    # Category: the model's own confidence in the classification.
    components.append(max(0.0, min(1.0, cat.confidence)))
    # Dedup: a near-duplicate is a reason to doubt auto-ingest.
    components.append(0.0 if dups else 1.0)

    score = sum(components) / len(components) if components else 0.0
    if policy.severity == PolicySeverity.WARN:
        score *= 0.85  # biased-language / data-quality warning lowers auto-confidence
    return round(score, 4)


def needs_human_review(posting: RawPosting, obs: dict, threshold: float) -> tuple[bool, str]:
    """Routing rule. Returns (needs_review, reason). Policy BLOCK is a hard override that
    forces review regardless of confidence; a near-duplicate and low confidence also route."""
    policy = _policy(obs)
    if policy.severity == PolicySeverity.BLOCK:
        return True, f"policy_block:{','.join(policy.flags) or 'blocked'}"
    dups = _duplicates(obs)
    if dups:
        return True, f"near_duplicate:{dups[0].posting_id}@{dups[0].score}"
    conf = compute_confidence(posting, obs)
    if conf < threshold:
        return True, f"low_confidence:{conf}<{threshold}"
    return False, ""


def assemble_record(posting: RawPosting, obs: dict, trajectory: list[str]) -> EnrichedPosting:
    """Build the final validated record from the observations. Raises pydantic
    ValidationError if the shape is wrong — the output guardrail (FR-5)."""
    loc = _loc(obs)
    return EnrichedPosting(
        external_id=posting.external_id,
        title=posting.title,
        location=loc,
        salary=_salary(obs),
        category=_category(obs),
        remote_type=loc.remote_type,
        duplicates=_duplicates(obs),
        policy=_policy(obs),
        confidence=compute_confidence(posting, obs),
        trajectory=trajectory,
    )
