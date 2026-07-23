"""Temporal durable layer: the workflow drives the agent loop and each LLM/tool call is an
idempotent, retryable activity, so a crash mid-run resumes from the last completed step."""
