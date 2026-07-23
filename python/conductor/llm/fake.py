"""Deterministic, offline LLM. The spine of hermetic CI (NFR-3): no network, no model,
no GPU, byte-identical output for identical input — which is also what makes the chaos
test's "resumes with identical output" assertion airtight.

It is *content-aware*, not a constant: it inspects the schema it's asked to fill and the
prompt it's given, then emits a sensible structured object. That means the agent's
trajectory genuinely varies with the posting (a posting with no salary text won't get a
parse_salary call), so tool-selection-accuracy is a real metric, not a tautology.

The decision prompt carries a compact `STATE_JSON: {...}` block (present fields + tools
already run) that this fake keys on; real models also see it and it only helps them.
"""

from __future__ import annotations

import json
import re

from .base import LLMClient, LLMResult, extract_json

# Canonical order a competent agent would enrich in. The fake calls a tool only when the
# posting actually needs it (mirrors what a good LLM does), then finishes.
_TOOL_ORDER = ["normalize_location", "parse_salary", "classify_category", "search_duplicates"]

# Minimal O*NET-ish keyword table for deterministic classification.
_CATEGORY_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("nurse", "rn", "clinical", "patient", "medical", "healthcare"), "29-1141.00", "Registered Nurse"),
    (("software", "engineer", "developer", "backend", "python", "java"), "15-1252.00", "Software Developer"),
    (("data", "scientist", "ml", "machine learning", "analytics"), "15-2051.00", "Data Scientist"),
    (("driver", "delivery", "cdl", "truck"), "53-3032.00", "Heavy Truck Driver"),
    (("sales", "account executive", "business development"), "41-4012.00", "Sales Representative"),
    (("warehouse", "forklift", "picker", "packer"), "53-7062.00", "Warehouse Laborer"),
    (("teacher", "tutor", "instructor", "educator"), "25-2021.00", "Elementary Teacher"),
    (("cook", "chef", "kitchen", "line cook"), "35-2014.00", "Cook"),
]
_DEFAULT_CATEGORY = ("00-0000.00", "General")


class FakeLLM(LLMClient):
    provider = "fake"
    model = "fake-deterministic"

    def generate(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        # Free-text path (e.g. an LLM-judge rationale) — deterministic canned text.
        text = "OK"
        return LLMResult(text=text, model=self.model, tokens_in=_toks(system + user), tokens_out=_toks(text))

    def generate_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 512
    ) -> tuple[dict | None, LLMResult]:
        kind = _detect(schema)
        if kind == "decision":
            obj = self._decide(user)
        elif kind == "category":
            obj = self._category(user)
        elif kind == "policy":
            obj = self._policy(user)
        elif kind == "judge":
            obj = {"score": 0.9, "reasoning": "fake judge: structurally valid enrichment"}
        else:
            obj = {}
        text = json.dumps(obj)
        result = LLMResult(model=self.model, text=text, tokens_in=_toks(system + user), tokens_out=_toks(text))
        return extract_json(text), result

    # ---- decision policy (ReAct turn) -------------------------------------------------
    def _decide(self, user: str) -> dict:
        state = _state_json(user)
        present = set(state.get("present_fields", []))
        run = set(state.get("tools_run", []))
        for tool in _TOOL_ORDER:
            if tool in run:
                continue
            if tool == "parse_salary" and "salary_raw" not in present:
                continue  # a competent agent skips salary parsing when there's no salary text
            if tool == "normalize_location" and "location_raw" not in present:
                continue
            return {"action": "call_tool", "tool": tool, "tool_args": {}, "reasoning": f"need {tool}"}
        return {"action": "finish", "tool": None, "tool_args": {}, "reasoning": "all needed tools run"}

    # ---- classification --------------------------------------------------------------
    def _category(self, user: str) -> dict:
        text = user.lower()
        for keywords, code, label in _CATEGORY_RULES:
            if any(k in text for k in keywords):
                return {"onet_code": code, "label": label, "confidence": 0.85}
        return {"onet_code": _DEFAULT_CATEGORY[0], "label": _DEFAULT_CATEGORY[1], "confidence": 0.3}

    # ---- policy judgment (LLM layer on top of deterministic rules) --------------------
    def _policy(self, user: str) -> dict:
        return {"ok": True, "flags": [], "severity": "NONE"}


def _toks(s: str) -> int:
    return max(1, len(s) // 4)


def _detect(schema: dict) -> str:
    props = set((schema or {}).get("properties", {}).keys())
    if {"action", "tool"} <= props:
        return "decision"
    if {"onet_code", "label"} <= props:
        return "category"
    if {"severity", "flags"} <= props or {"ok", "flags"} <= props:
        return "policy"
    if {"score", "reasoning"} <= props:
        return "judge"
    return "other"


_STATE_RE = re.compile(r"STATE_JSON:\s*(\{.*\})", re.DOTALL)


def _state_json(user: str) -> dict:
    m = _STATE_RE.search(user)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
