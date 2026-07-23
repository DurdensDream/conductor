.PHONY: up down logs obs-up test lint eval chaos ui-build fmt venv

# ---- stack ----
up:            ## boot the base stack (Temporal + Postgres + worker + API + UI)
	docker compose up -d --build

obs-up:        ## boot everything incl. Jaeger + Prometheus + Grafana
	OTEL_ENABLED=true docker compose --profile obs up -d --build

down:          ## stop everything and remove volumes
	docker compose --profile obs down -v

logs:
	docker compose logs -f --tail 100

# ---- local dev (against `docker compose up -d temporal app-db`) ----
LOCAL_ENV = TEMPORAL_ADDRESS=localhost:7233 POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
            LLM_PROVIDER=fake EMBEDDER=fake OTEL_ENABLED=false

worker:
	cd python && $(LOCAL_ENV) python -m conductor.temporal.worker

api:
	cd python && $(LOCAL_ENV) uvicorn conductor.api.app:app --port 8000

# ---- quality ----
test:          ## unit + time-skipping workflow tests (hermetic)
	cd python && python -m pytest -q

lint:
	cd python && ruff check conductor tests

eval:          ## run the eval gate (fake provider)
	cd python && LLM_PROVIDER=fake EMBEDDER=fake python -m conductor.eval --gate

chaos:         ## the reliability proof — needs `docker compose up -d temporal app-db`
	cd python && CONDUCTOR_CHAOS=1 TEMPORAL_ADDRESS=localhost:7233 POSTGRES_HOST=localhost \
	  POSTGRES_PORT=5433 LLM_PROVIDER=fake EMBEDDER=fake python -m pytest tests/test_chaos.py -q -s

ui-build:
	cd ui && npm ci && npm run build
