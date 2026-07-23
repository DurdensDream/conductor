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
and *proves* reliability by killing it mid-run and watching it recover with identical
output.

**Primary goal:** an agent that takes a raw, messy job posting and produces a clean,
validated, enriched record — routing genuinely-uncertain cases to a human — while being
durable, observable, and continuously evaluated.

**Quantified outcome to publish** (README headline): *"X% of postings auto-resolved
end-to-end without human touch; ~Y minutes of manual triage saved per 100 postings;
0 lost/duplicated work across N injected worker crashes."*

---

## 2. Domain (fixed for MVP, kept swappable)

**Job-posting intake & enrichment** — authentic to the author's day job (Joveo:
Kafka/Debezium/OpenSearch/Postgres job pipelines), so every layer is defensible in an
interview.

Input: one raw posting (free-text title/description + loosely-typed fields: location
string, salary string, employment type, company, source).
Output: a clean `EnrichedPosting` — normalized location, parsed salary range + period +
currency, an O\*NET-style category, a remote/hybrid/onsite flag, a dedupe verdict
(is this a near-duplicate of an existing posting?), policy flags, and a confidence
score — plus an append-only audit log of every step.

Swappability is a *design* property (see DESIGN §"Domain seam"), not an MVP feature: the
tool set, the Pydantic contracts, and the gold eval set are the only domain-specific
parts; the durable orchestration, HITL, guardrail, eval, and observability machinery are
domain-agnostic.

---

## 3. Functional requirements

### FR-1 — Typed tools (3–5, Pydantic contracts)
The agent has a registry of typed tools, each with a Pydantic input and output schema
and a docstring the LLM sees. MVP tool set (5):
1. `normalize_location(raw) -> Location` — parse a location string into
   city/state/country/lat-lon-less structured form + a `remote_type` flag.
2. `parse_salary(raw) -> SalaryRange | None` — parse "$80k–$100k/yr", "45000 EUR", etc.
   into min/max/currency/period; deterministic first, LLM fallback.
3. `classify_category(title, description) -> Category` — map to an O\*NET-style
   taxonomy code + label; LLM structured output.
4. `search_duplicates(text) -> list[DuplicateHit]` — embed the posting and vector-search
   existing postings (pgvector) for near-duplicates above a similarity threshold.
5. `check_policy(posting) -> PolicyVerdict` — flag discriminatory language, salary
   sanity violations, and banned terms (deterministic rules + optional LLM judgment).

**AC-1:** each tool has a Pydantic input+output model; calling a tool with malformed
input raises a typed validation error (never a raw 500); a unit test exercises each
tool's happy path and one invalid input.

### FR-2 — Agentic decision loop (LangGraph)
An LLM-driven agent decides *which* tools to call and *in what order* for a given
posting, observes each result, and produces a final structured `EnrichedPosting`.
Implemented as a LangGraph graph (plan → act → observe → finalize) whose tool execution
and LLM inference are delegated to the durable layer (see FR-3).

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
A test starts a run, lets it complete some activities, **kills the worker process
mid-run**, restarts a fresh worker, and asserts the workflow **resumes and completes with
byte-identical output** — and that already-completed activities did **not** re-execute
(proven via a persisted side-effect counter, not just equal output).

**AC-4:** this test passes in CI against a real Temporal server (not only the
time-skipping mock), the worker is killed via `SIGKILL` (not a graceful shutdown), and
the assertion includes both "output identical" and "no duplicate side effects."

### FR-5 — Guardrails
- **Input guardrail:** raw input validated/coerced by Pydantic; unparseable or abusive
  input (oversized, injection-shaped) is rejected with a typed error before any LLM call.
- **Output guardrail:** the final record must parse as `EnrichedPosting` (enforced
  structured output); a run that can't produce a valid record after N attempts ends in a
  terminal `FAILED_VALIDATION` state, never emits a half-record.
- **Policy check:** a dedicated step flags policy issues; a policy *block* forces the
  human-review route regardless of confidence.

**AC-5:** an oversized/garbage input is rejected pre-LLM; a crafted posting with a banned
term is forced to human review; a forced-invalid LLM output ends `FAILED_VALIDATION`
with the audit log showing the retries.

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

**AC-7:** `python -m conductor.eval --gate` runs the gold set through the real workflow
(with the deterministic LLM), prints metrics, and exits non-zero below thresholds; CI
runs it and blocks merge on failure.

### FR-8 — Observability
- OpenTelemetry traces: one span per workflow, per agent turn, per activity (tool/LLM),
  with token counts + cost as span attributes (GenAI semantic conventions) → **Jaeger**.
- Metrics: runs by outcome, per-tool latency, tokens, cost, HITL queue depth, review
  latency → Prometheus → **Grafana** dashboard.
- Temporal Web UI shows the durable event history for any run.

**AC-8:** after a demo run, Jaeger shows the full per-step trace with token/cost
attributes; Grafana shows run-outcome + cost panels; Temporal UI shows the history.
Telemetry degrades to a safe no-op when disabled/unreachable (never crashes a run).

### FR-9 — One-command boot
`docker compose up` boots Temporal + Temporal UI + both Postgres instances + worker +
API + approval UI. Observability (Jaeger/Prometheus/Grafana/collector) is behind a
`--profile obs` so the base stack stays light.

**AC-9:** a clean `docker compose up` on a fresh checkout reaches a state where a posting
can be submitted via the API and driven to completion, with no manual steps beyond env
defaults.

### FR-10 — Documentation
README with: one-line what + demo GIF; architecture diagram; design decisions ("why
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
- **NFR-6 Resource budget:** runs on a 16 GB Apple-Silicon Mac under Docker Desktop; JVM
  heaps pinned; CPU-only torch; base stack (no obs) fits comfortably in 8 GB.
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
killing the worker mid-run resumes with identical output and no duplicate side effects
(CI-asserted) · low-confidence items route to the approval UI and resume on approval ·
the eval harness runs in CI and gates merges · Jaeger shows per-step traces with cost ·
Grafana shows run/cost dashboards · README documents ≥6 failure modes + a quantified
outcome · a 60–90s demo (GIF/script) shows the crash-and-resume.
