"""`python -m conductor.eval [--gate] [--judge] [--out metrics.json]`

Runs the gold set through the agent, prints metrics, optionally runs the LLM-judge, writes
metrics.json, and (with --gate) exits non-zero below the published thresholds — this is
what blocks a merge in CI (FR-7)."""

from __future__ import annotations

import argparse
import json
import sys

from .gate import THRESHOLDS, evaluate_gate
from .harness import run_gold
from .metrics import compute_metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="conductor.eval")
    ap.add_argument("--gate", action="store_true", help="exit non-zero if metrics fall below THRESHOLDS")
    ap.add_argument("--judge", action="store_true", help="also run the LLM-as-judge quality score")
    ap.add_argument("--out", default="", help="write metrics JSON to this path")
    args = ap.parse_args(argv)

    results = run_gold()
    metrics = compute_metrics(results)

    if args.judge:
        from .judge import judge_quality

        q = judge_quality(results)
        if q is not None:
            metrics["llm_judge_quality"] = q

    print("=== Conductor eval ===")
    for k, v in metrics.items():
        floor = THRESHOLDS.get(k)
        suffix = f"   (floor {floor})" if floor is not None else ""
        print(f"  {k:32} {v}{suffix}")

    # metrics.json is written BEFORE the gate verdict so a failing run still uploads its numbers.
    if args.out:
        with open(args.out, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"wrote {args.out}")

    if args.gate:
        ok, failures = evaluate_gate(metrics)
        if not ok:
            print("\nGATE FAILED:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
