# Conductor — Requirements

> A **durable, observable, tool-calling AI agent** that automates a real recurring
> workflow (job-posting intake & enrichment). It plans, calls typed tools, enforces
> guardrails, pauses for human approval on low-confidence cases, **survives a crash
> mid-run and resumes exactly where it left off**, and proves its quality with a
> trajectory + task-success eval harness that gates CI.

This document is the contract. Design (`DESIGN.md`) and the implementation plan
(`PLAN.md`) must trace back to a requirement here. Each requirement has an ID and a
falsifiable acceptance criterion — "done" means the criterion is demonstrated with
fresh evidence, not asserted.

---

## 1. Problem & goal

Most "AI agents" are single-turn wrappers that silently fail. A production agent is a
distributed system: it needs durable state, retries, idempotency, compensation,
approval gates, guardrails, tracing, and evaluation. Conductor builds one the right way
and *proves* reliability by killing its worker and watching a fresh process rebuild the
run from event history and finish it without duplicating a single side effect.

**Primary goal:** an agent that takes a raw, messy job posting and produces a clean,
validated, enriched record — routing genuinely-uncertain cases to a human — while being
durable, observable, and continuously evaluated.

**Quantified outcome to publish** (README headline): the auto-resolution rate on the gold
set, and the crash-and-resume result. As shipped that is *"72% of gold postings (13 / 18)
auto-resolve with no human touch, the other 5 route correctly; the injected `SIGKILL`
loses no run and duplicates no side effect."* A "minutes of triage saved" figure was
considered and dropped — nothing in this repo measures human handling time, so it would
be invented.

---

## 2. Domain (fixed for MVP, kept swappable)

**Job-posting intake & enrichment** — a domain the author has worked in production
(Kafka/Debezium/OpenSearch/Postgres job pipelines), so the messiness of the inputs and the
cost of a wrong auto-ship are real rather than invented.

Input: one raw posting (free-text title/description + loosely-typed fields: location
string, salary string, employment type, company, source).
Output: a clean `EnrichedPosting` — normalized location, parsed salary range + period +
currency, an O\*NET-style category, a remote/hybrid/onsite flag, a dedupe verdict
(is this a near-duplicate of an existing posting?), policy flags, and a confidence
score — plus an append-only audit log of every step.

Swappability is a *design* property, not an MVP feature: the tool set, the Pydantic
contracts, and the gold eval set are the only domain-specific parts; the durable orchestration, HITL, guardrail, eval, and observability machinery are
domain-agnostic.

---

## 3. Functional requirements

### FR-1 — Typed tools (3–5, Pydantic contracts)
The agent has a registry of typed tools, each with a Pydantic input and output schema
and a docstring the LLM sees. MVP tool set (5):
1. `normalize_location(raw) -> Location` — parse a location string into
   city/state/country/lat-lon-less structured form + a `remote_type` flag.
2. `parse_salary(raw) -> SalaryRange | None` — parse "$80k–$100k/yr", "45000 EUR", etc.
   into min/max/currency/period; deterministic regex only (no LLM), and an unparseable
   string returns `parsed=False` rather than a guess.
3. `classify_category(title, description) -> Category` — map to an O\*NET-style
   taxonomy code + label; LLM structured output.
4. `search_duplicates(text) -> list[DuplicateHit]` — embed the posting and vector-search
   existing postings (pgvector) for near-duplicates above a similarity threshold.
5. `check_policy(posting) -> PolicyVerdict` — flag discriminatory language, salary
   sanity violations, and banned terms (deterministic rules only; an LLM layer could add
   nuance later but must never downgrade a rule-based BLOCK — fail-closed).

**AC-1:** each tool has a Pydantic input+output model; calling a tool with malformed
input raises a typed validation error (never a raw 500); a unit test exercises each
tool's happy path and one invalid input.

### FR-2 — Agentic decision loop (LangGraph)
An LLM-driven agent decides *which* tools to call and *in what order* for a given
posting, observes each result, and produces a final structured `EnrichedPosting`.
Implemented as a LangGraph graph that decides **one turn at a time** (propose → validate →
repair), whose tool execution and LLM inference are delegated to the durable layer, which
drives the act → observe loop across turns (see FR-3).

**AC-2:** given a posting missing salary + location, the agent calls the corresponding
tools and returns a valid `EnrichedPosting`; the *trajectory* (ordered list of tool
names) is recorded and queryable.

### FR-3 — Durable orchestration (Temporal)
A Temporal workflow owns the run. **Every LLM inference and every tool call is a
separate Temporal activity** with a retry policy, a timeout, and an idempotency key.
Workflow state (trajectory so far, partial record, confidence, status) survives worker
restarts because Temporal event-sources it.

**AC-3:** the workflow exposes a `query` returning current status + trajectory + partial
record, and `signal`s for approve/reject/cancel. Activities have explicit
`start_to_close_timeout` and `RetryPolicy`. A flaky activity (fails twice, succeeds on
the third attempt) still completes the run.

### FR-4 — Chaos / reliability proof (the wow moment)
A test starts a run, lets it complete every enrichment activity and park in the review
gate, **kills the worker process**, restarts a fresh worker, and asserts the workflow
**rebuilds the whole run from Temporal event history** — trajectory and proposed record, in
a process sharing no memory with the dead one — and **resumes to completion without
duplicating a side effect**.

**AC-4:** this test passes in CI against a real Temporal server (not only the
time-skipping mock), the worker is killed via `SIGKILL` (not a graceful shutdown), and the
assertions cover both recovery (same trajectory, confidence and category after the crash)
and non-duplication (every audit `idempotency_key` unique under the DB `UNIQUE` constraint,
every pre-crash row still present, exactly one `persist_record`). Note what is *not*
claimed: nothing counts LLM calls across the kill, so "completed activities did not
re-execute" is a property of Temporal's replay semantics rather than a number this test
measures.

### FR-5 — Guardrails
- **Input guardrail:** raw input validated/coerced by Pydantic; unparseable or abusive
  input (oversized, injection-shaped) is rejected with a typed error before any LLM call.
- **Output guardrail:** the final record must parse as `EnrichedPosting`. Bounded retries
  apply to the LLM's decision JSON; a record that cannot be assembled is a permanent
  (non-retryable) error and ends the run in a terminal `FAILED_VALIDATION` state, never a
  half-record.
- **Policy check:** a dedicated step flags policy issues; a policy *block* forces the
  human-review route regardless of confidence.

**AC-5:** an oversized/garbage input is rejected pre-LLM; a crafted posting with a banned
term is forced to human review; an input that cannot yield a valid record ends
`FAILED_VALIDATION` with the audit log showing the rejection and nothing persisted.

### FR-6 — Human-in-the-loop (HITL)
Low-confidence (or policy-blocked) items pause via a Temporal signal wait with an SLA
timeout. A Next.js approval UI lists pending items with the agent's proposed record +
reasoning; approve resumes and persists, reject records the decision and ends the run,
and no action before the SLA routes to a `REVIEW_TIMED_OUT` terminal state.

**AC-6:** submitting a deliberately-ambiguous posting surfaces it in the UI's pending
list; clicking approve resumes the *same* workflow to completion; the decision +
reviewer + timestamp land in the audit log.

### FR-7 — Eval harness (gates CI)
A gold set of raw postings with expected enriched fields **and** expected tool
trajectories. Metrics computed per run and aggregated:
- **task success rate** — key output fields match expected (field-level scoring).
- **tool-selection accuracy** — trajectory vs expected (ordered/set F1).
- **output validity** — fraction of runs producing a schema-valid record.
- **cost/run** — tokens × price (0 for local), surfaced even when free.
- **LLM-as-judge quality** — a judge model scores enrichment quality (optional, gated
  only when a judge is configured).

Thresholds live in one `THRESHOLDS` dict (single source of truth, published in README);
the gate fails on missing required metrics or an empty gold set (never vacuously green).

**AC-7:** `python -m conductor.eval --gate` runs the gold set through the in-process agent
runner (same graph, tools and finalize logic the workflow calls, with the deterministic
LLM), prints metrics, and exits non-zero below thresholds; CI runs it and blocks merge on
failure. Orchestration correctness is covered by the time-skipping workflow tests and the
chaos test, not by the eval.

### FR-8 — Observability
- OpenTelemetry traces: one span per workflow and per activity (tool/LLM), emitted by
  Temporal's own `TracingInterceptor` → **Jaeger**. Traces carry step structure and
  latency; token counts and cost are *metrics*, not span attributes.
- Metrics: submissions, runs by terminal outcome, per-tool latency, LLM latency, tokens,
  cost → Prometheus → **Grafana** dashboard. Every instrument must have a caller; pending-
  review depth is read from Temporal (`GET /pending`), not published as a counter.
- Temporal Web UI shows the durable event history for any run.

**AC-8:** after a demo run, Jaeger shows the full per-step trace; Grafana shows
terminal-outcome, latency, token and cost panels; Temporal UI shows the history.
Telemetry degrades to a safe no-op when disabled/unreachable (never crashes a run).

### FR-9 — One-command boot
`docker compose up` boots Temporal + Temporal UI + both Postgres instances + worker +
API + approval UI. Observability (Jaeger/Prometheus/Grafana/collector) is behind a
`--profile obs` so the base stack stays light.

**AC-9:** a clean `docker compose up` on a fresh checkout reaches a state where a posting
can be submitted via the API and driven to completion, with no manual steps beyond env
defaults.

### FR-10 — Documentation
README with: one-line what + a link to the 90-second demo script; architecture diagram; design decisions ("why
durable execution", "why the workflow drives the agent loop", "why pgvector"); the
**failure-mode table**; the chaos-proof description; published metrics; one-command run;
"what I'd do next." Plus `DESIGN.md`, `PLAN.md`, `docs/DEMO.md`, `docs/METRICS.md`.

**AC-10:** README failure-mode table lists ≥6 concrete failure modes and how the design
survives each; every metric in the README matches `gate.py`.

---

## 4. Non-functional requirements

- **NFR-1 Determinism:** the Temporal workflow body is deterministic — all non-determinism
  (LLM, IO, time, randomness) lives in activities or uses `workflow.now()`.
- **NFR-2 Idempotency:** every side-effecting activity is idempotent under retry, keyed by
  (run_id, step). Re-running an activity must not double-write the audit log or double-count.
- **NFR-3 Hermetic tests/CI:** the full test suite and eval gate run offline with a
  **deterministic fake LLM + fake embedder** — no model download, no network, no GPU.
- **NFR-4 Pluggable LLM:** provider is swappable (fake | ollama | anthropic) via config
  with zero code change; model aliases are bare (`claude-haiku-4-5`), never date-pinned.
- **NFR-5 Safety of demo:** default local models (Ollama), no secrets required to run the
  base demo; API keys only to opt into a cloud provider.
- **NFR-6 Resource budget:** runs on a 16 GB Apple-Silicon Mac under Docker Desktop; no JVM
  in the stack; CPU-only torch; base stack (no obs) fits comfortably in 8 GB.
- **NFR-7 Reproducibility:** SHA-pinned or minor-pinned images; `uv`/pip lockable Python;
  `package-lock.json` + `npm ci` for the UI.

---

## 5. Explicit non-goals (MVP)

- Multi-agent supervisor/worker topology (stretch only).
- Full SAGA compensation across external systems (we do *audit-log* compensation +
  cancel/reject; no real external side effects to roll back in MVP).
- Auth/multi-tenant UI (single-user local demo).
- Real O\*NET model / real ATS integration (a small taxonomy + a simulated "external
  API" activity stand in; the seam is real, the backend is simulated).
- Production deploy (stretch); MVP target is a flawless local `docker compose up`.

---

## 6. Definition of done (rolls up the ACs)

`docker compose up` boots the stack · the agent completes intake on real inputs ·
`SIGKILL`ing the worker leaves the run recoverable from event history and duplicates no
side effect (CI-asserted) · low-confidence items route to the approval UI and resume on
approval · the eval harness runs in CI and gates merges · Jaeger shows per-step traces ·
Grafana shows run/token/cost dashboards · README documents ≥6 failure modes + a quantified
outcome · a 60–90s demo script (`DEMO.md`) walks the crash-and-resume.
