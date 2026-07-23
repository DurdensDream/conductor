"""Fixed orchestration constants — safe to import inside the workflow sandbox (no env,
no IO). Runtime knobs that a caller might vary per run (max turns, review SLA) travel in
IntakeInput instead, so they're captured deterministically in workflow history."""

RETRY_INITIAL_INTERVAL_SECONDS = 1
RETRY_BACKOFF_COEFFICIENT = 2.0
RETRY_MAX_INTERVAL_SECONDS = 30
ACTIVITY_MAX_ATTEMPTS = 4
ACTIVITY_START_TO_CLOSE_SECONDS = 60
LLM_ACTIVITY_START_TO_CLOSE_SECONDS = 120

# A malformed payload / record won't fix itself — never retry these.
NON_RETRYABLE_ERRORS = ["ValidationError"]
