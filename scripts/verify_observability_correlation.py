#!/usr/bin/env python
"""Verify trace_id correlation and run_completed_meta token fields."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.settings import settings  # noqa: E402
from scripts._verify_helpers import build_verify_fixture, close_fixture, collect_runtime_events  # noqa: E402


class MessageState(TypedDict):
    messages: Annotated[list, add_messages]


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, object]:
    thread_id = f"{thread_prefix}-obs"
    fixture = None

    try:
        graph = StateGraph(MessageState)

        async def echo_node(state: MessageState) -> MessageState:
            last = state["messages"][-1]
            if isinstance(last, HumanMessage):
                return {
                    "messages": [
                        AIMessage(
                            content="observability ok",
                            response_metadata={
                                "token_usage": {
                                    "prompt_tokens": 11,
                                    "completion_tokens": 7,
                                    "total_tokens": 18,
                                }
                            },
                        )
                    ]
                }
            return {}

        graph.add_node("echo", echo_node)
        graph.set_entry_point("echo")
        graph.add_edge("echo", END)
        fixture = build_verify_fixture(
            event_store_path=event_store_path,
            checkpoint_path=checkpoint_path,
            thread_id=thread_id,
            graph=graph,
        )
        config = {
            "configurable": {
                "thread_id": thread_id,
                "trace": None,
                "trace_id": f"local-{thread_id}-{fixture.run_id}",
            }
        }

        events = await collect_runtime_events(
            fixture,
            graph_input={"messages": [HumanMessage(content="trace check")]},
            graph_config=config,
        )
        completed_meta = next((event for event in events if event["type"] == "run_completed_meta"), {})
        done_event = next((event for event in events if event["type"] == "done"), {})
        payload = completed_meta.get("payload") if isinstance(completed_meta.get("payload"), dict) else {}
        done_payload = done_event.get("payload") if isinstance(done_event.get("payload"), dict) else {}

        return {
            "trace_id_present": all(
                bool(event.get("trace_id")) for event in events if event["type"] in {"token", "done"}
            ),
            "trace_id_value": next((event.get("trace_id") for event in events if event.get("trace_id")), ""),
            "completed_meta_tokens": {
                "llm_rounds": payload.get("llm_rounds"),
                "prompt_tokens": payload.get("prompt_tokens"),
                "completion_tokens": payload.get("completion_tokens"),
                "total_tokens": payload.get("total_tokens"),
            },
            "done_has_final_answer": isinstance(done_payload.get("final_answer"), dict),
        }
    finally:
        await close_fixture(fixture)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify observability correlation fields.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-prefix", default=f"obs-correlation-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/observability-correlation-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).with_name(
        f"{Path(args.event_store_path).stem}-obs-correlation.sqlite"
    )
    checkpoint_path = Path(args.checkpoint_path).with_name(
        f"{Path(args.checkpoint_path).stem}-obs-correlation.sqlite"
    )
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    summary = asyncio.run(verify(event_store_path, checkpoint_path, args.thread_prefix))
    tokens = summary["completed_meta_tokens"] if isinstance(summary["completed_meta_tokens"], dict) else {}
    checks = {
        "trace_id_present": bool(summary.get("trace_id_present")),
        "trace_id_local_prefix": str(summary.get("trace_id_value", "")).startswith("local-"),
        "token_fields_present": all(
            key in tokens for key in ("llm_rounds", "prompt_tokens", "completion_tokens", "total_tokens")
        ),
        "done_has_final_answer": bool(summary.get("done_has_final_answer")),
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["status"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"observability_correlation={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
