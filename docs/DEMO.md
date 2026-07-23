# Demo — 90 seconds

## 0. Boot everything
```bash
cp .env.example .env
make obs-up            # or: docker compose --profile obs up -d --build
```
Wait until `curl -s localhost:8000/health` returns `{"status":"ok"}`.

Endpoints:
- Approval UI — http://localhost:3000
- Control API docs — http://localhost:8000/docs
- Temporal Web UI — http://localhost:8080
- Jaeger (traces) — http://localhost:16686
- Grafana (dashboards, anon admin) — http://localhost:3001
- Prometheus — http://localhost:9090

## 1. Auto-resolved posting (high confidence)
```bash
curl -s -X POST localhost:8000/postings -H 'content-type: application/json' -d '{
  "external_id":"demo-1","title":"Senior Python Engineer",
  "description":"Backend Kafka Postgres, hybrid.","location_raw":"Bangalore, India",
  "salary_raw":"$120k-$150k/yr"}'
curl -s localhost:8000/postings/demo-1     # -> status COMPLETED, confidence ~0.96
```

## 2. Human-in-the-loop (policy block → approval UI)
```bash
curl -s -X POST localhost:8000/postings -H 'content-type: application/json' -d '{
  "external_id":"demo-2","title":"Warehouse Packer - men only",
  "description":"No women. Forklift required.","location_raw":"Austin","salary_raw":"$18/hr"}'
```
Open http://localhost:3000 → the posting is **Pending review** with reason `policy_block`.
Click **Approve** (or `POST /postings/demo-2/approve`) → the same workflow resumes to COMPLETED.
(Or use the UI's "Discriminatory" / "Ambiguous" sample buttons.)

## 3. The reliability proof (crash & resume)
```bash
# Submit a review-bound posting, then hard-kill the worker while it's parked:
docker compose kill -s SIGKILL worker
# The run's state is safe in Temporal. Bring a fresh worker back:
docker compose up -d worker
# Approve in the UI — it resumes on the NEW worker with identical output.
```
Automated version (asserts identical output + no duplicate side effects):
```bash
docker compose up -d temporal app-db
make chaos
```

## 4. Traces & dashboards
- **Jaeger** → service `conductor-worker` → newest `RunWorkflow` trace: per-activity spans
  (`agent_decide`, `run_tool`, `check_policy`, `finalize_record`, `persist_record`) with
  LLM token/cost attributes.
- **Grafana** → "Conductor — Agent Runs & Cost": runs by outcome, tool latency p95, tokens, cost.
- **Temporal UI** → the workflow's full event history (every activity, signal, timer).

## Run it with a real model
```bash
# Local (Ollama on the host):
LLM_PROVIDER=ollama OLLAMA_MODEL=qwen3.5:9b docker compose up -d --build
# Cloud quality:
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... docker compose up -d --build
```
