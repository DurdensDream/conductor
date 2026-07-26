# Metrics

## Eval harness (CI-gating)

The gate runs the gold set (18 cases spanning clean / policy-block / ambiguous / duplicate
/ salary-format / remote-type) through the agent and fails the build below the floors in
[`gate.py`](../python/conductor/eval/gate.py). Numbers below are under the **deterministic
`fake` provider** (what CI uses) — reproducible with `make eval`.

| Metric | Floor (gate) | Observed (fake) | What it measures |
|---|---|---|---|
| `task_success` | 0.90 | **1.000** | mean field-level accuracy (category, remote, salary-parsed, location-resolved, review-routing) |
| `task_success_strict` | 0.75 | **1.000** | fraction of cases with *every* field correct (no partial credit) |
| `review_routing_accuracy` | 0.90 | **1.000** | fraction where `needs_review` matches expected — the auto-ship-vs-escalate call, scored on its own |
| `tool_selection_f1` | 0.90 | **1.000** | trajectory set-F1 vs the expected tool set |
| `tool_selection_exact_order` | 0.80 | **1.000** | fraction with the exact tool order |
| `output_validity` | 0.99 | **1.000** | fraction whose record survives a JSON persist/reload round-trip **and** business invariants (non-blank id/title, confidence∈[0,1], non-empty trajectory) — not a re-validation of the just-built object |
| `llm_judge_quality` | 0.60 | (only with `--judge`) | LLM-as-judge enrichment quality; enforced *only when computed* |
| `avg_tokens_per_run` | — | ~1576 | cost proxy (fake tokens are estimated) |
| `cost_per_run_usd` | — | 0.0 | 0 on local/fake; real under `anthropic` |

The floors sit below the deterministic means with headroom so a **live** model
(`LLM_PROVIDER=ollama|anthropic`) can be noisier without flapping the build. `task_success`,
`review_routing_accuracy`, `tool_selection_f1`, and `output_validity` are **required** — a
missing key or an empty gold set (`n_cases = 0`) fails the gate rather than passing vacuously.
`review_routing_accuracy` is gated *separately* from the lenient mean so a broken router
(auto-shipping everything, or dumping everything on a human) trips the build even though
routing is only one of the five fields inside `task_success`.

## Reliability (the headline outcome)

- **Auto-resolution rate (gold set):** 13 / 18 postings (72%) complete end-to-end with **no
  human touch**; the other 5 correctly route to review (2 policy-block, 2 low-confidence,
  1 near-duplicate). A wrapper would either auto-ship all 18 (including the discriminatory
  one) or dump all 18 on a human. These counts are recomputed from `gold.py` and asserted
  against this file by `test_documented_split_matches_gold`, so they cannot rot.
- **Chaos proof:** the CI chaos test injects **one** `SIGKILL`, on one posting, while its run
  is parked at the review gate — i.e. after every activity has completed. It asserts the run
  is neither lost nor duplicated: a fresh worker rebuilds the trajectory + proposed record
  from event history alone, the run then completes on approval, and the audit log holds one
  row per logical step (unique `idempotency_key`, enforced by a DB `UNIQUE` constraint).

## Observability (live under `make obs-up`)

- **Jaeger** (`:16686`, service `conductor-worker`): one trace per run — `RunWorkflow:…` →
  `StartActivity:agent_decide` / `RunActivity:agent_decide` → `…:run_tool` … All spans come
  from Temporal's `TracingInterceptor` (no hand-rolled spans), so the trace gives step
  structure and per-activity latency; **token counts and cost are metrics, not span
  attributes** — read them from Prometheus/Grafana below.
- **Prometheus** (`:9090`) / **Grafana** (`:3001`): `conductor_runs_total` by *terminal* outcome
  (one increment per run; arrivals are counted separately by `conductor_submissions_total`, so
  the outcome breakdown isn't double-counted), `conductor_tool_latency_seconds`,
  `conductor_llm_latency_seconds`, `conductor_llm_tokens_total`, `conductor_llm_cost_usd_total`.
  Verified live: e.g. `conductor_llm_tokens_total{direction="in"}` populated after one run.
  Every instrument in [`obs/telemetry.py`](../python/conductor/obs/telemetry.py) has a caller —
  pending-review depth is deliberately *not* a metric: `GET /pending` reads it from Temporal,
  which is the source of truth, and a per-process up/down counter would go negative the first
  time a worker died while a run was parked.
