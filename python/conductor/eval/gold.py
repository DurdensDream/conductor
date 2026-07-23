"""The gold set: raw postings with expected key fields + expected tool trajectories +
difficulty tags. Deterministic under the FakeLLM (hermetic CI); becomes a genuine quality
signal under a live model (llama3.2 / claude), where the trajectory and classifications
can diverge from expected.

`expected_trajectory` is the sensible tool set for the posting (location only if there's
a location string, salary only if there's salary text, always classify + dedup) — that's
what makes tool-selection-accuracy meaningful rather than tautological."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GoldCase:
    tag: str
    raw: dict
    expect: dict                       # category_label, remote_type, salary_parsed, location_resolved, needs_review
    expected_trajectory: list[str]
    seed: dict | None = None           # a prior posting to place in the dedup corpus first
    extra: dict = field(default_factory=dict)


def _traj(has_loc: bool, has_sal: bool) -> list[str]:
    t = []
    if has_loc:
        t.append("normalize_location")
    if has_sal:
        t.append("parse_salary")
    t += ["classify_category", "search_duplicates"]
    return t


def _case(tag, title, desc, loc, sal, *, category, remote, needs_review, seed=None) -> GoldCase:
    return GoldCase(
        tag=tag,
        raw=dict(source="gold", external_id=f"gold-{tag}", title=title, description=desc,
                 location_raw=loc, salary_raw=sal),
        expect=dict(category_label=category, remote_type=remote, salary_parsed=bool(sal),
                    location_resolved=bool(loc), needs_review=needs_review),
        expected_trajectory=_traj(bool(loc), bool(sal)),
        seed=seed,
    )


GOLD: list[GoldCase] = [
    _case("clean-eng", "Senior Python Engineer", "Backend Kafka Postgres. Hybrid role.",
          "Bangalore, India", "$120k-$150k/yr", category="Software Developer", remote="HYBRID", needs_review=False),
    _case("clean-nurse", "Registered Nurse ICU", "Patient care, night shift.",
          "London", "", category="Registered Nurse", remote="UNKNOWN", needs_review=False),
    _case("clean-driver", "Delivery Driver CDL", "Local delivery routes, onsite.",
          "Austin", "$22/hr", category="Heavy Truck Driver", remote="ONSITE", needs_review=False),
    _case("clean-ds", "Data Scientist", "Machine learning, analytics, remote.",
          "Berlin", "60000-90000 EUR per year", category="Data Scientist", remote="REMOTE", needs_review=False),
    _case("clean-warehouse", "Warehouse Forklift Operator", "Picker packer, onsite.",
          "Toronto", "$19/hr", category="Warehouse Laborer", remote="ONSITE", needs_review=False),
    _case("clean-teacher", "Elementary Teacher", "Classroom instructor, onsite, full time.",
          "New York", "$55k/yr", category="Elementary Teacher", remote="ONSITE", needs_review=False),
    _case("clean-cook", "Line Cook", "Kitchen, evening shift.",
          "San Francisco", "$25/hour", category="Cook", remote="UNKNOWN", needs_review=False),
    _case("clean-sales", "Sales Representative", "Business development, hybrid.",
          "Mumbai", "₹12,00,000 per annum", category="Sales Representative", remote="HYBRID", needs_review=False),
    _case("remote-eng", "Software Engineer", "Fully remote backend developer.",
          "Remote", "$100k-$130k/yr", category="Software Developer", remote="REMOTE", needs_review=False),
    _case("no-salary-eng", "Backend Developer", "Java microservices, hybrid.",
          "London", "", category="Software Developer", remote="HYBRID", needs_review=False),
    # ---- policy blocks (forced review) ----
    _case("policy-men-only", "Warehouse Packer - men only", "No women. Forklift experience.",
          "Austin", "$18/hr", category="Warehouse Laborer", remote="UNKNOWN", needs_review=True),
    _case("policy-no-immigrants", "Cook wanted", "no immigrants. kitchen work.",
          "Berlin", "$20/hr", category="Cook", remote="UNKNOWN", needs_review=True),
    # ---- ambiguous / low confidence (forced review) ----
    _case("ambiguous-empty", "Mystery Opportunity", "Exciting role, details TBD.",
          "", "", category="General", remote="UNKNOWN", needs_review=True),
    _case("ambiguous-noloc", "Great Job", "Join our team!",
          "", "$50k/yr", category="General", remote="UNKNOWN", needs_review=True),
    # ---- salary parse variety (all clean) ----
    _case("salary-k", "Software Engineer", "Backend, hybrid.",
          "Austin", "80k", category="Software Developer", remote="HYBRID", needs_review=False),
    _case("salary-range-eur", "Data Scientist", "ML, onsite.",
          "Berlin", "45000-60000 EUR", category="Data Scientist", remote="ONSITE", needs_review=False),
    _case("salary-gbp-hr", "Cook", "Kitchen, hybrid.",
          "London", "£30/hour", category="Cook", remote="HYBRID", needs_review=False),
    # ---- duplicate detection (forced review) ----
    _case("duplicate", "Senior Python Engineer", "Backend Kafka Postgres Hybrid",
          "Bangalore, India", "$120k-$150k/yr", category="Software Developer", remote="HYBRID", needs_review=True,
          seed=dict(external_id="existing-1", title="Senior Python Engineer",
                    description="Backend Kafka Postgres Hybrid")),
]
