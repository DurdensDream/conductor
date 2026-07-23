# Conductor — Design

Traces to `REQUIREMENTS.md`. Where a requirement is satisfied it is cited as `[FR-n]`.

---

## 1. The one decision everything hangs on: the Temporal ↔ LangGraph boundary

The brief says *"Temporal for the durable outer workflow + LangGraph for the inner
agentic steps."* There are three ways to wire that, and the choice determines whether
the chaos test is meaningful or theater.

| Option | What runs where | Durability granularity | Verdict |
|---|---|---|---|
| **A. Agent-in-one-activity** | The whole LangGraph agent runs inside a *single* Temporal activity | Whole agent = one unit. A crash re-runs the entire agent from scratch — re-pays every LLM + tool call. | ❌ "Durable" in name only; the resume re-does work and can double-spend. Chaos test would be theater. |
| **B. LangGraph graph run inside the workflow** | Graph execution happens in the workflow sandbox | N/A | ❌ Illegal — LangGraph does arbitrary non-deterministic Python; Temporal's workflow sandbox forbids that. |
| **C. Workflow drives the loop; each step is an activity** | Temporal workflow is the durable state machine; **each LLM inference and each tool call is its own activity**; LangGraph defines the decision graph + structured-output contract that the "decide" activity runs | Per-step. A crash resumes from the last *completed* activity; finished steps are never re-run. | ✅ **Chosen.** |

**Chosen: Option C.** Rationale:

- **Durability is real at the step level.** Temporal event-sources every activity
  result. If the worker dies after `classify_category` succeeded, the restarted worker
  replays history, sees that result, and continues to the next step — it does **not**
  re-call the LLM. That is the property the chaos test asserts `[FR-4]`, and it's only
  true under Option C.
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

**So what, concretely, does LangGraph do here?** It defines the agent as a small
`StateGraph`: a `plan` node (LLM proposes which enrichment tools are needed for *this*
posting), a `route` conditional edge, and a `finalize` node (LLM assembles the validated
`EnrichedPosting` via structured output). The graph is compiled and invoked **inside the
`agent_decide` activity** — one turn per call. The workflow calls `agent_decide`, gets
back either a tool-call request or a final record, executes any requested tool as its own
activity, feeds the observation back, and calls `agent_decide` again. This is a ReAct
loop whose *driver* is the durable workflow.

```
JobIntakeWorkflow (deterministic, durable)
  ├─ activity: validate_input            (guardrail, idempotent)
  ├─ loop, max N turns:
  │    ├─ activity: agent_decide         (LangGraph turn → next tool | done)  ← LLM
  │    └─ activity: run_tool[name]       (one of the 5 typed tools)           ← LLM/IO
  ├─ activity: check_policy              (guardrail)
  ├─ compute confidence (deterministic, in-workflow)
  ├─ if low-confidence OR policy-blocked:
  │    └─ wait_condition(approve|reject|cancel signal, SLA timeout)           ← HITL
  ├─ activity: validate_output           (guardrail, structured-output enforce)
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
AgentDecision     : action ∈ {call_tool, finish}, tool?, tool_args?, final?, reasoning
```

- `RawPosting` is the **input guardrail**: size caps, required-field coercion, and an
  injection-shape reject (control chars, absurd length) happen at parse time.
- `EnrichedPosting` is the **output guardrail**: the run cannot finish unless the final
  object parses. Structured-output enforcement (native `format=`/`with_structured_output`)
  makes the small local model comply; on repeated failure the run ends
  `FAILED_VALIDATION` — never a partial write `[FR-5]`.

Confidence is computed **deterministically in the workflow** from tool-level signals
(salary parsed? location resolved? category confidence? any duplicate above threshold?
policy severity?) — *not* asked of the LLM. A number the model invents isn't a
calibrated confidence; a formula over concrete signals is auditable and testable.

---

## 4. Durable execution model & idempotency `[FR-3, NFR-2]`

- **Activities** carry `start_to_close_timeout` + a `RetryPolicy`
  (`initial_interval`, `backoff`, `maximum_attempts`). LLM/tool activities are
  configured for transient-failure retry; validation activities are effectively
  non-retryable (a schema error won't fix itself).
- **Idempotency key** = `(workflow_id, step_name, attempt-invariant)`. The audit-log
  writer and the final `persist_record` upsert on this key, so a retried or replayed
  activity never double-writes. Proven by a **side-effect counter** the chaos test reads
  `[FR-4]`: completed steps must show exactly one effect after a crash+resume.
- **Determinism guards:** `workflow.now()` never `datetime.now()`; no `random`, no IO,
  no `import` of non-deterministic modules in the workflow module (guarded by
  `workflow.unsafe.imports_passed_through()` for the dataclasses it needs).
- **Compensation (MVP scope):** there are no irreversible external side effects in MVP,
  so "compensation" = marking the audit log `COMPENSATED`/`CANCELLED` and never emitting
  a final record. A `cancel` signal at any point unwinds cleanly to `CANCELLED`.

---

## 5. Human-in-the-loop `[FR-6]`

- Workflow reaches `PENDING_REVIEW`, records it in the audit log, then
  `await workflow.wait_condition(lambda: self._decision is not None, timeout=SLA)`.
- Signals: `approve(reviewer, note)`, `reject(reviewer, reason)`, `cancel()`.
- No signal before SLA → `REVIEW_TIMED_OUT` terminal state (nothing persisted).
- The UI's pending list is built by the API listing open workflows in the
  `PENDING_REVIEW` state and calling each one's `get_state` query for the proposed
  record + reasoning. Approve → `signal("approve", …)` → the *same* workflow resumes.

---

## 6. Guardrails `[FR-5]`

1. **Input** — Pydantic parse/validate + size/shape caps, pre-LLM.
2. **Policy** — `check_policy` activity: deterministic banned-term/discriminatory-language
   rules + salary-sanity (max < min, absurd magnitudes) + optional LLM judgment; a
   `BLOCK` severity forces human review regardless of confidence.
3. **Output** — enforced structured output; final must parse as `EnrichedPosting`;
   bounded retries then `FAILED_VALIDATION`.

---

## 7. Eval harness `[FR-7]`

- **Gold set** (`eval/gold.py`): ~20–30 raw postings, each with expected key fields +
  expected tool trajectory + difficulty tag (clean / messy / policy / duplicate /
  ambiguous). Deterministic — runs offline against the **fake LLM**.
- **Metrics** (`eval/metrics.py`): task-success (field-level match), tool-selection
  accuracy (trajectory set/ordered F1), output validity (parse rate), cost/run,
  LLM-judge quality (only when a judge is configured).
- **Gate** (`eval/gate.py`): one `THRESHOLDS` dict; fails on missing required metric or
  empty gold set (never vacuously green — the StreamRAG lesson).
- **Runner** drives postings through the **real Temporal workflow** in a time-skipping
  test environment, so evals exercise the actual orchestration, not a bypass.
- **Stretch:** failed eval cases auto-mined into new gold cases.

---

## 8. Observability `[FR-8]`

- **Traces → Jaeger** (OTLP). Span tree: `workflow` → `agent_turn.k` → `activity:<name>`.
  LLM spans carry GenAI attributes: `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`, `conductor.cost_usd`.
- **Metrics → Prometheus → Grafana:** `conductor.runs` (by outcome), `conductor.tool.latency`,
  `conductor.llm.tokens`, `conductor.llm.cost_usd`, `conductor.review.queue_depth`,
  `conductor.review.latency`.
- Temporal interceptor emits spans for workflow/activity boundaries so traces and the
  Temporal UI line up. Telemetry is a safe no-op when disabled/unreachable `[FR-8]`.

---

## 9. Storage (app Postgres) `[FR-3]`

```
postings(external_id PK, record JSONB, status, confidence, created_at, updated_at)
audit_log(id, workflow_id, external_id, step, status, detail JSONB, ts)   -- append-only
posting_embeddings(external_id PK, title, embedding vector(384))          -- pgvector, dedup corpus
side_effect_counter(workflow_id, step, n)                                 -- idempotency proof for chaos test
```

`pgvector` (not OpenSearch) for dedup: Postgres is already in the stack for audit/records,
so one datastore serves both — fewer moving parts, and the `vector` extension + an
IVFFlat/HNSW index covers kNN over a small corpus. (StreamRAG proved the OpenSearch path;
here reuse-first + minimal-footprint says pgvector.) `[NFR-6]`

---

## 10. Failure-mode table (README-bound) `[FR-10]`

| Failure | Without design | Conductor's design |
|---|---|---|
| Worker crashes mid-run | Work lost / restarted from zero | Temporal replays history; resumes at last completed activity; no re-spend `[FR-4]` |
| LLM times out / flakes | Run hangs or 500s | Activity timeout + `RetryPolicy` with backoff; bounded attempts |
| Retry re-runs a write | Double audit rows / double record | Idempotency key + upsert; side-effect counter proves single effect |
| LLM emits invalid JSON | Half-written record downstream | Enforced structured output; bounded retries → `FAILED_VALIDATION`, nothing persisted |
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

**Reused from StreamRAG** (`~40%`): the LLM adapter (`llm/base.py`, `factory.py`,
`ollama_client.py`, `anthropic_client.py`), `settings.py` pattern, `obs/telemetry.py`
pattern, the eval `gate.py`/`metrics.py` shape, the FastAPI+OTel wiring, the Next.js UI
scaffold, the CI structure. **Reused from the Temporal order-workflow take-home:** the
Compose Temporal stack, the worker bootstrap (`_with_retry` connect), the
workflow/activity/RetryPolicy idioms, and the time-skipping test harness.

---

## 12. Repo layout

```
Conductor/
  docs/            REQUIREMENTS.md DESIGN.md PLAN.md DEMO.md METRICS.md
  python/
    conductor/
      settings.py            models.py            # config + Pydantic contracts
      llm/                   {base,factory,fake,ollama_client,anthropic_client}.py
      embed/                 {base,fake,bge}.py    # pluggable embedder
      tools/                 {registry,location,salary,category,dedup,policy}.py
      agent/                 graph.py              # LangGraph decision graph
      temporal/              workflow.py activities.py worker.py client.py shared.py
      db/                    schema.sql pool.py audit.py
      api/                   app.py                # FastAPI control plane
      eval/                  gold.py harness.py metrics.py gate.py judge.py __main__.py
      obs/                   telemetry.py
    tests/                   unit + time-skipping workflow tests + chaos test
    pyproject.toml
  ui/              Next.js App Router + Tailwind approval UI
  infra/           postgres/ jaeger/ prometheus/ grafana/ otel/
  docker-compose.yml   Makefile   .github/workflows/ci.yml   README.md
```
