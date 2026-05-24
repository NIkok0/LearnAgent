#!/usr/bin/env python
"""Verify local llm_generation events and timeline cost summary."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.stream.event_mapper import GraphEventMapper  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class FakeGraph:
    async def astream_events(self, graph_input: Any, config: dict[str, Any], version: str):
        message = AIMessage(
            content="cost ok",
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                },
                "finish_reason": "stop",
            },
        )
        yield {
            "event": "on_chat_model_start",
            "run_id": "llm-run-1",
            "data": {"input": {"messages": graph_input.get("messages", [])}},
        }
        yield {
            "event": "on_chat_model_end",
            "run_id": "llm-run-1",
            "data": {"output": message},
        }


class FakeCheckpointReader:
    async def snapshot(self, thread_id: str) -> dict[str, Any]:
        return {
            "checkpoint_thread_id": thread_id,
            "message_count": 2,
            "has_interrupt": False,
        }

    async def state_values(self, thread_id: str) -> dict[str, Any]:
        return {
            "messages": [HumanMessage(content="cost check"), AIMessage(content="cost ok")],
            "tool_route": {"kind": "chat"},
        }


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, object]:
    old_model = settings.openai_model
    old_provider = settings.openai_provider
    settings.openai_model = "gpt-4o-mini"
    settings.openai_provider = "openai-compatible"
    thread_id = f"{thread_prefix}-cost"
    try:
        store = EventStore(str(event_store_path))
        run = store.create_run(thread_id)
        run_id = str(run["id"])
        store.update_run_status(run_id, RUN_STATUS_RUNNING)
        memory = MemoryManager(
            rag_store=RagStore([]),
            event_store=store,
            checkpoint_path=str(checkpoint_path),
        )
        mapper = GraphEventMapper(
            memory=memory,
            tool_registry=ToolRegistry(),
            checkpoint_reader=FakeCheckpointReader(),  # type: ignore[arg-type]
        )
        events: list[dict[str, Any]] = []
        async for runtime_event in mapper.map(
            graph=FakeGraph(),
            graph_input={"messages": [HumanMessage(content="cost check")]},
            graph_config={
                "configurable": {
                    "thread_id": thread_id,
                    "trace": None,
                    "trace_id": f"local-{thread_id}-{run_id}",
                    "observability_provider": "none",
                }
            },
            thread_id=thread_id,
            run_id=run_id,
        ):
            payload = runtime_event.to_store_payload()
            memory.append_event(thread_id, run_id, runtime_event.kind, payload)
            events.append(
                {
                    "type": runtime_event.kind,
                    "trace_id": runtime_event.correlation.trace_id,
                    "payload": payload,
                }
            )
        stored_events = store.list_run_events(run_id)
        run = store.get_run(run_id) or {"id": run_id, "thread_id": thread_id, "status": "completed"}
        timeline = TimelineProjector().project_run(run, stored_events)
        generation = next((event for event in events if event["type"] == "llm_generation"), {})
        completed_meta = next((event for event in events if event["type"] == "run_completed_meta"), {})
        gen_payload = generation.get("payload") if isinstance(generation.get("payload"), dict) else {}
        meta_payload = completed_meta.get("payload") if isinstance(completed_meta.get("payload"), dict) else {}
        return {
            "event_types": [event["type"] for event in events],
            "generation": gen_payload,
            "completed_meta": meta_payload,
            "timeline_observability": timeline.get("observability"),
            "timeline_cost": timeline.get("cost"),
            "timeline_has_observability_item": any(
                item.get("kind") == "observability" for item in timeline.get("items", [])
            ),
        }
    finally:
        settings.openai_model = old_model
        settings.openai_provider = old_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Observability / Cost v1.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-prefix", default=f"obs-cost-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/observability-cost-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).with_name(
        f"{Path(args.event_store_path).stem}-obs-cost.sqlite"
    )
    checkpoint_path = Path(args.checkpoint_path).with_name(
        f"{Path(args.checkpoint_path).stem}-obs-cost.sqlite"
    )
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    summary = asyncio.run(verify(event_store_path, checkpoint_path, args.thread_prefix))
    generation = summary.get("generation") if isinstance(summary.get("generation"), dict) else {}
    meta = summary.get("completed_meta") if isinstance(summary.get("completed_meta"), dict) else {}
    observability = summary.get("timeline_observability") if isinstance(summary.get("timeline_observability"), dict) else {}
    cost = summary.get("timeline_cost") if isinstance(summary.get("timeline_cost"), dict) else {}
    checks = {
        "llm_generation_written": "llm_generation" in summary.get("event_types", []),
        "generation_token_fields": generation.get("prompt_tokens") == 1000
        and generation.get("completion_tokens") == 500
        and generation.get("total_tokens") == 1500,
        "generation_cost_estimated": generation.get("estimated_cost") is not None,
        "completed_meta_aggregated": meta.get("llm_rounds") == 1 and meta.get("total_tokens") == 1500,
        "timeline_observability_summary": observability.get("total_tokens") == 1500,
        "timeline_cost_summary": cost.get("estimated_cost") is not None,
        "timeline_item_present": bool(summary.get("timeline_has_observability_item")),
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["status"] = "PASS" if passed else "FAIL"
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"observability_cost_v1={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
