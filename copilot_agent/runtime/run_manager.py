from __future__ import annotations

from copilot_agent.runtime.execution_engine import (
    FINAL_STREAM_MARKER,
    ApprovalRequired,
    ExecutionEngine,
    ManagedRun,
)


class RunManager(ExecutionEngine):
    """Backward-compatible name for the local execution engine."""


__all__ = [
    "ApprovalRequired",
    "ExecutionEngine",
    "FINAL_STREAM_MARKER",
    "ManagedRun",
    "RunManager",
]
