#!/usr/bin/env python
"""Optional live LLM E2E acceptance for the local Agent loop.

This is intentionally not part of default CI. It proves that a real
OpenAI-compatible provider can execute:

hello agent -> SSE token stream -> done -> llm_generation ->
run_completed_meta -> timeline projection.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.settings import settings  # noqa: E402


def verify_live(
    *,
    event_store_path: Path,
    checkpoint_path: Path,
    thread_id: str,
    message: str,
    require_live: bool,
) -> dict[str, Any]:
    if not (settings.openai_api_key or "").strip():
        return {
            "status": "FAIL" if require_live else "SKIP",
            "ok": False,
            "skipped": not require_live,
            "reason": "OPENAI_API_KEY_not_set",
        }

    settings.agent_event_store_path = str(event_store_path)
    settings.agent_checkpoint_path = str(checkpoint_path)
    settings.observability_provider = "none"
    settings.langfuse_enabled = False

    try:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from copilot_agent import server as server_module  # noqa: PLC0415
    except RuntimeError as exc:
        reason = f"server_import_failed:{exc}"
        return {
            "status": "FAIL" if require_live else "SKIP",
            "ok": False,
            "skipped": not require_live,
            "reason": reason,
        }

    with TestClient(server_module.app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "thread_id": thread_id,
                "messages": [{"role": "user", "content": message}],
                "confirm_dangerous": False,
            },
            timeout=90,
        )
        sse = _parse_sse(response.text)
        meta = next((item["data"] for item in sse if item["event"] == "meta"), {})
        run_id = str(meta.get("run_id") or "")
        timeline_response = client.get(f"/v1/runs/{run_id}/timeline", timeout=30) if run_id else None
        timeline_json = (
            timeline_response.json()
            if timeline_response is not None and timeline_response.status_code == 200
            else {}
        )

    timeline = timeline_json.get("timeline") if isinstance(timeline_json.get("timeline"), dict) else {}
    events = timeline_json.get("events") if isinstance(timeline_json.get("events"), list) else []
    run = timeline_json.get("run") if isinstance(timeline_json.get("run"), dict) else {}
    event_types = [str(event.get("type") or "") for event in events if isinstance(event, dict)]
    sse_types = [item["event"] for item in sse]
    final_answer_item = next(
        (
            item
            for item in timeline.get("items", [])
            if isinstance(item, dict) and item.get("kind") == "final_answer"
        ),
        {},
    )
    checks = {
        "http_ok": response.status_code == 200,
        "meta_run_id": bool(run_id),
        "sse_has_token": "token" in sse_types,
        "sse_has_done": "done" in sse_types,
        "run_completed": run.get("status") == "completed",
        "persisted_token": "token" in event_types,
        "llm_generation_written": "llm_generation" in event_types,
        "run_completed_meta_written": "run_completed_meta" in event_types,
        "timeline_output": bool(str(timeline.get("assistant_output") or "").strip()),
        "timeline_final_answer": bool(final_answer_item),
        "timeline_observability": (timeline.get("observability") or {}).get("llm_rounds", 0) >= 1,
    }
    ok = all(checks.values())
    error_events = [
        event.get("payload")
        for event in events
        if isinstance(event, dict) and event.get("type") == "error"
    ]
    return {
        "status": "PASS" if ok else "FAIL",
        "ok": ok,
        "skipped": False,
        "thread_id": thread_id,
        "run_id": run_id,
        "http_status": response.status_code,
        "checks": checks,
        "sse_event_types": sse_types,
        "persisted_event_types": event_types,
        "run_status": run.get("status"),
        "assistant_output_preview": str(timeline.get("assistant_output") or "")[:400],
        "observability": timeline.get("observability"),
        "cost": timeline.get("cost"),
        "final_answer": final_answer_item.get("payload") if isinstance(final_answer_item, dict) else None,
        "run_error": run.get("error"),
        "error_events": error_events,
    }


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
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {"raw": raw}
        out.append({"event": event_type, "data": data})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Run optional live LLM E2E acceptance.")
    parser.add_argument("--event-store-path", default="storage/verify-live-llm-e2e-events.sqlite")
    parser.add_argument("--checkpoint-path", default="storage/verify-live-llm-e2e-checkpoints.sqlite")
    parser.add_argument("--thread-id", default=f"live-llm-e2e-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--message", default="hello agent")
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/live-llm-e2e-acceptance-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    checkpoint_path = Path(args.checkpoint_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    summary = verify_live(
        event_store_path=event_store_path,
        checkpoint_path=checkpoint_path,
        thread_id=str(args.thread_id),
        message=str(args.message),
        require_live=bool(args.require_live),
    )
    summary["event_store_path"] = str(event_store_path)
    summary["checkpoint_path"] = str(checkpoint_path)
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"live_llm_e2e_acceptance={summary['status']}")
    print(f"run_id={summary.get('run_id', '')}")
    print(f"summary_json={summary_path}")
    if summary.get("reason"):
        print(f"reason={summary['reason']}")
    return 0 if summary["status"] in {"PASS", "SKIP"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
