"""The typed spine. Every tool, the agent, the workflow, the API and the eval harness
speak these Pydantic models — malformed data fails at a model boundary with a typed
error, never as a raw 500 deep in the stack.

`RawPosting` is the input guardrail (size/shape caps at parse time); `EnrichedPosting`
is the output guardrail (a run cannot finish unless the final object validates)."""

from __future__ import annotations

import enum
import re

from pydantic import BaseModel, Field, field_validator

# Bounds that keep a hostile or accidental payload from reaching the LLM. Tuned so a
# real posting (a few KB) always fits and a 1 MB blob or an injection wall does not.
MAX_TEXT = 20_000
MAX_TITLE = 512
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class RemoteType(enum.StrEnum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    UNKNOWN = "UNKNOWN"


class SalaryPeriod(enum.StrEnum):
    YEAR = "YEAR"
    MONTH = "MONTH"
    HOUR = "HOUR"


class PolicySeverity(enum.StrEnum):
    NONE = "NONE"
    WARN = "WARN"
    BLOCK = "BLOCK"


class RunStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    PENDING_REVIEW = "PENDING_REVIEW"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    REVIEW_TIMED_OUT = "REVIEW_TIMED_OUT"
    FAILED_VALIDATION = "FAILED_VALIDATION"


# ---------------------------------------------------------------- input guardrail
class RawPosting(BaseModel):
    """The messy input. Validators enforce size/shape caps *before* any LLM call so an
    oversized or injection-shaped payload is rejected at the boundary."""

    source: str = Field(min_length=1, max_length=128)
    external_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    description: str = Field(default="", max_length=MAX_TEXT)
    location_raw: str = Field(default="", max_length=1024)
    salary_raw: str = Field(default="", max_length=256)
    employment_type_raw: str = Field(default="", max_length=128)
    company: str = Field(default="", max_length=512)

    @field_validator("title", "description", "location_raw", "salary_raw", "company")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        # Strip control characters (a cheap prompt-injection / binary-blob guard). Newlines
        # and tabs are allowed; the exotic C0 range is not.
        return _CONTROL_CHARS.sub("", v)


# ---------------------------------------------------------------- tool outputs
class Location(BaseModel):
    city: str | None = None
    state: str | None = None
    country: str | None = None
    remote_type: RemoteType = RemoteType.UNKNOWN
    raw: str = ""
    resolved: bool = False  # did we produce at least a city or a country?


class SalaryRange(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str = "USD"
    period: SalaryPeriod = SalaryPeriod.YEAR
    raw: str = ""
    parsed: bool = False


class Category(BaseModel):
    onet_code: str = "00-0000.00"
    label: str = "Unclassified"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DuplicateHit(BaseModel):
    posting_id: str
    score: float = Field(ge=0.0, le=1.0)
    title: str = ""


class PolicyVerdict(BaseModel):
    ok: bool = True
    flags: list[str] = Field(default_factory=list)
    severity: PolicySeverity = PolicySeverity.NONE


# ---------------------------------------------------------------- output guardrail
class EnrichedPosting(BaseModel):
    """The clean record. A run cannot COMPLETE unless the final object validates here."""

    external_id: str
    title: str
    location: Location
    salary: SalaryRange | None = None
    category: Category
    remote_type: RemoteType = RemoteType.UNKNOWN
    duplicates: list[DuplicateHit] = Field(default_factory=list)
    policy: PolicyVerdict = Field(default_factory=PolicyVerdict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    trajectory: list[str] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------- agent decision
class AgentAction(enum.StrEnum):
    CALL_TOOL = "call_tool"
    FINISH = "finish"


class AgentDecision(BaseModel):
    """One turn of the agent: either call a named tool with args, or finish. The LLM
    node emits this (structured output); the workflow executes it."""

    action: AgentAction
    tool: str | None = None
    tool_args: dict = Field(default_factory=dict)
    reasoning: str = ""


class ReviewDecision(BaseModel):
    """Carried by the approve/reject signal from the approval UI."""

    approved: bool
    reviewer: str = "unknown"
    note: str = ""
