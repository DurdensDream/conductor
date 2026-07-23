"""Eval-harness tests (FR-7): the gold set clears the gate under the deterministic
provider, and the gate itself fails safe (empty gold set / missing required metric)."""

from __future__ import annotations

from conductor.eval.gate import THRESHOLDS, evaluate_gate
from conductor.eval.harness import run_gold
from conductor.eval.metrics import _traj_prf, compute_metrics


def test_gold_set_passes_gate():
    metrics = compute_metrics(run_gold())
    ok, failures = evaluate_gate(metrics)
    assert ok, failures
    assert metrics["n_cases"] >= 15
    assert metrics["output_validity"] == 1.0


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
    metrics = {"n_cases": 10, "task_success": 1.0, "tool_selection_f1": 1.0,
               "tool_selection_exact_order": 1.0, "output_validity": 1.0}
    ok, _ = evaluate_gate(metrics)
    assert ok


def test_trajectory_f1():
    assert _traj_prf(["a", "b"], ["a", "b"])[2] == 1.0
    p, r, f1 = _traj_prf(["a"], ["a", "b"])
    assert p == 1.0 and r == 0.5
