#!/usr/bin/env python
"""Export the latest memory item deletion proof for one thread/item."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_schema import EVENT_MEMORY_ITEM_DELETE_PROOF  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def export_proof(*, event_store_path: Path, thread_id: str, item_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    thread = store.get_thread(thread_id)
    if thread is None:
        raise SystemExit(f"thread not found: {thread_id}")
    proof = latest_memory_deletion_proof(store, thread_id=thread_id, item_id=item_id)
    if proof is None:
        return {"status": "NOT_FOUND", "thread_id": thread_id, "item_id": item_id, "proof": None}
    return {"status": "FOUND", "thread_id": thread_id, "item_id": item_id, "proof": proof}


def latest_memory_deletion_proof(store: EventStore, *, thread_id: str, item_id: str) -> dict[str, Any] | None:
    for event in reversed(store.list_events(thread_id)):
        if str(event.get("type") or "") != EVENT_MEMORY_ITEM_DELETE_PROOF:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if str(payload.get("item_id") or "") == item_id:
            return payload
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a memory item deletion proof JSON.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    summary = export_proof(
        event_store_path=Path(args.event_store_path).resolve(),
        thread_id=str(args.thread_id),
        item_id=str(args.item_id),
    )
    output_json = Path(args.output_json) if args.output_json else (
        ROOT / "artifacts/runtime" / f"memory-deletion-proof-{_safe_name(args.item_id)}.json"
    )
    output_json = output_json.resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"memory_deletion_proof={summary['status']}")
    print(f"summary_json={output_json}")
    return 0 if summary["status"] == "FOUND" else 1


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:96] or "item"


if __name__ == "__main__":
    raise SystemExit(main())
