# Conductor Python image: worker + control-plane API. Default install is light (no torch)
# — the fake embedder needs none, and the demo bge embedder is an opt-in extra installed
# via the pytorch CPU index so we never drag in the multi-GB CUDA build.
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; psycopg[binary] ships its own libpq.
COPY pyproject.toml ./
COPY conductor ./conductor
RUN pip install --no-cache-dir .

# Default to the hermetic providers; compose/env overrides for a real demo.
ENV LLM_PROVIDER=fake \
    EMBEDDER=fake \
    PYTHONUNBUFFERED=1

# Overridden per-service in compose (worker vs api).
CMD ["python", "-m", "conductor.temporal.worker"]
