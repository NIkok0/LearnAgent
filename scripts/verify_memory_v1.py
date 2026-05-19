#!/usr/bin/env python
"""Verify MemoryManager v1 summaries and three-layer context."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent MemoryManager v1.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-id", default=f"memory-v1-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/memory-v1-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(event_store_path))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="Redis stream deployment memory guide")])
    memory = MemoryManager(
        rag_store=rag,
        event_store=store,
        checkpoint_path=str(Path(args.checkpoint_path).resolve()),
    )

    run = store.create_run(args.thread_id)
    run_id = str(run["id"])
    memory.append_event(args.thread_id, run_id, "plan_created", {"goal": "check redis stream status"})
    memory.append_event(args.thread_id, run_id, "token", {"text": "Redis stream status should be checked with docs and health APIs."})
    memory.append_event(
        args.thread_id,
        run_id,
        "tool_start",
        {
            "name": "search_docs",
            "category": "memory",
            "risk_level": "low",
            "arguments": {"query": "Redis stream"},
        },
    )
    memory.append_event(args.thread_id, run_id, "tool_end", {"name": "search_docs", "success": True})
    memory.append_event(args.thread_id, run_id, "done", {})
    store.complete_run(run_id)

    run_summary = memory.summarize_run(args.thread_id, run_id)
    thread_summary = memory.update_thread_summary(args.thread_id, run_id)
    latest_thread_summary = memory.get_thread_summary(args.thread_id)
    context = memory.build_context(
        thread_id=args.thread_id,
        run_id=run_id,
        messages=[{"role": "user", "content": "check redis stream status"}],
        goal="check redis stream status",
    )
    events = store.list_run_events(run_id)

    checks = {
        "run_summary_event": any(event["type"] == "memory_run_summary" for event in events),
        "thread_summary_event": any(event["type"] == "memory_thread_summary" for event in events),
        "run_summary_sources": bool(run_summary.get("source_event_ids")),
        "thread_summary_sources": bool(thread_summary.get("source_run_ids")),
        "latest_thread_summary": latest_thread_summary == thread_summary,
        "context_working": context.working.get("thread_id") == args.thread_id,
        "context_semantic": context.semantic.get("rag_chunks") == 1,
        "context_episodic": bool(context.episodic.get("thread_summary")),
    }
    passed = all(checks.values())
    summary = {
        "thread_id": args.thread_id,
        "run_id": run_id,
        "event_store_path": str(event_store_path),
        "event_types": [event["type"] for event in events],
        "run_summary": run_summary,
        "thread_summary": thread_summary,
        "checks": checks,
        "memory_v1": "PASS" if passed else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"thread_id={summary['thread_id']}")
    print(f"run_id={summary['run_id']}")
    print(f"event_store_path={summary['event_store_path']}")
    print(f"event_types={','.join(summary['event_types'])}")
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"memory_v1={summary['memory_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
