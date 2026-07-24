"""Unit tests for the 5 typed tools: each happy path + one invalid/edge input (AC-1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.embed.fake import HashEmbedder
from conductor.llm.fake import FakeLLM
from conductor.models import MAX_TITLE, PolicySeverity, RawPosting, RemoteType, SalaryPeriod
from conductor.tools.dedup_index import InMemoryIndex
from conductor.tools.registry import ToolContext, run_tool


def _ctx() -> ToolContext:
    return ToolContext(llm=FakeLLM(), embedder=HashEmbedder(384), index=InMemoryIndex())


def _p(**kw) -> RawPosting:
    base = dict(source="test", external_id="x1", title="Software Engineer")
    base.update(kw)
    return RawPosting(**base)


# ---- input guardrail (RawPosting) ----
def test_raw_posting_rejects_oversized_title():
    with pytest.raises(ValidationError):
        _p(title="x" * (MAX_TITLE + 1))


def test_raw_posting_strips_control_chars():
    p = _p(title="Eng\x00ineer\x07")
    assert p.title == "Engineer"


def test_raw_posting_rejects_title_of_only_control_chars():
    # Control-char stripping runs BEFORE min_length, so an all-control-char title collapses
    # to "" and trips the non-empty guard instead of sneaking through.
    with pytest.raises(ValidationError):
        _p(title="\x01\x02")


def test_raw_posting_requires_nonempty_id_and_title():
    with pytest.raises(ValidationError):
        RawPosting(source="t", external_id="", title="x")


# ---- normalize_location ----
def test_location_resolves_known_city_and_remote():
    loc = run_tool("normalize_location", _p(location_raw="Bangalore, India",
                                            description="Fully remote role"), _ctx())
    assert loc.country == "India"
    assert loc.remote_type == RemoteType.REMOTE
    assert loc.resolved


def test_location_unknown_still_positional():
    loc = run_tool("normalize_location", _p(location_raw="Smalltown, Country"), _ctx())
    assert loc.city == "Smalltown"
    assert loc.resolved


def test_location_single_junk_token_is_unresolved():
    # A single unrecognized token (no structure, not in the geocoder) must NOT be marked
    # resolved — else garbage inflates confidence and skips the human-review gate.
    loc = run_tool("normalize_location", _p(location_raw="Zzqqwx garbage"), _ctx())
    assert not loc.resolved


def test_location_empty_is_unresolved():
    loc = run_tool("normalize_location", _p(location_raw=""), _ctx())
    assert not loc.resolved


# ---- parse_salary ----
@pytest.mark.parametrize(
    "raw,mn,mx,cur,per,parsed",
    [
        ("$120k-$150k/yr", 120000, 150000, "USD", SalaryPeriod.YEAR, True),
        ("£30/hour", 30, None, "GBP", SalaryPeriod.HOUR, True),
        ("45000-60000 EUR per year", 45000, 60000, "EUR", SalaryPeriod.YEAR, True),
        ("competitive", None, None, "USD", SalaryPeriod.YEAR, False),
    ],
)
def test_salary_parsing(raw, mn, mx, cur, per, parsed):
    r = run_tool("parse_salary", _p(salary_raw=raw), _ctx())
    assert (r.min, r.max, r.currency, r.period, r.parsed) == (mn, mx, cur, per, parsed)


def test_salary_absent_returns_none():
    assert run_tool("parse_salary", _p(salary_raw=""), _ctx()) is None


# ---- classify_category (FakeLLM deterministic) ----
def test_category_keyword_match():
    cat = run_tool("classify_category", _p(title="Registered Nurse", description="patient care"), _ctx())
    assert cat.label == "Registered Nurse"
    assert cat.onet_code == "29-1141.00"


# ---- search_duplicates ----
def test_dedup_finds_near_duplicate():
    ctx = _ctx()
    doc = "Senior Python Engineer\nBackend Kafka Postgres"
    ctx.index.add("seed", "Senior Python Engineer", ctx.embedder.embed([doc])[0])
    res = run_tool("search_duplicates",
                   _p(external_id="new", title="Senior Python Engineer",
                      description="Backend Kafka Postgres"), ctx)
    assert res.hits and res.hits[0].posting_id == "seed"
    assert res.hits[0].score >= 0.9


def test_dedup_ignores_unrelated():
    ctx = _ctx()
    ctx.index.add("seed", "Registered Nurse", ctx.embedder.embed(["Registered Nurse\nPatient care ICU"])[0])
    res = run_tool("search_duplicates", _p(title="Truck Driver", description="CDL delivery routes"), ctx)
    assert res.hits == []


# ---- check_policy ----
def test_policy_blocks_discriminatory():
    v = run_tool("check_policy", _p(title="Role - men only", description="no women"), _ctx())
    assert v.severity == PolicySeverity.BLOCK
    assert not v.ok


def test_policy_warns_biased_language():
    v = run_tool("check_policy", _p(description="young energetic team, rockstar dev"), _ctx())
    assert v.severity == PolicySeverity.WARN
    assert v.ok  # WARN is not a hard block


def test_policy_clean_is_none():
    v = run_tool("check_policy", _p(description="We value inclusion and diversity"), _ctx())
    assert v.severity == PolicySeverity.NONE


def test_unknown_tool_raises():
    with pytest.raises(KeyError):
        run_tool("does_not_exist", _p(), _ctx())
