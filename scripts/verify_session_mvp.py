#!/usr/bin/env python
"""Verify session-level P0/P1 runtime guarantees without external LLM calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import (  # noqa: E402
    EventStore,
    RunConcurrencyLimitError,
)
from copilot_agent.runtime.execution_engine import ExecutionEngine, GraphInterrupted  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


class ApprovalState(TypedDict):
    value: str


class CheckpointApprovalRunner:
    def __init__(self, event_store: EventStore) -> None:
        self._events = event_store
        graph = StateGraph(ApprovalState)
        graph.add_node("approval", self._approval_node)
        graph.set_entry_point("approval")
        graph.add_edge("approval", END)
        self._graph = graph.compile(checkpointer=MemorySaver())

    def _approval_node(self, state: ApprovalState) -> ApprovalState:
        approved = interrupt({"required": True, "reason": "dangerous_tool", "message": "approve?"})
        return {"value": "approved" if approved else "rejected"}

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        resume: bool | None = None,
    ):
        config = {"configurable": {"thread_id": conversation_id, "run_id": run_id}}
        graph_input = Command(resume=resume) if resume is not None else {"value": "start"}
        async for event in self._graph.astream_events(graph_input, config=config, version="v2"):
            payload = _interrupt_payload(event)
            if payload is not None:
                yield self._emit(conversation_id, str(run_id), "approval_required", payload)
                raise GraphInterrupted(payload)
        if resume is False:
            yield self._emit(conversation_id, str(run_id), "token", {"text": "rejected"})
        elif resume is True:
            yield self._emit(conversation_id, str(run_id), "token", {"text": "resumed-approved"})
        yield self._emit(conversation_id, str(run_id), "done", {})

    def finalize_memory(self, *_args, **_kwargs) -> None:
        return None

    def _emit(self, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> str:
        self._events.append_event(thread_id, run_id, event_type, payload)
        return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class SlowRunner:
    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay

    async def run_stream(self, **_kwargs):
        await asyncio.sleep(self.delay)
        yield ""

    def finalize_memory(self, *_args, **_kwargs) -> None:
        return None


class FakeToolBoundModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_post_1",
                        "name": "http_post",
                        "args": {
                            "path": "/api/v1/jobs/watermark",
                            "json_body": {"image_url": "https://example.invalid/a.png"},
                        },
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="approved-complete-after-tool")


class FakeLLMProvider:
    def __init__(self) -> None:
        self.model = FakeToolBoundModel()

    def get_tool_bound_model(self, _tools):
        return self.model


class FakeHttpTools:
    def __init__(self) -> None:
        self.post_calls = 0

    async def http_post(
        self,
        path: str,
        json_body: dict[str, Any],
        cookie_header: str | None = None,
        stored_cookie: str | None = None,
        idempotency_key: str | None = None,
        *,
        allow_job_post: bool,
        user_confirmed_dangerous: bool,
    ):
        del cookie_header, stored_cookie, idempotency_key
        self.post_calls += 1
        return {
            "ok": True,
            "status_code": 200,
            "body": {
                "job_id": "verify-job",
                "path": path,
                "confirmed": user_confirmed_dangerous,
                "allow_job_post": allow_job_post,
                "image_url": json_body.get("image_url"),
            },
        }


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


async def verify(event_store_path: Path, thread_prefix: str) -> dict[str, Any]:
    previous_timeout = settings.run_timeout_seconds
    previous_allow_job_post = settings.copilot_allow_job_post
    previous_max_concurrent = settings.max_concurrent_runs
    try:
        store = EventStore(str(event_store_path))
        approval_engine = ExecutionEngine(event_store=store, runner=CheckpointApprovalRunner(store))  # type: ignore[arg-type]
        approval = await approval_engine.create_run(
            thread_id=f"{thread_prefix}-approval",
            messages=[{"role": "user", "content": "approval"}],
        )
        waiting = await _wait_for_status(store, approval.run_id, {"waiting_approval"})
        await approval_engine.approve(approval.run_id)
        approved = await _wait_for_status(store, approval.run_id, {"completed"})
        approval_events = store.list_run_events(approval.run_id)

        settings.run_timeout_seconds = 1
        timeout_store = EventStore(str(event_store_path))
        timeout_engine = ExecutionEngine(event_store=timeout_store, runner=SlowRunner())  # type: ignore[arg-type]
        timed = await timeout_engine.create_run(
            thread_id=f"{thread_prefix}-timeout",
            messages=[{"role": "user", "content": "slow"}],
        )
        timed_run = await _wait_for_status(timeout_store, timed.run_id, {"failed"}, timeout=3.0)
        timeout_events = timeout_store.list_run_events(timed.run_id)

        from copilot_agent.agent.runner import ChatRunner
        from copilot_agent.conversation_store import ConversationCookieStore
        from copilot_agent.memory import MemoryManager
        from copilot_agent.rag.retriever import RagStore

        settings.copilot_allow_job_post = True
        chat_store = EventStore(str(event_store_path))
        fake_http = FakeHttpTools()
        fake_llm = FakeLLMProvider()
        chat_memory = MemoryManager(
            rag_store=RagStore([]),
            event_store=chat_store,
            checkpoint_path=str(event_store_path.with_name(f"{event_store_path.stem}-chat-checkpoints.sqlite")),
        )
        chat_runner = ChatRunner(
            rag_store=RagStore([]),
            cookie_store=ConversationCookieStore(ttl_seconds=60),
            event_store=chat_store,
            http=fake_http,  # type: ignore[arg-type]
            memory=chat_memory,
            llm_provider=fake_llm,  # type: ignore[arg-type]
        )
        chat_engine = ExecutionEngine(event_store=chat_store, runner=chat_runner)
        try:
            chat = await chat_engine.create_run(
                thread_id=f"{thread_prefix}-chatrunner",
                messages=[{"role": "user", "content": "enqueue a watermark job"}],
            )
            chat_waiting = await _wait_for_status(chat_store, chat.run_id, {"waiting_approval", "failed"}, timeout=5.0)
            pre_approve_post_calls = fake_http.post_calls
            if chat_waiting.get("status") == "waiting_approval":
                await chat_engine.approve(chat.run_id)
            deadline = asyncio.get_running_loop().time() + 5.0
            chat_done: dict[str, Any] = {}
            while asyncio.get_running_loop().time() < deadline:
                chat_events = chat_store.list_run_events(chat.run_id)
                event_types = [event["type"] for event in chat_events]
                if "tool_end" in event_types:
                    chat_done = chat_store.get_run(chat.run_id) or {}
                    break
                chat_done = chat_store.get_run(chat.run_id) or {}
                if str(chat_done.get("status", "")) in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.05)
            if chat_done.get("status") not in {"completed", "failed"}:
                await chat_engine.cancel(chat.run_id)
                chat_done = await _wait_for_status(chat_store, chat.run_id, {"cancelled", "failed"}, timeout=2.0)
            chat_events = chat_store.list_run_events(chat.run_id)
        finally:
            await chat_runner.aclose()

        settings.max_concurrent_runs = 2
        concurrency_path = event_store_path.with_name(f"{event_store_path.stem}-concurrency.sqlite")
        concurrency_store = EventStore(str(concurrency_path))
        concurrency_engine = ExecutionEngine(
            event_store=concurrency_store,
            runner=SlowRunner(delay=2.0),
        )  # type: ignore[arg-type]
        concurrent_runs = []
        concurrency_blocked = False
        for index in range(3):
            thread_id = f"{thread_prefix}-concurrency-{index}"
            try:
                managed = await concurrency_engine.create_run(
                    thread_id=thread_id,
                    messages=[{"role": "user", "content": f"slow-{index}"}],
                )
                concurrent_runs.append(managed.run_id)
            except RunConcurrencyLimitError:
                concurrency_blocked = True
                break
        running_count = 0
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            running_count = sum(
                1
                for run_id in concurrent_runs
                if str((concurrency_store.get_run(run_id) or {}).get("status", "")) == "running"
            )
            if running_count >= 2:
                break
            await asyncio.sleep(0.05)
        for run_id in concurrent_runs:
            await _wait_for_status(
                concurrency_store,
                run_id,
                {"completed", "failed", "cancelled"},
                timeout=3.0,
            )

        rehydrate_path = event_store_path.with_name(f"{event_store_path.stem}-rehydrate.sqlite")
        rehydrate_store = EventStore(str(rehydrate_path))
        rehydrate_thread = f"{thread_prefix}-rehydrate"
        rehydrate_runner = CheckpointApprovalRunner(rehydrate_store)
        rehydrate_engine = ExecutionEngine(
            event_store=rehydrate_store,
            runner=rehydrate_runner,
        )  # type: ignore[arg-type]
        rehydrate_run = await rehydrate_engine.create_run(
            thread_id=rehydrate_thread,
            messages=[{"role": "user", "content": "rehydrate approval"}],
        )
        await _wait_for_status(rehydrate_store, rehydrate_run.run_id, {"waiting_approval"}, timeout=5.0)
        restarted_engine = ExecutionEngine(
            event_store=rehydrate_store,
            runner=rehydrate_runner,
        )  # type: ignore[arg-type]
        await restarted_engine.approve(rehydrate_run.run_id)
        rehydrated = await _wait_for_status(rehydrate_store, rehydrate_run.run_id, {"completed"}, timeout=5.0)
        rehydrated_events = rehydrate_store.list_run_events(rehydrate_run.run_id)

        return {
            "approval": {
                "waiting_status": waiting.get("status"),
                "final_status": approved.get("status"),
                "event_types": [event["type"] for event in approval_events],
                "run_started_count": sum(1 for event in approval_events if event["type"] == "run_started"),
                "token_text": "".join(str(event.get("payload", {}).get("text", "")) for event in approval_events if event["type"] == "token"),
            },
            "timeout": {
                "status": timed_run.get("status"),
                "error": timed_run.get("error"),
                "event_types": [event["type"] for event in timeout_events],
            },
            "chatrunner_approval": {
                "waiting_status": chat_waiting.get("status"),
                "final_status": chat_done.get("status"),
                "event_types": [event["type"] for event in chat_events],
                "run_started_count": sum(1 for event in chat_events if event["type"] == "run_started"),
                "token_text": "".join(
                    str(event.get("payload", {}).get("text", "")) for event in chat_events if event["type"] == "token"
                ),
                "tool_start_count": sum(1 for event in chat_events if event["type"] == "tool_start"),
                "tool_end_count": sum(1 for event in chat_events if event["type"] == "tool_end"),
                "pre_approve_post_calls": pre_approve_post_calls,
                "post_calls": fake_http.post_calls,
                "llm_calls": fake_llm.model.calls,
            },
            "concurrency": {
                "max_concurrent_runs": 2,
                "started_run_ids": concurrent_runs,
                "running_count": running_count,
                "blocked": concurrency_blocked,
            },
            "rehydrate": {
                "final_status": rehydrated.get("status"),
                "event_types": [event["type"] for event in rehydrated_events],
                "run_started_count": sum(1 for event in rehydrated_events if event["type"] == "run_started"),
                "token_text": "".join(
                    str(event.get("payload", {}).get("text", "")) for event in rehydrated_events if event["type"] == "token"
                ),
            },
        }
    finally:
        settings.run_timeout_seconds = previous_timeout
        settings.copilot_allow_job_post = previous_allow_job_post
        settings.max_concurrent_runs = previous_max_concurrent


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent session MVP P0/P1 guarantees.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--thread-prefix", default=f"session-mvp-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/session-mvp-summary.json"))
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, args.thread_prefix))
    checks = {
        "approval_waiting": summary["approval"]["waiting_status"] == "waiting_approval",
        "approval_completed": summary["approval"]["final_status"] == "completed",
        "approval_events": {"approval_required", "approval_resolved", "token", "done"}.issubset(set(summary["approval"]["event_types"])),
        "approval_resumed_once": summary["approval"]["run_started_count"] == 2,
        "approval_token": "resumed-approved" in summary["approval"]["token_text"],
        "timeout_failed": summary["timeout"]["status"] == "failed",
        "timeout_error_event": "error" in summary["timeout"]["event_types"],
        "timeout_reason": "timed out" in str(summary["timeout"]["error"]),
        "chatrunner_approval_waiting": summary["chatrunner_approval"]["waiting_status"] == "waiting_approval",
        "chatrunner_approval_reached_tool": summary["chatrunner_approval"]["post_calls"] == 1,
        "chatrunner_resumed_once": summary["chatrunner_approval"]["run_started_count"] == 2,
        "chatrunner_no_tool_before_approve": summary["chatrunner_approval"]["pre_approve_post_calls"] == 0,
        "chatrunner_tool_executed_once": summary["chatrunner_approval"]["post_calls"] == 1,
        "chatrunner_tool_audit": (
            summary["chatrunner_approval"]["tool_start_count"] == 1
            and summary["chatrunner_approval"]["tool_end_count"] == 1
        ),
        "concurrency_blocked": summary["concurrency"]["blocked"],
        "concurrency_running_cap": summary["concurrency"]["running_count"] <= 2,
        "rehydrate_completed": summary["rehydrate"]["final_status"] == "completed",
        "rehydrate_resumed_once": summary["rehydrate"]["run_started_count"] == 2,
        "rehydrate_token": "resumed-approved" in summary["rehydrate"]["token_text"],
    }
    summary["checks"] = checks
    summary["session_mvp"] = "PASS" if all(checks.values()) else "FAIL"
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"session_mvp={summary['session_mvp']}")
    return 0 if summary["session_mvp"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
