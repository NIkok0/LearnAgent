#!/usr/bin/env python
"""Export a local debug bundle for one run.

The bundle is intentionally local-only: raw events may contain user text and
tool payloads. It does not call LangGraph or reconstruct graph state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.side_effects import build_side_effect_read_model  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def build_debug_bundle(
    *,
    event_store_path: Path,
    checkpoint_path: Path,
    run_id: str,
) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    run = store.get_run(run_id)
    if run is None:
        raise SystemExit(f"run not found: {run_id}")
    thread_id = str(run.get("thread_id") or "")
    thread = store.get_thread(thread_id) if thread_id else None
    events = store.list_run_events(run_id)
    timeline = TimelineProjector().project_run(run, events)
    side_effects = build_side_effect_read_model(run, events)
    return {
        "run": run,
        "thread": thread,
        "events": events,
        "timeline": timeline,
        "side_effects": side_effects["side_effects"],
        "side_effect_summary": side_effects["summary"],
        "side_effect_warnings": side_effects["warnings"],
        "latest_run_consistency": _latest_payload(events, "run_consistency_checked"),
        "latest_checkpoint_consistency": _latest_payload(events, "checkpoint_consistency_checked"),
        "checkpoint_raw": inspect_checkpoint_sqlite(checkpoint_path, thread_id=thread_id),
    }


def inspect_checkpoint_sqlite(checkpoint_path: Path, *, thread_id: str) -> dict[str, Any]:
    path = checkpoint_path.expanduser()
    out: dict[str, Any] = {
        "path": str(path),
        "db_exists": path.is_file(),
        "thread_id": thread_id,
        "tables": [],
        "row_counts_by_table": {},
        "has_thread": False,
        "latest_checkpoint_preview": None,
        "error": None,
    }
    if not path.is_file():
        return out
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            tables = [
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            out["tables"] = tables
            counts: dict[str, int] = {}
            for table in tables:
                if not _safe_identifier(table):
                    continue
                try:
                    row = conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()
                    counts[table] = int(row["count"] if row is not None else 0)
                except sqlite3.DatabaseError:
                    continue
            out["row_counts_by_table"] = counts
            if "checkpoints" not in tables:
                return out
            has_thread = conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (thread_id,),
            ).fetchone()
            out["has_thread"] = has_thread is not None
            row = conn.execute(
                """
                SELECT *
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY checkpoint_ns DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            out["latest_checkpoint_preview"] = _row_preview(row) if row is not None else None
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _latest_payload(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if str(event.get("type") or "") != event_type:
            continue
        payload = event.get("payload")
        return payload if isinstance(payload, dict) else {}
    return None


def _safe_identifier(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char == "_" for char in value)


def _row_preview(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _preview_value(row[key]) for key in row.keys()}


def _preview_value(value: Any, *, limit: int = 240) -> Any:
    if isinstance(value, bytes):
        text = value[: min(len(value), 48)].hex()
        suffix = "..." if len(value) > 48 else ""
        return {"type": "bytes", "size": len(value), "preview_hex": text + suffix}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a run debug bundle JSON.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    bundle = build_debug_bundle(
        event_store_path=Path(args.event_store_path),
        checkpoint_path=Path(args.checkpoint_path),
        run_id=str(args.run_id),
    )
    output_json = Path(args.output_json) if args.output_json else (
        ROOT / "artifacts/runtime/debug-bundles" / f"run-{args.run_id}.json"
    )
    output_json = output_json.resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(bundle, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"debug_bundle={output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
