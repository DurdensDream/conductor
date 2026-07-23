"""The typed tool set. Each tool is a pure function of (RawPosting, ToolContext) -> a
Pydantic output model. The agent picks a tool by *name* (reliable on small models); the
workflow constructs the call and runs it as an idempotent Temporal activity."""

from .registry import TOOLS, ToolContext, ToolSpec, run_tool

__all__ = ["TOOLS", "ToolContext", "ToolSpec", "run_tool"]
