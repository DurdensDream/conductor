# Conductor — Design

Traces to `REQUIREMENTS.md`. Where a requirement is satisfied it is cited as `[FR-n]`.

---

## 1. The one decision everything hangs on: the Temporal ↔ LangGraph boundary

The pattern this repo commits to is *"Temporal for the durable outer workflow + LangGraph
for the inner agentic steps."* There are three ways to wire that, and the choice determines
whether the chaos test is meaningful or theater.

| Option | What runs where | Durability granularity | Verdict |
|---|---|---|---|
| **A. Agent-in-one-activity** | The whole LangGraph agent runs inside a *single* Temporal activity | Whole agent = one unit. A crash re-runs the entire agent from scratch — re-pays every LLM + tool call. | ❌ "Durable" in name only; the resume re-does work and can double-spend. Chaos test would be theater. |
| **B. LangGraph graph run inside the workflow** | Graph execution happens in the workflow sandbox | N/A | ❌ Illegal — LangGraph does arbitrary non-deterministic Python; Temporal's workflow sandbox forbids that. |
| **C. Workflow drives the loop; each step is an activity** | Temporal workflow is the durable state machine; **each LLM inference and each tool call is its own activity**; LangGraph defines the decision graph + structured-output contract that the "decide" activity runs | Per-step. A crash resumes from the last *completed* activity; finished steps are never re-run. | ✅ **Chosen.** |

**Chosen: Option C.** Rationale:

- **Durability is real at the step level.** Temporal event-sources every activity
  result. If the worker dies after `classify_category` succeeded, the restarted worker
  replays history, sees that result, and continues to the next step — it does **not**
  re-call the LLM. That no-re-execution property is Temporal's replay semantics, and it
  only holds under Option C; what the chaos test actually asserts `[FR-4]` is the visible
  consequence — a fresh worker rebuilds the whole run from event history and finishes it
  with no duplicated side effects (it does not count LLM calls).
- **The workflow stays deterministic** `[NFR-1]`. The workflow body only orchestrates
  (branch on results, wait for signals, loop); every non-deterministic thing (LLM, IO,
  embeddings, time) is quarantined in an activity. This is the single rule that makes
  Temporal replay-safe.
- **The event history *is* the trajectory.** Free observability + free eval input: the
  ordered list of "which tool did the agent pick" falls out of the workflow history and
  is exposed via a `query` `[FR-2, FR-7]`.
- **HITL is a first-class Temporal signal, not a LangGraph interrupt.** We deliberately
  do *not* use LangGraph's `interrupt()`/checkpointer for human-in-the-loop. Durable
  pause/resume is exactly what Temporal is best at; layering LangGraph's own persistence
  underneath Temporal's would be two competing sources of truth. LangGraph is used for
  what it's uniquely good at — expressing the agent's decision graph and binding
  tools/structured output to the LLM — and Temporal owns durability. This also insulates
  us from LangGraph HITL-API churn.

**So what, concretely, does LangGraph do here?** It defines *one decision turn* as a small
`StateGraph` (`agent/graph.py`) with two nodes and one conditional edge: `propose` (the LLM
emits an `AgentDecision` via structured output) and `repair` (a deterministic next-tool
fallback), wired `START → propose`, then a conditional edge off `propose` that returns to
`propose` while the proposal is unusable and attempts remain, jumps to `repair` once they
don't, and otherwise goes to `END`. The graph is compiled once per worker and invoked
**inside the `agent_decide` activity** — one turn per call, returning either a tool-call
request or `finish`. The workflow calls `agent_decide`, executes any requested tool as its
own activity, feeds the observation back, and calls `agent_decide` again. This is a ReAct
loop whose *driver* is the durable workflow.

Note what the LLM is **not** trusted with: it does not assemble the final record. Once the
loop finishes, the `finalize_record` activity calls plain deterministic Python
(`agent/finalize.py`) to assemble the `EnrichedPosting`, compute the confidence score, and
decide the review route — no LLM in that path, which is why the same code can back both the
workflow and the eval harness.

```
JobIntakeWorkflow (deterministic, durable)
  ├─ activity: validate_input            (guardrail, idempotent)
  ├─ loop, max N turns:
  │    ├─ activity: agent_decide         (LangGraph turn → next tool | finish)     ← LLM
  │    └─ activity: run_tool[name]       (one of the 4 agent-selectable tools)     ← LLM/IO
  ├─ activity: check_policy              (mandatory guardrail, off the agent menu)
  ├─ activity: finalize_record           (assemble + confidence + review route,
  │                                       deterministic Python; output guardrail)
  ├─ if needs_review (low-confidence OR policy-blocked OR near-duplicate):
  │    └─ wait_condition(approve|reject|cancel signal, SLA timeout)                ← HITL
  └─ activity: persist_record + audit    (idempotent write)
```

---

## 2. Component map

```mermaid
flowchart TB
  subgraph client
    UI[Next.js approval UI]
  end
  subgraph api[FastAPI control plane]
    SUBMIT[POST /postings → start workflow]
    PENDING[GET /pending → query workflows]
    DECIDE[POST /postings/:id/approve|reject → signal]
  end
  subgraph temporal[Temporal]
    TS[(Temporal server + Postgres)]
    WF[JobIntakeWorkflow]
    TUI[Temporal Web UI]
  end
  subgraph worker[Python worker]
    ACT[Activities: validate · agent_decide · tools · policy · persist]
    LG[LangGraph agent graph]
    LLM[Pluggable LLM: fake · ollama · anthropic]
    EMB[Pluggable embedder: fake · bge-small]
  end
  APPDB[(App Postgres + pgvector: postings, audit_log, embeddings)]
  subgraph obs[--profile obs]
    JAEGER[Jaeger traces]
    PROM[Prometheus] --> GRAF[Grafana]
  end

  UI --> SUBMIT & PENDING & DECIDE
  SUBMIT --> TS --> WF
  WF -.dispatch.-> ACT
  ACT --> LG --> LLM
  ACT --> EMB
  ACT --> APPDB
  DECIDE -->|signal| WF
  PENDING -->|list+query| TS
  WF -. OTel .-> JAEGER
  ACT -. OTel metrics .-> PROM
  WF --- TUI
```

- **Temporal server + its own Postgres** — durable workflow state (persistence store).
- **App Postgres + pgvector** — domain data: `postings` (final records), `audit_log`
  (append-only step history), `posting_embeddings` (dedup corpus). Separate from
  Temporal's store: Temporal owns *workflow* state; we own *domain* state `[FR-3]`.
- **Python worker** — hosts the workflow + activities; where LangGraph + the LLM live.
- **FastAPI control plane** — submit a posting (start workflow), list pending (Temporal
  list + per-workflow query), approve/reject (signal). Thin; no business logic.
- **Next.js UI** — pending queue + a review screen; approve/reject → API → signal.

---

## 3. Data contracts (Pydantic — the typed spine) `[FR-1, FR-5]`

```
RawPosting        : source, external_id, title, description, location_raw,
                    salary_raw, employment_type_raw, company
Location          : city?, state?, country?, remote_type ∈ {ONSITE,HYBRID,REMOTE,UNKNOWN}, raw
SalaryRange       : min?, max?, currency, period ∈ {YEAR,MONTH,HOUR}, raw, parsed:bool
Category          : onet_code, label, confidence
DuplicateHit      : posting_id, score, title
PolicyVerdict     : ok:bool, flags:list[str], severity ∈ {NONE,WARN,BLOCK}
EnrichedPosting   : external_id, title, location, salary?, category, remote_type,
                    duplicates:list[DuplicateHit], policy:PolicyVerdict,
                    confidence:float, trajectory:list[str], notes?
AgentDecision     : action ∈ {call_tool, finish}, tool?, tool_args, reasoning
```

- `RawPosting` is the **input guardrail**: size caps, required-field coercion, and an
  injection-shape reject (control chars, absurd length) happen at parse time.
- `EnrichedPosting` is the **output guardrail**: the run cannot finish unless the final
  object parses. `finalize_record` assembles it and a pydantic failure there is raised as a
  non-retryable `ValidationError`, ending the run `FAILED_VALIDATION` — never a partial
  write `[FR-5]`. Structured-output enforcement applies one layer up, to the *decision* and
  *classification* JSON the small local model emits.

Confidence is computed **deterministically** from tool-level signals (salary parsed?
location resolved? category confidence? any duplicate above threshold? policy severity?) —
*not* asked of the LLM. A number the model invents isn't a calibrated confidence; a formula
over concrete signals is auditable and testable. It lives in `agent/finalize.py`, called
from the `finalize_record` activity rather than from the workflow body itself: identical
pure code, but keeping it out of the replay sandbox means the workflow module imports
nothing but the shared dataclasses `[NFR-1]`.

---

## 4. Durable execution model & idempotency `[FR-3, NFR-2]`

- **Activities** carry `start_to_close_timeout` + a `RetryPolicy`
  (`initial_interval`, `backoff`, `maximum_attempts`). LLM/tool activities are
  configured for transient-failure retry; validation activities are effectively
  non-retryable (a schema error won't fix itself).
- **Idempotency key** = `(workflow_run_id, seq, step_name)`, stored as
  `audit_log.idempotency_key` under `UNIQUE (workflow_id, idempotency_key)`; the *run* id is
  in the key so a re-submitted `external_id` gets its own trail instead of colliding with the
  previous run's. `persist_record` upserts on `postings.external_id`. So a retried or replayed
  activity never double-writes — and the chaos test proves it by reading every audit key for
  the workflow back out of Postgres after crash+resume: all unique, all pre-crash keys still
  present, exactly one `persist_record` `[FR-4]`.
- **Determinism guards:** `workflow.now()` never `datetime.now()`; no `random`, no IO,
  no `import` of non-deterministic modules in the workflow module (guarded by
  `workflow.unsafe.imports_passed_through()` for the dataclasses it needs).
- **Compensation (MVP scope):** there are no irreversible external side effects in MVP —
  `persist_record` is the only write outside the audit log and it runs *after* the gate — so
  "compensation" is just: never emit a final record, and leave the audit trail showing why. A
  `cancel` signal at any point unwinds cleanly to a `CANCELLED` audit row and terminal state.
  A real external effect (an ATS push) would need an explicit compensating activity; there
  isn't one because there is nothing yet to undo.

---

## 5. Human-in-the-loop `[FR-6]`

- Workflow reaches `PENDING_REVIEW`, records it in the audit log, then
  `await workflow.wait_condition(lambda: self._review_decision is not None or self._cancel_requested, timeout=SLA)`.
- Signals: `approve(reviewer, note)`, `reject(reviewer, reason)`, `cancel()`. A decision is
  only honoured while the gate is *armed* (status already `PENDING_REVIEW`), so a signal that
  races ahead of the run can't pre-approve a record no human has seen.
- No signal before SLA → `REVIEW_TIMED_OUT` terminal state (nothing persisted).
- The UI's pending list is built by the API listing *running* workflows of this type
  (bounded scan), calling each one's `state` query, and keeping the `PENDING_REVIEW` ones
  with their proposed record + reason. Approve → `signal("approve", …)` → the *same*
  workflow resumes.

---

## 6. Guardrails `[FR-5]`

1. **Input** — Pydantic parse/validate + size/shape caps, pre-LLM.
2. **Policy** — `check_policy` activity: deterministic banned-term/discriminatory-language
   regexes (5 BLOCK + 3 WARN patterns today — a stand-in for a curated policy pack) +
   salary-sanity (max < min, absurd magnitudes); a `BLOCK` severity forces human review
   regardless of confidence. Rules only, no LLM layer: one could add nuance later but must
   never downgrade a deterministic BLOCK (fail-closed).
3. **Output** — the final object must parse as `EnrichedPosting`. Bounded retries apply to
   the LLM's *decision* JSON (re-propose, then a deterministic fallback); a record that
   fails to assemble is a permanent error — non-retryable, terminal `FAILED_VALIDATION`,
   nothing persisted.

---

## 7. Eval harness `[FR-7]`

- **Gold set** (`eval/gold.py`): 18 raw postings, each with expected key fields + expected
  tool trajectory + difficulty tag — 13 that should auto-resolve (clean / remote / salary
  formats) and 5 that should route to a human (2 policy-block, 2 ambiguous, 1 near-duplicate).
  Deterministic — runs offline against the **fake LLM**.
- **Metrics** (`eval/metrics.py`): task-success (field-level match), tool-selection
  accuracy (trajectory set/ordered F1), output validity (parse rate), cost/run,
  LLM-judge quality (only when a judge is configured).
- **Gate** (`eval/gate.py`): one `THRESHOLDS` dict; fails on missing required metric or
  empty gold set (never vacuously green — the StreamRAG lesson).
- **Runner** (`eval/harness.py`) drives postings through the in-process agent runner — the
  *same* decision graph, tools, and `finalize.py` assembly/confidence/routing the durable
  workflow calls, but without Temporal. Deliberate split of concerns: the eval measures agent
  **quality**, and orchestration is covered separately by the time-skipping workflow tests
  (`tests/test_workflow.py`) and the chaos test. So the eval is not a proof that the workflow
  wiring is right — those tests are.
- **Stretch:** failed eval cases auto-mined into new gold cases.

---

## 8. Observability `[FR-8]`

- **Traces → Jaeger** (OTLP). Every span comes from Temporal's `TracingInterceptor` — there
  are no hand-rolled spans — so the tree is workflow/activity boundaries as Temporal names
  them: `RunWorkflow:JobIntakeWorkflow` → `StartActivity:<name>` / `RunActivity:<name>`, one
  pair per agent turn and per tool. Token counts and cost are *metrics* (below), not span
  attributes: `MeteredLLM` records them on every call, so per-run cost is read from
  Prometheus, and the trace is used for step structure and latency.
- **Metrics → Prometheus → Grafana:** `conductor.runs` (by *terminal* outcome, one increment
  per run), `conductor.submissions` (arrivals, counted separately so the outcome breakdown
  isn't double-counted), `conductor.tool.latency`, `conductor.llm.latency`,
  `conductor.llm.tokens`, `conductor.llm.cost_usd`. Pending-review depth is intentionally not
  a metric: `GET /pending` reads it from Temporal, the source of truth, and a per-process
  up/down counter would go negative when a worker dies with a run parked.
- Temporal interceptor emits spans for workflow/activity boundaries so traces and the
  Temporal UI line up. Telemetry is a safe no-op when disabled/unreachable `[FR-8]`.

---

## 9. Storage (app Postgres) `[FR-3]`

```
postings(external_id PK, record JSONB, status, confidence, created_at, updated_at)
audit_log(id, workflow_id, external_id, idempotency_key, seq, step, status, detail JSONB, ts)
          -- append-only; UNIQUE (workflow_id, idempotency_key) is the idempotency proof
posting_embeddings(external_id PK, title, embedding vector(384))          -- pgvector, dedup corpus
```

`pgvector` (not OpenSearch) for dedup: Postgres is already in the stack for audit/records,
so one datastore serves both — fewer moving parts, and the `vector` extension + an HNSW
cosine index (no training step, unlike IVFFlat) covers kNN over a small corpus. (StreamRAG
proved the OpenSearch path; here reuse-first + minimal-footprint says pgvector.) `[NFR-6]`

---

## 10. Failure-mode table (README-bound) `[FR-10]`

| Failure | Without design | Conductor's design |
|---|---|---|
| Worker crashes mid-run | Work lost / restarted from zero | Temporal replays history; resumes at last completed activity; no re-spend `[FR-4]` |
| LLM times out / flakes | Run hangs or 500s | Activity timeout + `RetryPolicy` with backoff; bounded attempts |
| Retry re-runs a write | Double audit rows / double record | Idempotency key + upsert; `UNIQUE (workflow_id, idempotency_key)` makes the re-insert a no-op |
| LLM emits invalid JSON | Half-written record downstream | Constrained JSON output; a bad decision is re-proposed then falls back to a deterministic choice; an unassemblable record is a permanent error → `FAILED_VALIDATION`, nothing persisted |
| Genuinely ambiguous posting | Silent wrong answer | Confidence gate → HITL signal wait → human decides |
| Policy violation slips through | Bad record shipped | `check_policy` BLOCK forces human review regardless of confidence |
| Reviewer never responds | Workflow stuck forever | SLA timeout → `REVIEW_TIMED_OUT` terminal state |
| Telemetry backend down | Service crashes on export | OTel wrapped; safe no-op on failure |
| Duplicate posting re-submitted | Corpus pollution | `search_duplicates` flags near-dupes; workflow_id keyed on external_id (dedupe of runs) |

---

## 11. Tech stack (versions confirmed in PLAN.md before coding)

Python 3.12 · `temporalio` (Temporal Python SDK) · `langgraph` + `langchain-core` +
`langchain-ollama` / `langchain-anthropic` · `pydantic` v2 + `pydantic-settings` ·
`psycopg[binary]` + `pgvector` · `sentence-transformers` bge-small (CPU) for real
embeddings; deterministic hash embedder for CI · `fastapi` + `uvicorn` · OTel SDK +
OTLP gRPC exporter · Next.js (App Router) + Tailwind · Docker Compose · GitHub Actions.

**Reused from the author's StreamRAG project:** the LLM adapter (`llm/base.py`,
`factory.py`, `ollama_client.py`, `anthropic_client.py`), `settings.py` pattern,
`obs/telemetry.py` pattern, the eval `gate.py`/`metrics.py` shape, the FastAPI+OTel wiring,
the Next.js UI scaffold, the CI structure. **Reused from the author's
`temporal-order-workflow` project:** the Compose Temporal stack, the worker bootstrap
(retrying connect), the workflow/activity/RetryPolicy idioms, and the time-skipping test
harness.

---

## 12. Repo layout

```
Conductor/
  docs/            REQUIREMENTS.md DESIGN.md PLAN.md DEMO.md METRICS.md
  python/
    conductor/
      settings.py            models.py            # config + Pydantic contracts
      llm/                   {base,factory,fake,metered,ollama_client,anthropic_client}.py
      embed/                 {base,fake,bge}.py    # pluggable embedder
      tools/                 {registry,location,salary,category,dedup,dedup_index,policy}.py
      agent/                 graph.py finalize.py runner.py   # decision graph · assembly+confidence · in-process driver
      temporal/              workflow.py activities.py worker.py client.py shared.py constants.py
      db/                    schema.sql pg.py store.py
      api/                   app.py                # FastAPI control plane
      eval/                  gold.py harness.py metrics.py gate.py judge.py __main__.py
      obs/                   telemetry.py
    tests/                   unit + time-skipping workflow tests + chaos test
    pyproject.toml
  ui/              Next.js App Router + Tailwind approval UI
  infra/           otel/ prometheus/ grafana/          # config only; images come from Compose
  docker-compose.yml   Makefile   .github/workflows/ci.yml   README.md
```
