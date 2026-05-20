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
    RunConcurrencyLimitError,
    TERMINAL_RUN_STATUSES,
)
from copilot_agent.settings import settings

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
    checkpoint_pending: bool = False
    slot_acquired: bool = False
    rehydrated: bool = False
    interrupt_payload: dict[str, Any] = field(default_factory=dict)


class GraphInterrupted(Exception):
    def __init__(self, interrupt_payload: dict[str, Any] | None = None) -> None:
        self.interrupt_payload = interrupt_payload or {}
        super().__init__("graph interrupted for approval")


class ApprovalRequired(GraphInterrupted):
    """Backward-compatible alias for GraphInterrupted."""


class ExecutionEngine:
    """Local runtime engine for background runs.

    This is intentionally still an in-process implementation. It owns the
    stable API boundary between FastAPI handlers, EventStore, and ChatRunner.
    """

    def __init__(self, *, event_store: EventStore, runner: "ChatRunner") -> None:
        self._events = event_store
        self._runner = runner
        self._run_timeout_seconds = max(1, int(settings.run_timeout_seconds))
        self._max_concurrent_runs = max(1, int(settings.max_concurrent_runs))
        self._run_slots = asyncio.Semaphore(self._max_concurrent_runs)
        self._runs: dict[str, ManagedRun] = {}
        self._lock = asyncio.Lock()
        self._cleanup_orphan_runs()

    async def create_run(
        self,
        *,
        thread_id: str,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool = False,
        stream: bool = False,
    ) -> ManagedRun:
        await self._acquire_run_slot()
        try:
            run = self._events.create_run(thread_id, status=RUN_STATUS_QUEUED)
            managed = ManagedRun(
                run_id=str(run["id"]),
                thread_id=thread_id,
                messages=messages,
                confirm_dangerous=confirm_dangerous,
                stream=stream,
                stream_queue=asyncio.Queue() if stream else None,
                slot_acquired=True,
            )
            if confirm_dangerous:
                managed.approved = True
                managed.approval_event.set()
            self._events.append_event(
                thread_id,
                managed.run_id,
                "run_created",
                {"status": RUN_STATUS_QUEUED, "messages": messages},
            )
            async with self._lock:
                self._runs[managed.run_id] = managed
                managed.task = asyncio.create_task(self._execute(managed))
            return managed
        except Exception:
            self._release_run_slot()
            raise

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
        if str(current.get("status", "")) != RUN_STATUS_WAITING_APPROVAL:
            return current
        managed.approved = True
        self._events.append_event(managed.thread_id, run_id, "approval_resolved", {"approved": True})
        if managed.rehydrated or managed.task is None or managed.task.done():
            await self._start_approval_continuation(managed)
        else:
            managed.approval_event.set()
        return self._events.get_run(run_id) or {}

    async def reject(self, run_id: str) -> dict[str, Any]:
        managed = await self._get_managed(run_id)
        current = self._events.get_run(run_id)
        if current is None:
            return {}
        if str(current.get("status", "")) != RUN_STATUS_WAITING_APPROVAL:
            return current
        managed.rejected = True
        self._events.append_event(managed.thread_id, run_id, "approval_resolved", {"approved": False})
        if managed.rehydrated or managed.task is None or managed.task.done():
            await self._start_approval_continuation(managed)
        else:
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

    async def _acquire_run_slot(self) -> None:
        if self._run_slots._value <= 0:  # noqa: SLF001
            raise RunConcurrencyLimitError(self._max_concurrent_runs)
        await self._run_slots.acquire()

    def _release_run_slot(self) -> None:
        self._run_slots.release()

    async def _start_approval_continuation(self, managed: ManagedRun) -> None:
        if not managed.slot_acquired:
            await self._acquire_run_slot()
            managed.slot_acquired = True
        async with self._lock:
            if managed.task is not None and not managed.task.done():
                managed.approval_event.set()
                return
            managed.task = asyncio.create_task(self._continue_after_approval(managed))

    async def _execute(self, managed: ManagedRun) -> None:
        run_id = managed.run_id
        thread_id = managed.thread_id
        try:
            self._events.update_run_status(run_id, RUN_STATUS_RUNNING)
            self._events.append_event(thread_id, run_id, "run_started", {"status": RUN_STATUS_RUNNING})
            await self._run_with_timeout(managed, confirm_dangerous=managed.confirm_dangerous or managed.approved)
            self._events.complete_run(run_id)
        except GraphInterrupted as exc:
            if managed.cancel_requested:
                self._mark_cancelled(managed)
                return
            managed.interrupt_payload = exc.interrupt_payload
            managed.checkpoint_pending = True
            self._events.update_run_status(run_id, RUN_STATUS_WAITING_APPROVAL)
            self._append_checkpoint_meta(managed)
            await managed.approval_event.wait()
            managed.checkpoint_pending = False
            if managed.cancel_requested:
                self._mark_cancelled(managed)
                return
            if managed.rejected:
                await self._run_with_timeout(managed, confirm_dangerous=False, resume=False)
                self._events.complete_run(run_id)
                return
            self._events.update_run_status(run_id, RUN_STATUS_RUNNING)
            self._events.append_event(thread_id, run_id, "run_started", {"status": RUN_STATUS_RUNNING, "resumed": True})
            await self._run_with_timeout(managed, confirm_dangerous=True, resume=True)
            self._events.complete_run(run_id)
        except TimeoutError:
            message = f"run timed out after {self._run_timeout_seconds} seconds"
            await self._emit(managed, "error", {"error": message, "reason": "run_timeout"})
            self._events.complete_run(run_id, error=message)
        except asyncio.CancelledError:
            self._mark_cancelled(managed)
        except Exception as e:
            await self._emit(managed, "error", {"error": str(e)})
            self._events.complete_run(run_id, error=str(e))
        finally:
            if managed.stream_queue is not None:
                await managed.stream_queue.put(FINAL_STREAM_MARKER)
            if managed.slot_acquired:
                self._release_run_slot()
                managed.slot_acquired = False
            if self._is_terminal(run_id):
                self._finalize_memory(managed)
                async with self._lock:
                    self._runs.pop(run_id, None)

    async def _continue_after_approval(self, managed: ManagedRun) -> None:
        run_id = managed.run_id
        thread_id = managed.thread_id
        try:
            if managed.cancel_requested:
                self._mark_cancelled(managed)
                return
            if managed.rejected:
                await self._run_with_timeout(managed, confirm_dangerous=False, resume=False)
                self._events.complete_run(run_id)
                return
            self._events.update_run_status(run_id, RUN_STATUS_RUNNING)
            self._events.append_event(thread_id, run_id, "run_started", {"status": RUN_STATUS_RUNNING, "resumed": True})
            await self._run_with_timeout(managed, confirm_dangerous=True, resume=True)
            self._events.complete_run(run_id)
        except GraphInterrupted:
            managed.checkpoint_pending = True
            self._events.update_run_status(run_id, RUN_STATUS_WAITING_APPROVAL)
            self._append_checkpoint_meta(managed)
        except TimeoutError:
            message = f"run timed out after {self._run_timeout_seconds} seconds"
            await self._emit(managed, "error", {"error": message, "reason": "run_timeout"})
            self._events.complete_run(run_id, error=message)
        except asyncio.CancelledError:
            self._mark_cancelled(managed)
        except Exception as e:
            await self._emit(managed, "error", {"error": str(e)})
            self._events.complete_run(run_id, error=str(e))
        finally:
            if managed.stream_queue is not None:
                await managed.stream_queue.put(FINAL_STREAM_MARKER)
            if managed.slot_acquired:
                self._release_run_slot()
                managed.slot_acquired = False
            if self._is_terminal(run_id):
                self._finalize_memory(managed)
                async with self._lock:
                    self._runs.pop(run_id, None)

    async def _run_once(self, managed: ManagedRun, *, confirm_dangerous: bool, resume: bool | None = None) -> None:
        async for chunk in self._runner.run_stream(
            conversation_id=managed.thread_id,
            run_id=managed.run_id,
            messages=managed.messages,
            confirm_dangerous=confirm_dangerous,
            resume=resume,
        ):
            if managed.stream_queue is not None:
                await managed.stream_queue.put(chunk)

    async def _run_with_timeout(self, managed: ManagedRun, *, confirm_dangerous: bool, resume: bool | None = None) -> None:
        async with asyncio.timeout(self._run_timeout_seconds):
            await self._run_once(managed, confirm_dangerous=confirm_dangerous, resume=resume)

    async def _emit(self, managed: ManagedRun, event_type: str, payload: dict[str, Any]) -> None:
        self._events.append_event(managed.thread_id, managed.run_id, event_type, payload)
        if managed.stream_queue is not None:
            await managed.stream_queue.put(_sse(event_type, payload))

    def _append_checkpoint_meta(self, managed: ManagedRun) -> None:
        payload = managed.interrupt_payload or {}
        summary = {
            "required": bool(payload.get("required", True)),
            "reason": payload.get("reason"),
            "message": payload.get("message"),
            "tool_calls": [
                {"name": call.get("name"), "id": call.get("id")}
                for call in (payload.get("tool_calls") or [])
                if isinstance(call, dict)
            ],
        }
        self._events.append_event(
            managed.thread_id,
            managed.run_id,
            "run_checkpoint_meta",
            {
                "checkpoint_thread_id": managed.thread_id,
                "interrupt_summary": summary,
            },
        )

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

    def _cleanup_orphan_runs(self) -> None:
        self._events.fail_non_terminal_runs(
            error="server restarted before run completed",
            exclude_statuses={RUN_STATUS_WAITING_APPROVAL},
        )
        self._rehydrate_waiting_approval_runs()

    def _rehydrate_waiting_approval_runs(self) -> None:
        for run in self._events.list_runs_by_status({RUN_STATUS_WAITING_APPROVAL}):
            run_id = str(run["id"])
            thread_id = str(run["thread_id"])
            messages = self._recover_run_messages(run_id)
            interrupt_payload = self._recover_interrupt_payload(run_id)
            managed = ManagedRun(
                run_id=run_id,
                thread_id=thread_id,
                messages=messages,
                checkpoint_pending=True,
                rehydrated=True,
                interrupt_payload=interrupt_payload,
            )
            self._runs[run_id] = managed

    def _recover_run_messages(self, run_id: str) -> list[dict[str, Any]]:
        for event in self._events.list_run_events(run_id):
            if event.get("type") != "run_created":
                continue
            payload = event.get("payload") or {}
            messages = payload.get("messages")
            if isinstance(messages, list) and messages:
                return [message for message in messages if isinstance(message, dict)]
        return []

    def _recover_interrupt_payload(self, run_id: str) -> dict[str, Any]:
        for event in reversed(self._events.list_run_events(run_id)):
            if event.get("type") == "approval_required":
                payload = event.get("payload")
                return payload if isinstance(payload, dict) else {}
            if event.get("type") == "run_checkpoint_meta":
                payload = event.get("payload") or {}
                summary = payload.get("interrupt_summary")
                if isinstance(summary, dict):
                    return summary
        return {}


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
