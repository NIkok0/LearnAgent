#!/usr/bin/env python
"""Verify archived threads are readable but cannot create new runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent import server  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


async def verify(event_store_path: Path) -> dict[str, object]:
    server.event_store = server.EventStore(str(event_store_path))
    server.execution_engine = None
    old_env_key = os.environ.pop("OPENAI_API_KEY", None)
    old_settings_key = settings.openai_api_key
    settings.openai_api_key = ""
    transport = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            thread_res = await client.post("/v1/threads", json={"title": "archive api verification"})
            thread_res.raise_for_status()
            thread = thread_res.json()
            thread_id = str(thread["id"])

            end_thread_res = await client.post("/v1/threads", json={"title": "end api verification"})
            end_thread_res.raise_for_status()
            end_thread_id = str(end_thread_res.json()["id"])
            end_res = await client.post(f"/v1/threads/{end_thread_id}/end", json={"reason": "explicit"})
            end_read_res = await client.get(f"/v1/threads/{end_thread_id}")

            active_res = await client.post("/v1/threads", json={"title": "active no key verification"})
            active_res.raise_for_status()
            active_thread_id = str(active_res.json()["id"])
            active_run_res = await client.post(
                f"/v1/threads/{active_thread_id}/runs",
                json={"messages": [{"role": "user", "content": "needs key"}]},
            )

            archive_res = await client.post(f"/v1/threads/{thread_id}/archive")
            read_res = await client.get(f"/v1/threads/{thread_id}")
            run_res = await client.post(
                f"/v1/threads/{thread_id}/runs",
                json={"messages": [{"role": "user", "content": "blocked"}]},
            )
            chat_res = await client.post(
                "/v1/chat",
                json={"thread_id": thread_id, "messages": [{"role": "user", "content": "blocked"}]},
            )
    finally:
        settings.openai_api_key = old_settings_key
        if old_env_key is not None:
            os.environ["OPENAI_API_KEY"] = old_env_key

    archived = archive_res.json().get("thread", {})
    readable = read_res.json().get("thread", {})
    ended = end_res.json().get("thread", {})
    ended_readable = end_read_res.json().get("thread", {})
    return {
        "thread_id": thread_id,
        "end_thread_id": end_thread_id,
        "end_status_code": end_res.status_code,
        "end_status": ended.get("status"),
        "end_ended_at": ended.get("ended_at"),
        "end_reason": ended.get("end_reason"),
        "end_read_status_code": end_read_res.status_code,
        "end_read_status": ended_readable.get("status"),
        "archive_status_code": archive_res.status_code,
        "archive_status": archived.get("status"),
        "archive_archived_at": archived.get("archived_at"),
        "read_status_code": read_res.status_code,
        "read_status": readable.get("status"),
        "run_status_code": run_res.status_code,
        "run_detail": _detail(run_res),
        "chat_status_code": chat_res.status_code,
        "chat_detail": _detail(chat_res),
        "active_no_key_run_status_code": active_run_res.status_code,
        "active_no_key_run_detail": _detail(active_run_res),
    }


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text
    return str(data.get("detail", ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify archived thread API behavior.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/thread-archive-api-summary.json"),
        help="Path to write summary JSON.",
    )
    args = parser.parse_args()
    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path))
    passed = (
        summary["end_status_code"] == 200
        and summary["end_status"] == "ended"
        and bool(summary["end_ended_at"])
        and summary["end_reason"] == "explicit"
        and summary["end_read_status_code"] == 200
        and summary["end_read_status"] == "ended"
        and summary["archive_status_code"] == 200
        and summary["archive_status"] == "archived"
        and bool(summary["archive_archived_at"])
        and summary["read_status_code"] == 200
        and summary["read_status"] == "archived"
        and summary["run_status_code"] == 409
        and summary["run_detail"] == "thread is not active"
        and summary["chat_status_code"] == 409
        and summary["chat_detail"] == "thread is not active"
        and summary["active_no_key_run_status_code"] == 503
        and summary["active_no_key_run_detail"] == "OPENAI_API_KEY is not set"
    )
    summary["checks"] = {
        "end_thread": summary["end_status_code"] == 200 and summary["end_status"] == "ended",
        "archive_thread": summary["archive_status_code"] == 200 and summary["archive_status"] == "archived",
        "archived_readable": summary["read_status_code"] == 200 and summary["read_status"] == "archived",
        "archived_blocks_run": summary["run_status_code"] == 409 and summary["run_detail"] == "thread is not active",
        "archived_blocks_chat": summary["chat_status_code"] == 409 and summary["chat_detail"] == "thread is not active",
        "active_thread_still_requires_key": summary["active_no_key_run_status_code"] == 503
        and summary["active_no_key_run_detail"] == "OPENAI_API_KEY is not set",
    }
    summary["thread_archive_api"] = "PASS" if passed else "FAIL"
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for key, value in summary.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"thread_archive_api={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
