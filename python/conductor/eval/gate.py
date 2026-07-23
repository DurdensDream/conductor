"""The CI gate. One THRESHOLDS dict is the single source of truth (also published in the
README). Required metrics must be present and numeric, and n_cases must be > 0 — otherwise
a renamed key or an empty gold set would silently turn the gate permanently green (the
StreamRAG lesson).

Floors sit with headroom below the observed deterministic means so a live-model run can
dip without flapping the build. With the FakeLLM these all sit at/near 1.0; the floors are
tuned for the intended CI provider (fake) but leave room for a noisier real model."""

from __future__ import annotations

THRESHOLDS: dict[str, float] = {
    "task_success": 0.90,
    "tool_selection_f1": 0.90,
    "tool_selection_exact_order": 0.80,
    "output_validity": 0.99,
    # Enforced ONLY when computed (a --judge run); more slack — a small judge is noisy.
    "llm_judge_quality": 0.60,
}

REQUIRED = ("task_success", "tool_selection_f1", "output_validity")


def evaluate_gate(metrics: dict, thresholds: dict[str, float] | None = None) -> tuple[bool, list[str]]:
    thresholds = thresholds or THRESHOLDS
    failures: list[str] = []
    if not metrics.get("n_cases"):
        failures.append("n_cases = 0 (empty gold set — nothing was evaluated)")
    for key, floor in thresholds.items():
        val = metrics.get(key)
        if isinstance(val, (int, float)):
            if val < floor:
                failures.append(f"{key} = {val:.3f} < floor {floor}")
        elif key in REQUIRED:
            failures.append(f"{key} missing — required metric was not computed")
    return (len(failures) == 0), failures
