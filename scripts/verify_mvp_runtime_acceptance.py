#!/usr/bin/env python
"""End-to-end MVP runtime acceptance, including one live API + LLM agent loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.execution_engine import ExecutionEngine, ManagedRun  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.audit import (  # noqa: E402
    audit_payload_has_secret,
    build_tool_end_payload,
    build_tool_start_payload,
)


class AcceptanceFakeRunner:
    def __init__(self, *, event_store: EventStore, memory: MemoryManager) -> None:
        self._events = event_store
        self._memory = memory

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
    ) -> AsyncIterator[str]:
        thread_id = conversation_id
        actual_run_id = str(run_id)
        scenario = str(messages[-1].get("content", "")) if messages else ""

        if "slow" in scenario:
            await asyncio.sleep(30)
            yield self._emit(thread_id, actual_run_id, "done", {})
            return

        if "approval" in scenario and not confirm_dangerous:
            yield self._emit(
                thread_id,
                actual_run_id,
                "approval_required",
                {
                    "required": True,
                    "reason": "dangerous_tool",
                    "message": "POST /api/v1/jobs/watermark requires approval.",
                },
            )
            return

        goal = "enqueue watermark job" if "approval" in scenario else "check mvp runtime"
        tool_name = "http_post" if "approval" in scenario else "search_docs"
        category = "http" if "approval" in scenario else "memory"
        risk_level = "high" if "approval" in scenario else "low"
        requires_approval = "approval" in scenario
        yield self._emit(
            thread_id,
            actual_run_id,
            "plan_created",
            {
                "goal": goal,
                "strategy": "acceptance_fake_runner",
                "available_tools": [
                    {
                        "name": tool_name,
                        "category": category,
                        "risk_level": risk_level,
                        "requires_approval": requires_approval,
                    }
                ],
            },
        )
        yield self._emit(thread_id, actual_run_id, "token", {"text": f"accepted:{scenario}"})
        call_id = f"{actual_run_id}:{tool_name}"
        yield self._emit(
            thread_id,
            actual_run_id,
            "tool_start",
            build_tool_start_payload(
                name=tool_name,
                call_id=call_id,
                category=category,
                risk_level=risk_level,
                requires_approval=requires_approval,
                arguments={
                    "query": "Redis stream",
                    "cookie_header": "WMSESSIONID=should-not-persist",
                    "json_body": {"password": "should-not-persist"} if tool_name == "http_post" else {},
                },
            ),
        )
        yield self._emit(
            thread_id,
            actual_run_id,
            "tool_end",
            build_tool_end_payload(
                name=tool_name,
                call_id=call_id,
                result={
                    "ok": True,
                    "status_code": 200,
                    "body": {"result": "ok"},
                    "set-cookie": "WMSESSIONID=should-not-persist; Path=/",
                    "_raw_set_cookie_for_store_only": ["WMSESSIONID=should-not-persist; Path=/"],
                },
                duration_ms=3,
            ),
        )
        yield self._emit(thread_id, actual_run_id, "done", {})

    def finalize_memory(self, thread_id: str, run_id: str, *, messages: list[dict[str, Any]] | None = None) -> None:
        fallback_goal = _last_user_content(messages or [])
        self._memory.summarize_run(thread_id, run_id, fallback_goal=fallback_goal)
        self._memory.update_thread_summary(thread_id, run_id)

    def _emit(self, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> str:
        self._events.append_event(thread_id, run_id, event_type, payload)
        return _sse(event_type, payload)


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="MVP runtime acceptance Redis guide")])
    memory = MemoryManager(rag_store=rag, event_store=store, checkpoint_path=str(checkpoint_path))
    runner = AcceptanceFakeRunner(event_store=store, memory=memory)
    engine = ExecutionEngine(event_store=store, runner=runner)  # type: ignore[arg-type]
    projector = TimelineProjector()

    normal_thread = f"{thread_prefix}-normal"
    normal = await engine.create_run(thread_id=normal_thread, messages=[{"role": "user", "content": "normal"}])
    await _wait_managed_done(store, normal)
    second_normal = await engine.create_run(thread_id=normal_thread, messages=[{"role": "user", "content": "normal second"}])
    await _wait_managed_done(store, second_normal)

    cancel_thread = f"{thread_prefix}-cancel"
    cancelled = await engine.create_run(thread_id=cancel_thread, messages=[{"role": "user", "content": "slow"}])
    await _wait_for_status(store, cancelled.run_id, {"running"})
    await engine.cancel(cancelled.run_id)
    await _wait_managed_done(store, cancelled)

    approve_thread = f"{thread_prefix}-approve"
    approved = await engine.create_run(thread_id=approve_thread, messages=[{"role": "user", "content": "approval"}])
    waiting = await _wait_for_status(store, approved.run_id, {"waiting_approval"})
    await engine.approve(approved.run_id)
    await _wait_managed_done(store, approved)

    reject_thread = f"{thread_prefix}-reject"
    rejected = await engine.create_run(thread_id=reject_thread, messages=[{"role": "user", "content": "approval"}])
    await _wait_for_status(store, rejected.run_id, {"waiting_approval"})
    await engine.reject(rejected.run_id)
    await _wait_managed_done(store, rejected)

    runs = {
        "normal": _snapshot(store, projector, normal.run_id),
        "second_normal": _snapshot(store, projector, second_normal.run_id),
        "cancelled": _snapshot(store, projector, cancelled.run_id),
        "approved": _snapshot(store, projector, approved.run_id),
        "rejected": _snapshot(store, projector, rejected.run_id),
    }
    context = memory.build_context(
        thread_id=normal_thread,
        run_id=second_normal.run_id,
        messages=[{"role": "user", "content": "normal second"}],
        goal="normal second",
    )

    checks = {
        "normal_completed": runs["normal"]["run"].get("status") == "completed",
        "normal_events": _has_types(
            runs["normal"]["events"],
            {"run_created", "run_started", "plan_created", "token", "tool_start", "tool_end", "done", "memory_run_summary", "memory_thread_summary"},
        ),
        "second_run_reads_thread_summary": bool((context.episodic.get("thread_summary") or {}).get("recent_goals")),
        "cancelled_status": runs["cancelled"]["run"].get("status") == "cancelled",
        "cancelled_timeline": _has_types(runs["cancelled"]["events"], {"cancel_requested", "cancelled"}),
        "approval_waiting_seen": waiting.get("status") == "waiting_approval",
        "approved_completed": runs["approved"]["run"].get("status") == "completed",
        "approved_projection": any(item.get("kind") == "approval" and item.get("status") == "approved" for item in runs["approved"]["timeline"]["items"]),
        "approved_memory": _has_types(runs["approved"]["events"], {"memory_run_summary", "memory_thread_summary"}),
        "rejected_completed": runs["rejected"]["run"].get("status") == "completed",
        "rejected_no_tool": not any(event["type"] == "tool_start" for event in runs["rejected"]["events"]),
        "rejected_token": any(
            event["type"] == "token" and "rejected" in str(event.get("payload", {}).get("text", "")).lower()
            for event in runs["rejected"]["events"]
        ),
        "all_terminal_have_timeline": all(run_data["timeline"]["items"] for run_data in runs.values()),
        "tool_audit_contract": all(
            _tool_audit_ok(run_data["events"])
            for key, run_data in runs.items()
            if key not in {"cancelled", "rejected"}
        ),
        "tool_audit_sanitized": all(
            not audit_payload_has_secret(event.get("payload"))
            for run_data in runs.values()
            for event in run_data["events"]
            if event["type"] in {"tool_start", "tool_end"}
        ),
    }

    summary = {
        "event_store_path": str(event_store_path),
        "checkpoint_path": str(checkpoint_path),
        "thread_prefix": thread_prefix,
        "run_ids": {key: data["run"]["id"] for key, data in runs.items()},
        "statuses": {key: data["run"].get("status") for key, data in runs.items()},
        "event_types": {key: [event["type"] for event in data["events"]] for key, data in runs.items()},
        "timeline_kinds": {key: [item["kind"] for item in data["timeline"]["items"]] for key, data in runs.items()},
        "thread_summary": context.episodic.get("thread_summary"),
        "checks": checks,
    }
    summary["mvp_runtime_acceptance"] = "PASS" if all(checks.values()) else "FAIL"
    return summary


def verify_live_agent_loop(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    """Call the real FastAPI chat endpoint with `hello agent` and inspect persisted runtime state."""

    if not settings.openai_api_key:
        return {
            "enabled": True,
            "ok": False,
            "error": "OPENAI_API_KEY is not set; live API + LLM agent loop is required for MVP acceptance.",
        }

    settings.agent_event_store_path = str(event_store_path)
    settings.agent_checkpoint_path = str(checkpoint_path)
    settings.langfuse_enabled = False

    from fastapi.testclient import TestClient  # noqa: PLC0415

    from copilot_agent import server as server_module  # noqa: PLC0415

    with TestClient(server_module.app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "thread_id": thread_id,
                "messages": [{"role": "user", "content": "hello agent"}],
                "confirm_dangerous": False,
            },
            timeout=60,
        )
        body = response.text
        parsed = _parse_sse(body)
        meta = next((item["data"] for item in parsed if item["event"] == "meta"), {})
        run_id = str(meta.get("run_id", ""))
        timeline_response = client.get(f"/v1/runs/{run_id}/timeline") if run_id else None
        timeline_json = timeline_response.json() if timeline_response is not None and timeline_response.status_code == 200 else {}

    event_types = [item["event"] for item in parsed]
    events = timeline_json.get("events", [])
    timeline = timeline_json.get("timeline", {})
    run = timeline_json.get("run", {})
    assistant_output = str(timeline.get("assistant_output", ""))
    error_events = [event for event in events if event.get("type") == "error"]
    error_detail = run.get("error") or "; ".join(
        str((event.get("payload") or {}).get("error", "")) for event in error_events
    )
    ok = (
        response.status_code == 200
        and bool(run_id)
        and run.get("status") == "completed"
        and "meta" in event_types
        and "token" in event_types
        and "done" in event_types
        and any(event.get("type") == "token" for event in events)
        and bool((timeline.get("items") or []))
        and bool(assistant_output.strip())
    )
    return {
        "enabled": True,
        "ok": ok,
        "status_code": response.status_code,
        "thread_id": thread_id,
        "run_id": run_id,
        "sse_event_types": event_types,
        "run_status": run.get("status"),
        "persisted_event_types": [event.get("type") for event in events],
        "timeline_item_kinds": [item.get("kind") for item in timeline.get("items", [])],
        "assistant_output_preview": assistant_output[:300],
        "run_error": error_detail,
        "error_events": [event.get("payload") for event in error_events],
        "error": "" if ok else (str(error_detail) or body[:1000]),
    }


def _snapshot(store: EventStore, projector: TimelineProjector, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id) or {}
    events = store.list_run_events(run_id)
    return {"run": run, "events": events, "timeline": projector.project_run(run, events)}


def _has_types(events: list[dict[str, Any]], expected: set[str]) -> bool:
    return expected.issubset({str(event.get("type", "")) for event in events})


def _tool_audit_ok(events: list[dict[str, Any]]) -> bool:
    starts = [event for event in events if event["type"] == "tool_start"]
    ends = [event for event in events if event["type"] == "tool_end"]
    if not starts or len(starts) != len(ends):
        return False
    start_ids = {str(event.get("payload", {}).get("call_id", "")) for event in starts}
    end_ids = {str(event.get("payload", {}).get("call_id", "")) for event in ends}
    if not start_ids or start_ids != end_ids:
        return False
    for event in starts:
        payload = event.get("payload", {})
        if not all(key in payload for key in ("name", "call_id", "category", "risk_level", "requires_approval", "arguments")):
            return False
    for event in ends:
        payload = event.get("payload", {})
        result = payload.get("result", {})
        if not all(key in payload for key in ("name", "call_id", "result", "duration_ms", "success", "error")):
            return False
        if not isinstance(result, dict) or not all(key in result for key in ("success", "data", "error", "metadata", "sanitized")):
            return False
    return True


async def _wait_managed_done(store: EventStore, managed: ManagedRun, *, timeout: float = 4.0) -> dict[str, Any]:
    run = await _wait_for_status(store, managed.run_id, {"completed", "failed", "cancelled"}, timeout=timeout)
    if managed.task is not None:
        await asyncio.wait_for(asyncio.shield(managed.task), timeout=timeout)
    return run


async def _wait_for_status(store: EventStore, run_id: str, statuses: set[str], *, timeout: float = 4.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = store.get_run(run_id) or {}
        if str(run.get("status", "")) in statuses:
            return run
        await asyncio.sleep(0.05)
    return store.get_run(run_id) or {}


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role", "")).lower() == "user":
            return str(message.get("content", ""))
    return ""


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_sse(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event_type = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        data_raw = "\n".join(data_lines)
        try:
            data = json.loads(data_raw or "{}")
        except json.JSONDecodeError:
            data = {"raw": data_raw}
        out.append({"event": event_type, "data": data})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent MVP runtime acceptance.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-prefix", default=f"mvp-runtime-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/mvp-runtime-acceptance-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    checkpoint_path = Path(args.checkpoint_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, checkpoint_path, args.thread_prefix))
    live_summary = verify_live_agent_loop(
        event_store_path,
        checkpoint_path,
        thread_id=f"{args.thread_prefix}-live-hello",
    )
    summary["live_agent_loop"] = live_summary
    if not live_summary.get("ok"):
        summary["mvp_runtime_acceptance"] = "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"event_store_path={summary['event_store_path']}")
    print(f"checkpoint_path={summary['checkpoint_path']}")
    print(f"statuses={json.dumps(summary['statuses'], ensure_ascii=False)}")
    for key, value in summary["checks"].items():
        print(f"{key}={value}")
    print(f"live_agent_loop={live_summary.get('ok')}")
    print(f"live_agent_run_id={live_summary.get('run_id', '')}")
    print(f"live_agent_sse_events={','.join(str(x) for x in live_summary.get('sse_event_types', []))}")
    if live_summary.get("error"):
        print(f"live_agent_error={live_summary.get('error')}")
    print(f"summary_json={summary_path}")
    print(f"mvp_runtime_acceptance={summary['mvp_runtime_acceptance']}")

    return 0 if summary["mvp_runtime_acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
