#!/usr/bin/env python
"""Export the latest RAG deletion proof for one document id."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_schema import EVENT_RAG_DOCUMENT_DELETE_PROOF  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def export_proof(*, event_store_path: Path, doc_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    event = store.find_latest_event_by_type_and_payload(
        EVENT_RAG_DOCUMENT_DELETE_PROOF,
        payload_key="doc_id",
        payload_value=doc_id,
    )
    if event is None:
        return {
            "status": "NOT_FOUND",
            "doc_id": doc_id,
            "event_store_path": str(event_store_path),
        }
    return {
        "status": "FOUND",
        "doc_id": doc_id,
        "event_store_path": str(event_store_path),
        "proof": event.get("payload") or {},
        "event": event,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a RAG document deletion proof JSON.")
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    summary = export_proof(event_store_path=Path(args.event_store_path).resolve(), doc_id=str(args.doc_id))
    if args.output_json:
        output_path = Path(args.output_json).resolve()
    else:
        safe_doc_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(args.doc_id))
        output_path = ROOT / "artifacts" / "runtime" / f"rag-deletion-proof-{safe_doc_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"rag_deletion_proof={summary['status']}")
    print(f"output_json={output_path}")
    return 0 if summary["status"] == "FOUND" else 1


if __name__ == "__main__":
    raise SystemExit(main())
