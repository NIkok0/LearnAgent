from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from copilot_agent.runtime.event_store import (
    EventStore,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_CANCELLING,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    TERMINAL_RUN_STATUSES,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from copilot_agent.agent.runner import ChatRunner


FINAL_STREAM_MARKER = object()


@dataclass
class ManagedRun:
    run_id: str
    thread_id: str
    messages: list[dict[str, Any]]
    confirm_dangerous: bool = False
    stream: bool = False
    stream_queue: asyncio.Queue[str | object] | None = None
    task: asyncio.Task[None] | None = None
    approval_event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    rejected: bool = False
    cancel_requested: bool = False


class ApprovalRequired(Exception):
    pass


class ExecutionEngine:
    """Local runtime engine for background runs.

    This is intentionally still an in-process implementation. It owns the
    stable API boundary between FastAPI handlers, EventStore, and ChatRunner.
    """

    def __init__(self, *, event_store: EventStore, runner: "ChatRunner") -> None:
        self._events = event_store
        self._runner = runner
        self._runs: dict[str, ManagedRun] = {}
        self._lock = asyncio.Lock()

    async def create_run(
        self,
        *,
        thread_id: str,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool = False,
        stream: bool = False,
    ) -> ManagedRun:
        run = self._events.create_run(thread_id, status=RUN_STATUS_QUEUED)
        managed = ManagedRun(
            run_id=str(run["id"]),
            thread_id=thread_id,
            messages=messages,
            confirm_dangerous=confirm_dangerous,
            stream=stream,
            stream_queue=asyncio.Queue() if stream else None,
        )
        if confirm_dangerous:
            managed.approved = True
            managed.approval_event.set()
        self._events.append_event(thread_id, managed.run_id, "run_created", {"status": RUN_STATUS_QUEUED})
        async with self._lock:
            self._runs[managed.run_id] = managed
            managed.task = asyncio.create_task(self._execute(managed))
        return managed

    async def cancel(self, run_id: str) -> dict[str, Any]:
        managed = await self._get_managed(run_id)
        current = self._events.get_run(run_id)
        if current is None:
            return {}
        if str(current.get("status", "")) in TERMINAL_RUN_STATUSES:
            return current
        self._events.update_run_status(run_id, RUN_STATUS_CANCELLING)
        self._events.append_event(managed.thread_id, run_id, "cancel_requested", {})
        managed.cancel_requested = True
        managed.approval_event.set()
        if managed.task is not None:
            managed.task.cancel()
        return self._events.get_run(run_id) or {}

    async def approve(self, run_id: str) -> dict[str, Any]:
        managed = await self._get_managed(run_id)
        current = self._events.get_run(run_id)
        if current is None:
            return {}
        if str(current.get("status", "")) == RUN_STATUS_WAITING_APPROVAL:
            managed.approved = True
            self._events.append_event(managed.thread_id, run_id, "approval_resolved", {"approved": True})
            managed.approval_event.set()
        return self._events.get_run(run_id) or {}

    async def reject(self, run_id: str) -> dict[str, Any]:
        managed = await self._get_managed(run_id)
        current = self._events.get_run(run_id)
        if current is None:
            return {}
        if str(current.get("status", "")) == RUN_STATUS_WAITING_APPROVAL:
            managed.rejected = True
            self._events.append_event(managed.thread_id, run_id, "approval_resolved", {"approved": False})
            managed.approval_event.set()
        return self._events.get_run(run_id) or {}

    async def stream(self, run_id: str) -> "AsyncIterator[str]":
        managed = await self._get_managed(run_id)
        if managed.stream_queue is None:
            raise ValueError("run was not created with streaming enabled")
        while True:
            item = await managed.stream_queue.get()
            if item is FINAL_STREAM_MARKER:
                break
            yield str(item)

    async def _get_managed(self, run_id: str) -> ManagedRun:
        async with self._lock:
            managed = self._runs.get(run_id)
        if managed is None:
            raise KeyError(run_id)
        return managed

    async def _execute(self, managed: ManagedRun) -> None:
        run_id = managed.run_id
        thread_id = managed.thread_id
        try:
            self._events.update_run_status(run_id, RUN_STATUS_RUNNING)
            self._events.append_event(thread_id, run_id, "run_started", {"status": RUN_STATUS_RUNNING})
            await self._run_once(managed, confirm_dangerous=managed.confirm_dangerous or managed.approved)
            self._events.complete_run(run_id)
        except ApprovalRequired:
            if managed.cancel_requested:
                self._mark_cancelled(managed)
                return
            self._events.update_run_status(run_id, RUN_STATUS_WAITING_APPROVAL)
            await managed.approval_event.wait()
            if managed.cancel_requested:
                self._mark_cancelled(managed)
                return
            if managed.rejected:
                await self._emit(managed, "token", {"text": "Dangerous tool call was rejected by the user."})
                await self._emit(managed, "done", {})
                self._events.complete_run(run_id)
                return
            self._events.update_run_status(run_id, RUN_STATUS_RUNNING)
            self._events.append_event(thread_id, run_id, "run_started", {"status": RUN_STATUS_RUNNING, "resumed": True})
            await self._run_once(managed, confirm_dangerous=True)
            self._events.complete_run(run_id)
        except asyncio.CancelledError:
            self._mark_cancelled(managed)
        except Exception as e:
            await self._emit(managed, "error", {"error": str(e)})
            self._events.complete_run(run_id, error=str(e))
        finally:
            if managed.stream_queue is not None:
                await managed.stream_queue.put(FINAL_STREAM_MARKER)
            if self._is_terminal(run_id):
                self._finalize_memory(managed)
                async with self._lock:
                    self._runs.pop(run_id, None)

    async def _run_once(self, managed: ManagedRun, *, confirm_dangerous: bool) -> None:
        async for chunk in self._runner.run_stream(
            conversation_id=managed.thread_id,
            run_id=managed.run_id,
            messages=managed.messages,
            confirm_dangerous=confirm_dangerous,
        ):
            if _is_approval_chunk(chunk):
                if managed.stream_queue is not None:
                    await managed.stream_queue.put(chunk)
                raise ApprovalRequired()
            if managed.stream_queue is not None:
                await managed.stream_queue.put(chunk)

    async def _emit(self, managed: ManagedRun, event_type: str, payload: dict[str, Any]) -> None:
        self._events.append_event(managed.thread_id, managed.run_id, event_type, payload)
        if managed.stream_queue is not None:
            await managed.stream_queue.put(_sse(event_type, payload))

    def _mark_cancelled(self, managed: ManagedRun) -> None:
        self._events.append_event(managed.thread_id, managed.run_id, "cancelled", {})
        self._events.update_run_status(managed.run_id, RUN_STATUS_CANCELLED, completed=True)

    def _is_terminal(self, run_id: str) -> bool:
        run = self._events.get_run(run_id) or {}
        return str(run.get("status", "")) in TERMINAL_RUN_STATUSES

    def _finalize_memory(self, managed: ManagedRun) -> None:
        finalize = getattr(self._runner, "finalize_memory", None)
        if not callable(finalize):
            return
        try:
            finalize(managed.thread_id, managed.run_id, messages=managed.messages)
        except Exception:
            return


def _is_approval_chunk(chunk: str) -> bool:
    event_type = ""
    data = ""
    for line in chunk.splitlines():
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
    if event_type != "approval_required":
        return False
    try:
        payload = json.loads(data or "{}")
    except json.JSONDecodeError:
        return True
    return bool(payload.get("required", True))


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
