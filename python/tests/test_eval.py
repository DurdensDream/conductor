"""Eval-harness tests (FR-7): the gold set clears the gate under the deterministic
provider, the gate itself fails safe (empty gold set / missing required metric), and the
auto-resolution split published in the docs is the one `gold.py` actually produces."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from conductor.eval.gate import THRESHOLDS, evaluate_gate
from conductor.eval.harness import run_gold
from conductor.eval.metrics import _traj_prf, compute_metrics, validate_record

_REPO = Path(__file__).resolve().parents[2]


def _prose(path: Path) -> str:
    """Doc text with runs of whitespace collapsed, so a claim still matches after re-wrapping."""
    return " ".join(path.read_text().split())


def test_gold_set_passes_gate():
    metrics = compute_metrics(run_gold())
    ok, failures = evaluate_gate(metrics)
    assert ok, failures
    assert metrics["n_cases"] >= 15
    assert metrics["output_validity"] == 1.0
    assert metrics["review_routing_accuracy"] == 1.0


def test_documented_split_matches_gold():
    """The published auto-resolution split is a claim about `gold.py`, so derive it here and
    assert every doc that quotes it quotes these numbers. Stops the published split from
    rotting the next time a gold case is added or its expected routing changes."""
    results = run_gold()
    routed = [r for r in results if r["needs_review"]]
    by_reason = Counter(r["review_reason"].split(":")[0] for r in routed)
    auto, total = len(results) - len(routed), len(results)
    pct = round(100 * auto / total)
    breakdown = (f"{by_reason['policy_block']} policy-block, {by_reason['low_confidence']} "
                 f"low-confidence, {by_reason['near_duplicate']} near-duplicate")

    metrics_md = _prose(_REPO / "docs" / "METRICS.md")
    assert f"gold set ({total} cases" in metrics_md
    assert f"{auto} / {total} postings ({pct}%)" in metrics_md
    assert f"the other {len(routed)} correctly route to review ({breakdown})" in metrics_md

    readme = _prose(_REPO / "README.md")
    assert f"**{pct}%** of gold postings ({auto} / {total}) auto-resolve" in readme
    assert f"the other {len(routed)} route correctly ({breakdown})" in readme


def test_gate_fails_on_empty_gold_set():
    ok, failures = evaluate_gate({"n_cases": 0})
    assert not ok
    assert any("empty gold set" in f for f in failures)


def test_gate_fails_on_missing_required_metric():
    # tool_selection_f1 present but task_success absent -> required-metric failure.
    ok, failures = evaluate_gate({"n_cases": 5, "tool_selection_f1": 1.0, "output_validity": 1.0})
    assert not ok
    assert any("task_success missing" in f for f in failures)


def test_gate_fails_below_floor():
    metrics = {"n_cases": 10, "task_success": 0.5, "tool_selection_f1": 1.0, "output_validity": 1.0}
    ok, failures = evaluate_gate(metrics)
    assert not ok
    assert any("task_success" in f for f in failures)


def test_judge_floor_only_enforced_when_present():
    # No llm_judge_quality key -> not enforced even though it's in THRESHOLDS.
    assert "llm_judge_quality" in THRESHOLDS
    metrics = {"n_cases": 10, "task_success": 1.0, "review_routing_accuracy": 1.0,
               "tool_selection_f1": 1.0, "tool_selection_exact_order": 1.0, "output_validity": 1.0}
    ok, _ = evaluate_gate(metrics)
    assert ok


def test_gate_fails_when_review_routing_broken():
    # A router that mis-routes (auto-ships what should escalate) must trip the gate even if
    # the lenient mean task_success stays high — this is the whole point of gating routing.
    metrics = {"n_cases": 18, "task_success": 0.95, "review_routing_accuracy": 0.50,
               "tool_selection_f1": 1.0, "output_validity": 1.0}
    ok, failures = evaluate_gate(metrics)
    assert not ok
    assert any("review_routing_accuracy" in f for f in failures)


def test_gate_requires_review_routing_metric():
    # Missing the routing metric entirely (renamed key) fails, not passes vacuously.
    metrics = {"n_cases": 18, "task_success": 1.0, "tool_selection_f1": 1.0, "output_validity": 1.0}
    ok, failures = evaluate_gate(metrics)
    assert not ok
    assert any("review_routing_accuracy missing" in f for f in failures)


def test_validate_record_enforces_business_invariants():
    good = {"external_id": "x", "title": "Engineer", "location": {}, "category": {},
            "remote_type": "REMOTE", "confidence": 0.8, "trajectory": ["classify_category"]}
    assert validate_record(good)
    # schema-valid but semantically empty variants must be rejected
    assert not validate_record({**good, "title": ""})
    assert not validate_record({**good, "external_id": ""})
    assert not validate_record({**good, "trajectory": []})


def test_trajectory_f1():
    assert _traj_prf(["a", "b"], ["a", "b"])[2] == 1.0
    p, r, f1 = _traj_prf(["a"], ["a", "b"])
    assert p == 1.0 and r == 0.5
