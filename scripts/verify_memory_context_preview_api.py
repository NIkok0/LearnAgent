#!/usr/bin/env python
"""Verify memory explainability and side-effect-free context preview APIs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("COPILOT_CAPABILITIES", "rag,http")
os.environ.setdefault("SCENARIO", "minimal")

from fastapi.testclient import TestClient  # noqa: E402

from copilot_agent.memory.item_schema import MemoryScope, MemoryType  # noqa: E402
from copilot_agent.memory.item_writer import MemoryItemWriter  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent import server  # noqa: E402


def main() -> int:
    with TestClient(server.app) as client:
        thread_resp = client.post("/v1/threads", json={"title": "memory context preview verify"})
        thread_resp.raise_for_status()
        thread_id = str(thread_resp.json()["id"])

        assert server.runner is not None
        memory = server.runner.memory
        item_store = memory._item_store
        assert item_store is not None
        user_id = memory.resolve_user_id(thread_id)
        writer = MemoryItemWriter(item_store, policy=MemoryPolicyConfig(enabled=True, long_term_enabled=True))
        active = writer.upsert_candidate(
            user_id=user_id,
            thread_id=thread_id,
            candidate={
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.FACT,
                "content": "User is investigating QUEUED watermark tasks.",
                "importance": 0.9,
                "confidence": 0.95,
                "pending_confirmation": False,
            },
        )
        pending = writer.upsert_candidate(
            user_id=user_id,
            thread_id=thread_id,
            candidate={
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.PREFERENCE,
                "content": "User may prefer terse incident summaries.",
                "importance": 0.9,
                "confidence": 0.4,
                "pending_confirmation": True,
            },
        )
        writer.upsert_candidate(
            user_id=user_id,
            thread_id=thread_id,
            candidate={
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.FACT,
                "content": "Unrelated calendar note about lunch planning.",
                "importance": 0.8,
                "confidence": 0.9,
                "pending_confirmation": False,
            },
        )

        before_events = client.get(f"/v1/threads/{thread_id}/events")
        before_events.raise_for_status()
        before_count = len(before_events.json()["events"])

        preview_resp = client.post(
            f"/v1/threads/{thread_id}/context/preview",
            json={"messages": [{"role": "user", "content": "QUEUED watermark task 怎么排查？"}]},
        )
        preview_resp.raise_for_status()
        preview = preview_resp.json()

        after_events = client.get(f"/v1/threads/{thread_id}/events")
        after_events.raise_for_status()
        after_count = len(after_events.json()["events"])
        preview_access_unchanged = True
        if active.item is not None:
            after_preview_item = item_store.get(active.item.id)
            preview_access_unchanged = bool(after_preview_item and after_preview_item.access_count == 0)

        memory_resp = client.get(
            f"/v1/threads/{thread_id}/memory",
            params={"goal": "QUEUED watermark task 怎么排查？"},
        )
        memory_resp.raise_for_status()
        memory_payload = memory_resp.json()

        pending_resp = client.get(f"/v1/threads/{thread_id}/memory/items", params={"status": "pending"})
        pending_resp.raise_for_status()
        pending_items = pending_resp.json()["items"]

        confirm_ok = True
        if pending.item is not None:
            confirm_resp = client.post(f"/v1/threads/{thread_id}/memory/items/{pending.item.id}/confirm")
            confirm_resp.raise_for_status()
            confirm_ok = confirm_resp.json()["item"]["pending_confirmation"] is False

        reject_ok = True
        if active.item is not None:
            reject_resp = client.post(
                f"/v1/threads/{thread_id}/memory/items/{active.item.id}/reject",
                json={"reason": "verify_api"},
            )
            reject_resp.raise_for_status()
            reject_ok = reject_resp.json()["item"]["is_deprecated"] is True

    context = preview.get("context") or {}
    truncation_report = context.get("truncation_report") or {}
    side_effects = truncation_report.get("side_effects") or {}
    checks = {
        "context_preview_dry_run": preview.get("dry_run") is True and truncation_report.get("dry_run") is True,
        "context_preview_no_events": before_count == after_count,
        "context_preview_no_checkpoint_persist": side_effects.get("checkpoint_persisted") is False,
        "context_preview_no_memory_access_touch": preview_access_unchanged,
        "memory_preview_recalled_long_term": len(memory_payload.get("recalled_long_term") or []) >= 1,
        "memory_preview_explainability": isinstance(memory_payload.get("explainability"), dict),
        "memory_preview_dropped_reason": any(
            isinstance(item, dict) and item.get("reason")
            for item in memory_payload.get("dropped_long_term") or []
        ),
        "context_preview_memory_context_prompt": any(
            isinstance(item, dict)
            and str(item.get("content", "")).startswith("[MemoryContext]")
            and "Relevant facts:" in str(item.get("content", ""))
            for item in context.get("graph_messages_preview") or []
        ),
        "pending_items_listed": len(pending_items) >= 1,
        "confirm_memory_item_api": confirm_ok,
        "reject_memory_item_api": reject_ok,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "memory_context_preview_api",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
    }
    summary_path = ROOT / "artifacts/runtime/memory-context-preview-api-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_memory_context_preview_api={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
