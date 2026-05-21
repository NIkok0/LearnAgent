#!/usr/bin/env python
"""Verify Human-in-the-loop checkpoint resume semantics."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.execution_engine import ExecutionEngine, GraphInterrupted  # noqa: E402


class ApprovalState(TypedDict):
    value: str


class HitlCheckpointRunner:
    def __init__(self, event_store: EventStore) -> None:
        self._events = event_store
        graph = StateGraph(ApprovalState)
        graph.add_node("approval", self._approval_node)
        graph.set_entry_point("approval")
        graph.add_edge("approval", END)
        self._graph = graph.compile(checkpointer=MemorySaver())

    def _approval_node(self, state: ApprovalState) -> ApprovalState:
        approved = interrupt(
            {
                "required": True,
                "reason": "dangerous_tool",
                "message": "approve dangerous operation?",
                "tool_calls": [{"id": "call-danger", "name": "http_post"}],
                "state_before_interrupt": dict(state),
            }
        )
        return {"value": "approved-from-checkpoint" if approved else "rejected-from-checkpoint"}

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        resume: bool | None = None,
    ):
        del messages, confirm_dangerous
        config = {"configurable": {"thread_id": conversation_id, "run_id": run_id}}
        graph_input = Command(resume=resume) if resume is not None else {"value": "start"}
        final_value = ""
        async for event in self._graph.astream_events(graph_input, config=config, version="v2"):
            payload = _interrupt_payload(event)
            if payload is not None:
                yield self._emit(conversation_id, str(run_id), "approval_required", payload)
                raise GraphInterrupted(payload)
            output = (event.get("data") or {}).get("output")
            if isinstance(output, dict) and isinstance(output.get("value"), str):
                final_value = output["value"]
        if final_value:
            yield self._emit(conversation_id, str(run_id), "token", {"text": final_value})
        yield self._emit(conversation_id, str(run_id), "done", {"assistant_message": {"content": final_value}})

    def finalize_memory(self, *_args, **_kwargs) -> None:
        return None

    def _emit(self, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> str:
        self._events.append_event(thread_id, run_id, event_type, payload)
        return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _interrupt_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    chunk = (event.get("data") or {}).get("chunk")
    if not isinstance(chunk, dict) or "__interrupt__" not in chunk:
        return None
    interrupts = chunk.get("__interrupt__") or []
    first = interrupts[0] if isinstance(interrupts, (list, tuple)) and interrupts else None
    value = getattr(first, "value", None)
    return value if isinstance(value, dict) else {"required": True}


async def _wait_for_status(store: EventStore, run_id: str, statuses: set[str], timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = store.get_run(run_id) or {}
        if str(run.get("status", "")) in statuses:
            return run
        await asyncio.sleep(0.05)
    return store.get_run(run_id) or {}


async def _shutdown(engine: ExecutionEngine) -> None:
    async with engine._lock:  # noqa: SLF001
        tasks = [managed.task for managed in engine._runs.values() if managed.task and not managed.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def verify(event_store_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    runner = HitlCheckpointRunner(store)
    engine = ExecutionEngine(event_store=store, runner=runner)  # type: ignore[arg-type]
    engines = [engine]
    try:
        approved = await engine.create_run(
            thread_id=f"{thread_prefix}-approve",
            messages=[{"role": "user", "content": "approve"}],
        )
        approved_waiting = await _wait_for_status(store, approved.run_id, {"waiting_approval"})
        await engine.approve(approved.run_id)
        approved_done = await _wait_for_status(store, approved.run_id, {"completed"})
        approved_events = store.list_run_events(approved.run_id)

        rejected = await engine.create_run(
            thread_id=f"{thread_prefix}-reject",
            messages=[{"role": "user", "content": "reject"}],
        )
        rejected_waiting = await _wait_for_status(store, rejected.run_id, {"waiting_approval"})
        await engine.reject(rejected.run_id)
        rejected_done = await _wait_for_status(store, rejected.run_id, {"completed"})
        rejected_events = store.list_run_events(rejected.run_id)

        rehydrate_path = event_store_path.with_name(f"{event_store_path.stem}-rehydrate.sqlite")
        rehydrate_store = EventStore(str(rehydrate_path))
        rehydrate_runner = HitlCheckpointRunner(rehydrate_store)
        first_engine = ExecutionEngine(event_store=rehydrate_store, runner=rehydrate_runner)  # type: ignore[arg-type]
        engines.append(first_engine)
        rehydrated_run = await first_engine.create_run(
            thread_id=f"{thread_prefix}-rehydrate",
            messages=[{"role": "user", "content": "rehydrate"}],
        )
        await _wait_for_status(rehydrate_store, rehydrated_run.run_id, {"waiting_approval"})
        restarted_engine = ExecutionEngine(event_store=rehydrate_store, runner=rehydrate_runner)  # type: ignore[arg-type]
        engines.append(restarted_engine)
        await restarted_engine.approve(rehydrated_run.run_id)
        rehydrated_done = await _wait_for_status(rehydrate_store, rehydrated_run.run_id, {"completed"})
        rehydrated_events = rehydrate_store.list_run_events(rehydrated_run.run_id)

        return {
            "approved": _summarize(approved_waiting, approved_done, approved_events),
            "rejected": _summarize(rejected_waiting, rejected_done, rejected_events),
            "rehydrated": _summarize({"status": "waiting_approval"}, rehydrated_done, rehydrated_events),
        }
    finally:
        for item in engines:
            await _shutdown(item)


def _summarize(waiting: dict[str, Any], done: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    event_types = [event["type"] for event in events]
    checkpoint_meta = next((event.get("payload") or {} for event in events if event.get("type") == "run_checkpoint_meta"), {})
    resolved = next((event.get("payload") or {} for event in events if event.get("type") == "approval_resolved"), {})
    resumed_starts = [
        event.get("payload") or {}
        for event in events
        if event.get("type") == "run_started" and (event.get("payload") or {}).get("resume_from_checkpoint")
    ]
    token_text = "".join(str((event.get("payload") or {}).get("text") or "") for event in events if event.get("type") == "token")
    return {
        "waiting_status": waiting.get("status"),
        "final_status": done.get("status"),
        "event_types": event_types,
        "checkpoint_meta": checkpoint_meta,
        "approval_resolved": resolved,
        "resumed_starts": resumed_starts,
        "token_text": token_text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify HITL checkpoint resume.")
    parser.add_argument("--event-store-path", default="storage/verify-hitl-checkpoint-resume.sqlite")
    parser.add_argument("--thread-prefix", default=f"hitl-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/hitl-checkpoint-resume-summary.json"))
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, args.thread_prefix))
    checks = {
        "approve_waits": summary["approved"]["waiting_status"] == "waiting_approval",
        "approve_completes": summary["approved"]["final_status"] == "completed",
        "approve_resume_value": summary["approved"]["approval_resolved"].get("resume_value") is True,
        "approve_from_checkpoint": bool(summary["approved"]["resumed_starts"]),
        "approve_token": "approved-from-checkpoint" in summary["approved"]["token_text"],
        "reject_waits": summary["rejected"]["waiting_status"] == "waiting_approval",
        "reject_completes": summary["rejected"]["final_status"] == "completed",
        "reject_resume_value": summary["rejected"]["approval_resolved"].get("resume_value") is False,
        "reject_token": "rejected-from-checkpoint" in summary["rejected"]["token_text"],
        "checkpoint_meta_present": bool(summary["approved"]["checkpoint_meta"].get("resume_supported")),
        "checkpoint_interrupt_summary": bool(
            (summary["approved"]["checkpoint_meta"].get("interrupt_summary") or {}).get("tool_calls")
        ),
        "rehydrate_completes": summary["rehydrated"]["final_status"] == "completed",
        "rehydrate_from_checkpoint": bool(summary["rehydrated"]["resumed_starts"]),
        "rehydrate_token": "approved-from-checkpoint" in summary["rehydrated"]["token_text"],
    }
    summary["checks"] = checks
    summary["hitl_checkpoint_resume"] = "PASS" if all(checks.values()) else "FAIL"
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"hitl_checkpoint_resume={summary['hitl_checkpoint_resume']}")
    return 0 if summary["hitl_checkpoint_resume"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
