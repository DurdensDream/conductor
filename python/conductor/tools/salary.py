"""parse_salary — deterministic-first parsing of messy salary strings.

Handles "$80k–$100k/yr", "45000-60000 EUR", "£30/hour", "120000 per year", "competitive",
etc. Deterministic (no LLM) because salary parsing is a solved regex problem and a
formula is auditable; an unparseable string returns parsed=False rather than a
hallucinated number — which the confidence calculation then penalizes."""

from __future__ import annotations

import re

from ..models import RawPosting, SalaryPeriod, SalaryRange

_CURRENCY = {
    "$": "USD", "usd": "USD", "dollar": "USD",
    "£": "GBP", "gbp": "GBP", "pound": "GBP",
    "€": "EUR", "eur": "EUR", "euro": "EUR",
    "₹": "INR", "inr": "INR", "rupee": "INR", "rs": "INR",
}
_PERIOD = [
    (re.compile(r"\b(per\s*hour|/\s*h(ou)?r|hourly|/hr)\b", re.I), SalaryPeriod.HOUR),
    (re.compile(r"\b(per\s*month|/\s*mo(nth)?|monthly|/mo)\b", re.I), SalaryPeriod.MONTH),
    (re.compile(r"\b(per\s*year|/\s*y(ea)?r|annual(ly)?|p\.?a\.?|/yr)\b", re.I), SalaryPeriod.YEAR),
]
# A number with optional thousands separators and an optional 'k' multiplier.
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def _currency(text: str) -> str:
    low = text.lower()
    for token, code in _CURRENCY.items():
        if token in low:
            return code
    return "USD"


def _period(text: str) -> SalaryPeriod:
    for rx, period in _PERIOD:
        if rx.search(text):
            return period
    return SalaryPeriod.YEAR


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        val = float(m.group(1).replace(",", ""))
        if m.group(2):  # 'k' suffix
            val *= 1000
        out.append(val)
    return out


def parse_salary(posting: RawPosting) -> SalaryRange | None:
    raw = posting.salary_raw.strip()
    if not raw:
        return None
    currency = _currency(raw)
    period = _period(raw)
    nums = _numbers(raw)
    if not nums:
        return SalaryRange(currency=currency, period=period, raw=raw, parsed=False)
    lo = min(nums)
    hi = max(nums)
    return SalaryRange(
        min=lo,
        max=hi if hi != lo else None,
        currency=currency,
        period=period,
        raw=raw,
        parsed=True,
    )
