from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from copilot_agent.runtime.event_store import (
    EventStore,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_CANCELLING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RunConcurrencyLimitError,
    IdempotencyConflictError,
    TERMINAL_RUN_STATUSES,
)
from copilot_agent.runtime.checkpoint_reader import CheckpointReader
from copilot_agent.runtime.event_schema import (
    EVENT_CHECKPOINT_CONSISTENCY_CHECKED,
    EVENT_RUN_COMPLETED_META,
    EVENT_RUN_CONSISTENCY_CHECKED,
    EVENT_RUN_FAILED_META,
    EVENT_TOOL_SIDE_EFFECT_RECORDED,
)
from copilot_agent.contracts.adapters.sse import SseAdapter
from copilot_agent.contracts.base import RuntimeEvent
from copilot_agent.settings import settings
from copilot_agent.tools.audit import build_blocked_tool_side_effect_payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from copilot_agent.agent.runner import ChatRunner


FINAL_STREAM_MARKER = object()


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    idempotency_reused: bool = False
    interrupt_payload: dict[str, Any] = field(default_factory=dict)


class GraphInterrupted(Exception):
    def __init__(self, interrupt_payload: dict[str, Any] | None = None) -> None:
        self.interrupt_payload = interrupt_payload or {}
        super().__init__("graph interrupted for approval")


class CheckpointSyncFailed(Exception):
    def __init__(self, message: str = "checkpoint sync failed") -> None:
        self.message = message
        super().__init__(message)


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
        idempotency_key: str | None = None,
    ) -> ManagedRun:
        payload_hash = self._events.idempotency_payload_hash(
            {
                "messages": messages,
                "confirm_dangerous": bool(confirm_dangerous),
                "stream": bool(stream),
            }
        )
        await self._acquire_run_slot()
        try:
            run = self._events.create_run(
                thread_id,
                status=RUN_STATUS_QUEUED,
                idempotency_key=idempotency_key,
                idempotency_payload_hash=payload_hash if idempotency_key else None,
            )
            created_status = str(run.get("status") or "")
            if created_status != RUN_STATUS_QUEUED or self._has_run_created(str(run["id"])):
                managed = ManagedRun(
                    run_id=str(run["id"]),
                    thread_id=thread_id,
                    messages=messages,
                    confirm_dangerous=confirm_dangerous,
                    stream=stream,
                    stream_queue=asyncio.Queue() if stream else None,
                    idempotency_reused=True,
                )
                if managed.stream_queue is not None:
                    await managed.stream_queue.put(FINAL_STREAM_MARKER)
                    async with self._lock:
                        self._runs[managed.run_id] = managed
                self._release_run_slot()
                return managed
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
        self._events.append_event(
            managed.thread_id,
            run_id,
            "approval_resolved",
            {
                "approved": True,
                "resume_value": True,
                "checkpoint_thread_id": managed.thread_id,
                "checkpoint_resume": True,
            },
        )
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
        self._events.append_event(
            managed.thread_id,
            run_id,
            "approval_resolved",
            {
                "approved": False,
                "resume_value": False,
                "checkpoint_thread_id": managed.thread_id,
                "checkpoint_resume": True,
            },
        )
        self._append_blocked_side_effects_for_rejection(managed)
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
        if managed.idempotency_reused:
            async with self._lock:
                self._runs.pop(run_id, None)

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
            self._events.append_event(
                thread_id,
                run_id,
                "run_started",
                {
                    "status": RUN_STATUS_RUNNING,
                    "resumed": True,
                    "resume_from_checkpoint": True,
                    "checkpoint_thread_id": thread_id,
                },
            )
            await self._run_with_timeout(managed, confirm_dangerous=True, resume=True)
            self._events.complete_run(run_id)
        except CheckpointSyncFailed as exc:
            message = str(exc.message or exc)
            await self._emit_runtime(
                managed,
                RuntimeEvent.from_payload(
                    "error",
                    {"error": message, "reason": "checkpoint_sync_failed"},
                    thread_id=managed.thread_id,
                    run_id=managed.run_id,
                ),
            )
            self._append_failed_meta(
                managed,
                error=message,
                reason="checkpoint_sync_failed",
                phase="finalize",
            )
            self._events.complete_run(run_id, error=message)
        except TimeoutError:
            message = f"run timed out after {self._run_timeout_seconds} seconds"
            await self._emit_runtime(
                managed,
                RuntimeEvent.from_payload(
                    "error",
                    {"error": message, "reason": "run_timeout"},
                    thread_id=managed.thread_id,
                    run_id=managed.run_id,
                ),
            )
            self._append_failed_meta(managed, error=message, reason="run_timeout", phase="execute")
            self._events.complete_run(run_id, error=message)
        except asyncio.CancelledError:
            self._mark_cancelled(managed)
        except Exception as e:
            message = str(e)
            await self._emit_runtime(
                managed,
                RuntimeEvent.from_payload(
                    "error",
                    {"error": message},
                    thread_id=managed.thread_id,
                    run_id=managed.run_id,
                ),
            )
            self._append_failed_meta(managed, error=message, reason="runtime_exception", phase="execute")
            self._events.complete_run(run_id, error=message)
        finally:
            if managed.stream_queue is not None:
                await managed.stream_queue.put(FINAL_STREAM_MARKER)
            if managed.slot_acquired:
                self._release_run_slot()
                managed.slot_acquired = False
            if self._is_terminal(run_id):
                await self._finalize_memory(managed)
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
            self._events.append_event(
                thread_id,
                run_id,
                "run_started",
                {
                    "status": RUN_STATUS_RUNNING,
                    "resumed": True,
                    "resume_from_checkpoint": True,
                    "checkpoint_thread_id": thread_id,
                },
            )
            await self._run_with_timeout(managed, confirm_dangerous=True, resume=True)
            self._events.complete_run(run_id)
        except GraphInterrupted:
            managed.checkpoint_pending = True
            self._events.update_run_status(run_id, RUN_STATUS_WAITING_APPROVAL)
            self._append_checkpoint_meta(managed)
        except TimeoutError:
            message = f"run timed out after {self._run_timeout_seconds} seconds"
            await self._emit_runtime(
                managed,
                RuntimeEvent.from_payload(
                    "error",
                    {"error": message, "reason": "run_timeout"},
                    thread_id=managed.thread_id,
                    run_id=managed.run_id,
                ),
            )
            self._append_failed_meta(managed, error=message, reason="run_timeout", phase="approval_resume")
            self._events.complete_run(run_id, error=message)
        except asyncio.CancelledError:
            self._mark_cancelled(managed)
        except Exception as e:
            message = str(e)
            await self._emit_runtime(
                managed,
                RuntimeEvent.from_payload(
                    "error",
                    {"error": message},
                    thread_id=managed.thread_id,
                    run_id=managed.run_id,
                ),
            )
            self._append_failed_meta(managed, error=message, reason="runtime_exception", phase="approval_resume")
            self._events.complete_run(run_id, error=message)
        finally:
            if managed.stream_queue is not None:
                await managed.stream_queue.put(FINAL_STREAM_MARKER)
            if managed.slot_acquired:
                self._release_run_slot()
                managed.slot_acquired = False
            if self._is_terminal(run_id):
                await self._finalize_memory(managed)
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

    async def _emit_runtime(self, managed: ManagedRun, event: RuntimeEvent) -> None:
        self._events.append_event(
            managed.thread_id,
            managed.run_id,
            event.kind,
            event.to_store_payload(),
        )
        if managed.stream_queue is not None:
            await managed.stream_queue.put(SseAdapter.encode(event))

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
                "checkpoint_pending": True,
                "resume_supported": True,
                "resume_command": "Command(resume=<approval_value>)",
                "interrupt_summary": summary,
            },
        )

    def _append_failed_meta(self, managed: ManagedRun, *, error: str, reason: str, phase: str) -> None:
        self._events.append_event(
            managed.thread_id,
            managed.run_id,
            EVENT_RUN_FAILED_META,
            {
                "status": "failed",
                "reason": reason,
                "phase": phase,
                "error": error,
                "last_successful_event_id": self._events.latest_run_event_id(managed.run_id),
                "last_successful_sequence": self._events.latest_run_sequence(managed.run_id),
                "checkpoint_thread_id": managed.thread_id,
                "checkpoint_pending": bool(managed.checkpoint_pending),
                "resume_supported": bool(managed.checkpoint_pending),
            },
        )

    def _append_blocked_side_effects_for_rejection(self, managed: ManagedRun) -> None:
        existing_call_ids = {
            str((event.get("payload") or {}).get("call_id") or "")
            for event in self._events.list_run_events(managed.run_id)
            if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED
        }
        for call in (managed.interrupt_payload or {}).get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            call_id = str(call.get("id") or "").strip()
            if not call_id or call_id in existing_call_ids:
                continue
            payload = build_blocked_tool_side_effect_payload(
                tool_call=call,
                reason="approval_rejected",
                policy_source="human_approval",
                requires_approval=True,
            )
            if payload is None:
                continue
            self._events.append_event(
                managed.thread_id,
                managed.run_id,
                EVENT_TOOL_SIDE_EFFECT_RECORDED,
                payload,
            )

    def _mark_cancelled(self, managed: ManagedRun) -> None:
            self._events.append_event(managed.thread_id, managed.run_id, "cancelled", {})
            self._events.update_run_status(managed.run_id, RUN_STATUS_CANCELLED, completed=True)

    def _is_terminal(self, run_id: str) -> bool:
        run = self._events.get_run(run_id) or {}
        return str(run.get("status", "")) in TERMINAL_RUN_STATUSES

    async def _finalize_memory(self, managed: ManagedRun) -> None:
        checkpoint_consistency: dict[str, Any] | None = None
        if (self._events.get_run(managed.run_id) or {}).get("status") == RUN_STATUS_COMPLETED:
            checkpoint_consistency = await self._append_checkpoint_consistency_checked(managed)
        self._append_consistency_checked(managed, checkpoint_consistency=checkpoint_consistency)
        finalize = getattr(self._runner, "finalize_memory", None)
        if callable(finalize):
            try:
                finalize(managed.thread_id, managed.run_id, messages=managed.messages)
            except Exception:
                return
        compact = getattr(self._runner, "compact_checkpoint", None)
        if not callable(compact):
            return
        try:
            await compact(managed.thread_id, run_id=managed.run_id)
        except Exception:
            return

    async def _append_checkpoint_consistency_checked(self, managed: ManagedRun) -> dict[str, Any]:
        events = self._events.list_run_events(managed.run_id)
        completed_meta = next(
            (
                event
                for event in reversed(events)
                if str(event.get("type") or "") == EVENT_RUN_COMPLETED_META
            ),
            None,
        )
        reported = None
        source_event_ids: list[int] = []
        if completed_meta is not None:
            payload = completed_meta.get("payload") if isinstance(completed_meta.get("payload"), dict) else {}
            reported = _optional_int(payload.get("message_count"))
            event_id = completed_meta.get("id")
            if event_id is not None:
                source_event_ids.append(int(event_id))

        warnings: list[str] = []
        error: str | None = None
        checkpoint_read_ok = False
        checkpoint_missing = False
        checkpoint_has_interrupt: bool | None = None
        actual: int | None = None

        graph = getattr(self._runner, "graph", None)
        if graph is None:
            warnings.append("checkpoint_graph_unavailable")
        else:
            try:
                snapshot = await CheckpointReader(graph).snapshot(managed.thread_id)
                checkpoint_read_ok = True
                actual = _optional_int(snapshot.get("message_count"))
                checkpoint_has_interrupt = bool(snapshot.get("has_interrupt", False))
                if actual is None or actual <= 0:
                    checkpoint_missing = True
                    warnings.append("checkpoint_missing")
            except Exception as exc:
                error = str(exc)
                warnings.append("checkpoint_read_failed")

        checkpoint_match = bool(
            checkpoint_read_ok
            and not checkpoint_missing
            and reported is not None
            and actual == reported
        )
        if completed_meta is None:
            warnings.append("run_completed_meta_missing")
        elif reported is None:
            warnings.append("run_completed_meta_message_count_missing")
        elif checkpoint_read_ok and not checkpoint_missing and actual != reported:
            warnings.append("checkpoint_message_count_mismatch")

        payload = {
            "checkpoint_read_ok": checkpoint_read_ok,
            "checkpoint_missing": checkpoint_missing,
            "checkpoint_has_interrupt": checkpoint_has_interrupt,
            "checkpoint_message_count_actual": actual,
            "checkpoint_message_count_reported": reported,
            "checkpoint_match": checkpoint_match,
            "warnings": warnings,
            "error": error,
            "source_event_ids": source_event_ids,
        }
        event = self._events.append_event(
            managed.thread_id,
            managed.run_id,
            EVENT_CHECKPOINT_CONSISTENCY_CHECKED,
            payload,
        )
        event_id = event.get("id")
        if event_id is not None:
            payload["event_id"] = int(event_id)
        return payload

    def _append_consistency_checked(
        self,
        managed: ManagedRun,
        *,
        checkpoint_consistency: dict[str, Any] | None = None,
    ) -> None:
        run = self._events.get_run(managed.run_id) or {}
        status = str(run.get("status") or "")
        events = self._events.list_run_events(managed.run_id)
        event_types = [str(event.get("type") or "") for event in events]
        missing: list[str] = []
        if status == "completed" and "done" not in event_types:
            missing.append("done")
        if status == "failed" and "error" not in event_types:
            missing.append("error")
        if status == "failed" and EVENT_RUN_FAILED_META not in event_types:
            missing.append(EVENT_RUN_FAILED_META)
        if status == "cancelled" and "cancelled" not in event_types:
            missing.append("cancelled")
        if status == "completed" and "approval_required" in event_types and "approval_resolved" not in event_types:
            missing.append("approval_resolved")
        starts = {
            str((event.get("payload") or {}).get("call_id") or "")
            for event in events
            if event.get("type") == "tool_start"
        }
        ends = {
            str((event.get("payload") or {}).get("call_id") or "")
            for event in events
            if event.get("type") == "tool_end"
        }
        missing_tool_ends = sorted(call_id for call_id in starts if call_id and call_id not in ends)
        missing.extend(f"tool_end:{call_id}" for call_id in missing_tool_ends)
        payload = {
            "status": status,
            "ok": not missing,
            "missing_events": missing,
            "event_count": len(events),
            "last_event_id": self._events.latest_run_event_id(managed.run_id),
            "last_sequence": self._events.latest_run_sequence(managed.run_id),
            "checkpoint_pending": bool(managed.checkpoint_pending),
            "missing_tool_end_call_ids": missing_tool_ends,
        }
        if checkpoint_consistency is not None:
            warnings = checkpoint_consistency.get("warnings")
            payload.update(
                {
                    "checkpoint_match": checkpoint_consistency.get("checkpoint_match"),
                    "checkpoint_message_count_actual": checkpoint_consistency.get(
                        "checkpoint_message_count_actual"
                    ),
                    "checkpoint_message_count_reported": checkpoint_consistency.get(
                        "checkpoint_message_count_reported"
                    ),
                    "checkpoint_warning_count": len(warnings) if isinstance(warnings, list) else 0,
                }
            )
        self._events.append_event(
            managed.thread_id,
            managed.run_id,
            EVENT_RUN_CONSISTENCY_CHECKED,
            payload,
        )

    def _cleanup_orphan_runs(self) -> None:
        for run in self._events.list_runs_by_status({RUN_STATUS_CANCELLING}):
            self._recover_cancelled_run(run, reason="process_restarted")
        for run in self._events.list_runs_by_status({RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}):
            self._recover_failed_run(run, reason="process_restarted")
        self._rehydrate_waiting_approval_runs()

    def _rehydrate_waiting_approval_runs(self) -> None:
        for run in self._events.list_runs_by_status({RUN_STATUS_WAITING_APPROVAL}):
            run_id = str(run["id"])
            thread_id = str(run["thread_id"])
            self._events.mark_run_recovered(run_id, reason="waiting_approval_rehydrated")
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

    def _recover_failed_run(self, run: dict[str, Any], *, reason: str) -> None:
        run_id = str(run["id"])
        thread_id = str(run["thread_id"])
        managed = ManagedRun(run_id=run_id, thread_id=thread_id, messages=self._recover_run_messages(run_id))
        self._events.mark_run_recovered(run_id, reason=reason)
        message = "server restarted before run completed"
        self._events.append_event(
            thread_id,
            run_id,
            "error",
            {"error": message, "reason": reason},
        )
        self._append_failed_meta(managed, error=message, reason=reason, phase="startup_recovery")
        self._events.update_run_status(run_id, RUN_STATUS_FAILED, error=message, completed=True)
        self._append_consistency_checked(managed)

    def _recover_cancelled_run(self, run: dict[str, Any], *, reason: str) -> None:
        run_id = str(run["id"])
        thread_id = str(run["thread_id"])
        managed = ManagedRun(run_id=run_id, thread_id=thread_id, messages=self._recover_run_messages(run_id))
        self._events.mark_run_recovered(run_id, reason=reason)
        self._events.append_event(thread_id, run_id, "cancelled", {"reason": reason})
        self._events.update_run_status(run_id, RUN_STATUS_CANCELLED, completed=True)
        self._append_consistency_checked(managed)

    def _has_run_created(self, run_id: str) -> bool:
        return any(event.get("type") == "run_created" for event in self._events.list_run_events(run_id))

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
