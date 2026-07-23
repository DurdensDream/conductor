# Conductor — Implementation Plan

Build order is chosen so that **each phase ends in something runnable and verifiable**,
and so the durability/chaos property is provable as early as possible. Maps the brief's
4-week plan onto build phases; every phase lists its *verification* (fresh evidence, not
assertion — the StreamRAG discipline).

Reuse-first: pull the LLM adapter, settings, telemetry, eval-gate shape, and UI scaffold
from `~/Desktop/StreamRAG`; pull the Temporal Compose stack + worker/workflow idioms +
time-skipping tests from `~/Desktop/temporal-order-workflow`. Write new code only where
the domain differs.

---

## Phase 0 — Foundations (contracts before behavior)
- `pyproject.toml`, `settings.py` (pluggable provider config), `models.py` (all Pydantic
  contracts from DESIGN §3), `llm/` adapter incl. **`FakeLLM`** (deterministic, scripted
  per-posting responses — the spine of hermetic CI), `embed/` incl. **fake hash embedder**.
- `db/schema.sql` + a connection pool + `audit.py` (idempotent append).
- **Verify:** `pytest` on model validation + FakeLLM determinism + embedder shape; ruff clean.

## Phase 1 — Tools + agent, no durability yet  *(brief Wk1)*
- The 5 typed tools (`tools/`), each with Pydantic in/out + docstring + a registry.
- `search_duplicates` against pgvector (seed a tiny corpus).
- LangGraph `agent/graph.py`: plan → route → (tool) → finalize, structured output.
- A thin in-process runner so the agent can process a posting end-to-end *without*
  Temporal (proves the agent logic in isolation).
- **Verify:** run the agent on 3 sample postings with FakeLLM → valid `EnrichedPosting`
  + a sensible trajectory; unit test each tool (happy + one invalid input) `[AC-1, AC-2]`.

## Phase 2 — Durable orchestration + chaos  *(brief Wk2 — the differentiator)*
- `temporal/shared.py` (dataclasses), `activities.py` (validate, agent_decide, run_tool,
  check_policy, persist — each idempotent, keyed), `workflow.py` (the driver loop +
  confidence + status), `worker.py` (two-store connect with retry), `client.py`.
- Retry policies + timeouts + idempotency keys + side-effect counter.
- **Chaos test** in a real Temporal server: start → run some activities → `SIGKILL`
  worker → restart → assert identical output + single side effect `[AC-4]`.
- Time-skipping unit tests for happy path, retry-then-succeed, cancel.
- **Verify:** `docker compose up temporal app-db worker` + submit a posting → completes;
  chaos test passes locally against the real server, not just the mock `[FR-4]`.

## Phase 3 — Guardrails + HITL + UI + evals  *(brief Wk3)*
- Input/output guardrails wired into the workflow; policy BLOCK → forced review.
- HITL: `PENDING_REVIEW` + `wait_condition` on approve/reject/cancel signals + SLA.
- FastAPI control plane (`api/app.py`): submit / pending / approve / reject / run-state.
- Next.js approval UI: pending queue + review screen → approve/reject.
- Eval harness (`eval/`): gold set, harness (drives the real workflow), metrics, gate.
- **Verify:** ambiguous posting → appears in UI → approve → same workflow resumes to done
  `[AC-6]`; `python -m conductor.eval --gate` prints metrics + exits non-zero below floors
  `[AC-7]`; garbage input rejected pre-LLM, banned term forced to review `[AC-5]`.

## Phase 4 — Observability + one-command boot + docs  *(brief Wk4)*
- OTel: workflow/activity spans (Temporal interceptor) + GenAI token/cost attributes →
  Jaeger; run/cost/queue metrics → Prometheus → Grafana dashboard (provisioned JSON).
- Full `docker-compose.yml` (base + `--profile obs`), `Makefile`, `.env.example`.
- `.github/workflows/ci.yml`: ruff + unit tests + eval gate + **chaos test** job.
- README (architecture diagram, failure-mode table, chaos proof, published metrics,
  one-command run, "what's next"), `docs/DEMO.md`, `docs/METRICS.md`.
- **Verify:** clean `docker compose up` from a fresh checkout drives a posting to done
  `[AC-9]`; Jaeger shows per-step trace w/ cost, Grafana panels populate `[AC-8]`; README
  metrics == `gate.py` `[AC-10]`.

## Phase 5 — Adversarial review + hardening
- Multi-lens review (durability/determinism, idempotency, guardrail bypass, eval
  vacuity, observability no-op, UI races, Compose/CI correctness). Fix every confirmed
  finding with a live-verified repro, mirroring the StreamRAG hardening round.
- **Verify:** each fix has a before/after evidence line; full suite + chaos + eval green.

## Phase 6 — Ship
- Squash-free, single-line commits. Create private repo under **DurdensDream only**;
  push. Confirm no `joveo` email in any commit; confirm `.env`/secrets git-ignored.

---

## Cross-cutting rules
- **Hermetic by default:** provider=`fake`, embedder=`fake` in tests/CI/eval — no network,
  no model, no GPU `[NFR-3]`. Ollama is the *local demo* default; Anthropic opt-in.
- **Determinism:** nothing non-deterministic in the workflow module `[NFR-1]`.
- **Every side effect idempotent + keyed** `[NFR-2]`.
- **Commit style:** single-line messages, no body, no Co-Authored-By, DurdensDream identity.
- **No "done" without fresh evidence.**

---

## Verified dependency versions & API notes
Verified against live registries/docs on 2026-07-23 (a dedicated verification pass —
knowledge cutoff was Jan 2026, so this caught three things stale copy-paste would break).

**Python (pin exact — `temporalio` and `anthropic` move ~bi-weekly):**
- `temporalio==1.30.0` — API stable, **but** `RetryPolicy` imports from `temporalio.common`
  (not `temporalio.workflow`). `WorkflowEnvironment.start_time_skipping()` unchanged.
- `langgraph==1.2.9` (on the 1.x line since late 2025). `StateGraph`/`add_node`/`add_edge`/
  `add_conditional_edges`/`compile` unchanged — we use these. `create_react_agent` is
  **deprecated** (→ `langchain.agents.create_agent`); we don't use it. `interrupt()` needs
  a checkpointer + thread_id — we deliberately do HITL at the Temporal layer instead
  (research explicitly endorsed this: don't rely on LangGraph's checkpointer for
  cross-activity durability).
- `pydantic>=2.7`, `pydantic-settings>=2.3`, `psycopg[binary]>=3.2`, `pgvector>=0.3`,
  `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `httpx>=0.27`, `numpy>=1.26`,
  `ollama>=0.6`, `anthropic>=0.40`, OTel SDK `>=1.30` + OTLP-gRPC exporter + FastAPI instr.
- `sentence-transformers>=5,<6` (CPU torch only) — demo embedder; **not** in the CI deps
  (CI uses the fake hash embedder).
- `llama3.2:3b` confirmed unreliable for native tool-calling → we route tool selection via
  `format=`/JSON-schema structured output, keep schemas small/flat, and default CI to the
  fake LLM. `qwen3.5:9b` (present locally) is the recommended local model for a quality demo.

**Docker images (pin — some `latest` tags are stale):**
- `temporalio/auto-setup:1.29.7` (⚠️ `latest` frozen at 1.29.3), `temporalio/ui:2.52.1`.
- `jaegertracing/jaeger:2.20.0` (v2 single binary; OTLP **on by default**, ports
  4317/4318/16686 — no `COLLECTOR_OTLP_ENABLED` needed; v1 `all-in-one` is legacy).
- `pgvector/pgvector:pg16` for app-db (bare `postgres:16` lacks the vector extension).
- `postgres:16` for Temporal's own store, `prom/prometheus:v3.13.1`,
  `grafana/grafana:13.1.1`, `otel/opentelemetry-collector-contrib:0.157.0`.

**Design review (folded in):** the two riskiest architectural bets — (1) the Temporal
workflow drives the agent loop with each step an activity, and (2) HITL lives at the
Temporal signal layer, not LangGraph's checkpointer — were both independently validated
by the verification pass. Proceeding on Option C (DESIGN §1).
