"""check_policy — the guardrail tool.

Deterministic rules first (fast, auditable, no cost): discriminatory language, banned
terms, and salary sanity. A BLOCK severity forces the human-review route regardless of
the confidence score. The rules are the source of truth; an LLM layer can add nuance but
can never *downgrade* a deterministic BLOCK (fail-closed)."""

from __future__ import annotations

import re

from ..models import PolicySeverity, PolicyVerdict, RawPosting, SalaryRange

# Age/gender/nationality discrimination and other disallowed asks. Deliberately blunt;
# a real system would use a curated policy pack, but the enforcement seam is what matters.
_BLOCK_PATTERNS = [
    re.compile(r"\b(men|women|male|female)\s+only\b", re.I),
    re.compile(r"\bno\s+(women|men|disabled|immigrants)\b", re.I),
    re.compile(r"\b(under|below)\s+\d{2}\s+years?\s+(only|preferred)\b", re.I),
    re.compile(r"\b(whites?|christians?|hindus?|muslims?)\s+only\b", re.I),
    re.compile(r"\bunpaid\b.*\b(mandatory|required)\b", re.I),
]
_WARN_PATTERNS = [
    re.compile(r"\b(young|energetic)\s+(team|candidate|individual)\b", re.I),
    re.compile(r"\brockstar|ninja|guru\b", re.I),
    re.compile(r"\bnative\s+speaker\b", re.I),
]


def check_policy(posting: RawPosting, salary: SalaryRange | None = None) -> PolicyVerdict:
    blob = f"{posting.title}\n{posting.description}"
    flags: list[str] = []
    severity = PolicySeverity.NONE

    for rx in _BLOCK_PATTERNS:
        if rx.search(blob):
            flags.append(f"discriminatory:{rx.pattern}")
            severity = PolicySeverity.BLOCK
    for rx in _WARN_PATTERNS:
        if rx.search(blob):
            flags.append(f"biased-language:{rx.pattern}")
            if severity == PolicySeverity.NONE:
                severity = PolicySeverity.WARN

    # Salary sanity: max < min, or absurd magnitudes, is a data-quality WARN.
    if salary and salary.parsed and salary.min is not None:
        if salary.max is not None and salary.max < salary.min:
            flags.append("salary:max_lt_min")
            severity = max(severity, PolicySeverity.WARN, key=_sev_rank)
        if salary.min < 0 or (salary.max or salary.min) > 1e9:
            flags.append("salary:out_of_range")
            severity = max(severity, PolicySeverity.WARN, key=_sev_rank)

    return PolicyVerdict(ok=(severity != PolicySeverity.BLOCK), flags=flags, severity=severity)


def _sev_rank(s: PolicySeverity) -> int:
    return {PolicySeverity.NONE: 0, PolicySeverity.WARN: 1, PolicySeverity.BLOCK: 2}[s]
