"""Metrics over a gold run: task success (field-level), tool-selection accuracy
(trajectory F1 + exact-order), output validity, and cost/run. Pure functions over the
per-case results so they're unit-testable."""

from __future__ import annotations

from ..models import EnrichedPosting


def _field_matches(record: dict, expect: dict, needs_review: bool) -> tuple[int, int]:
    """Return (matched, total) over the expected key fields for one case."""
    loc = record.get("location", {}) or {}
    sal = record.get("salary")
    checks = [
        (record.get("category", {}).get("label") == expect["category_label"]),
        (loc.get("remote_type") == expect["remote_type"]),
        (bool(sal and sal.get("parsed")) == expect["salary_parsed"]),
        (bool(loc.get("resolved")) == expect["location_resolved"]),
        (needs_review == expect["needs_review"]),
    ]
    return sum(1 for c in checks if c), len(checks)


def _traj_prf(actual: list[str], expected: list[str]) -> tuple[float, float, float]:
    a, e = set(actual), set(expected)
    tp = len(a & e)
    precision = tp / len(a) if a else 1.0
    recall = tp / len(e) if e else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def compute_metrics(results: list[dict]) -> dict:
    """`results` items: {record, trajectory, expected_trajectory, expect, needs_review,
    tokens_in, tokens_out, cost_usd, valid}."""
    n = len(results)
    if n == 0:
        return {"n_cases": 0}

    field_scores, strict_pass = [], 0
    f1s, exact_order = [], 0
    valid_count = 0
    total_tokens, total_cost = 0, 0.0

    for r in results:
        matched, total = _field_matches(r["record"], r["expect"], r["needs_review"])
        field_scores.append(matched / total)
        if matched == total:
            strict_pass += 1
        _, _, f1 = _traj_prf(r["trajectory"], r["expected_trajectory"])
        f1s.append(f1)
        if r["trajectory"] == r["expected_trajectory"]:
            exact_order += 1
        valid_count += 1 if r.get("valid") else 0
        total_tokens += r.get("tokens_in", 0) + r.get("tokens_out", 0)
        total_cost += r.get("cost_usd", 0.0)

    return {
        "n_cases": n,
        "task_success": round(sum(field_scores) / n, 4),          # mean field-level accuracy
        "task_success_strict": round(strict_pass / n, 4),          # all fields correct
        "tool_selection_f1": round(sum(f1s) / n, 4),
        "tool_selection_exact_order": round(exact_order / n, 4),
        "output_validity": round(valid_count / n, 4),
        "avg_tokens_per_run": round(total_tokens / n, 1),
        "cost_per_run_usd": round(total_cost / n, 6),
    }


def validate_record(record: dict) -> bool:
    try:
        EnrichedPosting.model_validate(record)
        return True
    except Exception:
        return False
