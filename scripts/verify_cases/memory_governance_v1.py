#!/usr/bin/env python
"""Verify memory governance deletion audit and proof export."""

from __future__ import annotations

import json
import os
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("COPILOT_CAPABILITIES", "rag,http")
os.environ.setdefault("SCENARIO", "minimal")

from fastapi.testclient import TestClient  # noqa: E402

from copilot_agent import server  # noqa: E402
from copilot_agent.contracts.events.registry import PayloadValidationError, validate_payload_for_kind  # noqa: E402
from copilot_agent.memory.item_schema import MemoryScope, MemoryType  # noqa: E402
from copilot_agent.memory.item_writer import MemoryItemWriter  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent.runtime.event_schema import (  # noqa: E402
    EVENT_MEMORY_ITEM_CONFIRMED,
    EVENT_MEMORY_ITEM_DELETED,
    EVENT_MEMORY_ITEM_DELETE_PROOF,
    EVENT_MEMORY_ITEM_REJECTED,
)
from scripts.export_memory_deletion_proof import export_proof  # noqa: E402
from scripts.export_run_debug_bundle import build_debug_bundle  # noqa: E402


SECRET_TEXT = "secret-cookie-token"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify memory governance deletion audit and proof export.")
    parser.add_argument("--include-subprocess-export", action="store_true")
    args = parser.parse_args(argv)
    with TestClient(server.app) as client:
        thread_resp = client.post("/v1/threads", json={"title": "memory governance verify"})
        thread_resp.raise_for_status()
        thread_id = str(thread_resp.json()["id"])

        other_resp = client.post("/v1/threads", json={"title": "other memory user"})
        other_resp.raise_for_status()
        other_thread_id = str(other_resp.json()["id"])

        assert server.runner is not None
        assert server.rag_manager is not None
        memory = server.runner.memory
        item_store = memory._item_store
        assert item_store is not None
        user_id = memory.resolve_user_id(thread_id)
        other_user_id = memory.resolve_user_id(other_thread_id)
        writer = MemoryItemWriter(item_store, policy=MemoryPolicyConfig(enabled=True, long_term_enabled=True))

        active = writer.upsert_candidate(
            user_id=user_id,
            thread_id=thread_id,
            candidate={
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.FACT,
                "content": f"User keeps QUEUED incident notes with {SECRET_TEXT}.",
                "importance": 0.92,
                "confidence": 0.96,
                "pending_confirmation": False,
            },
        )
        pending = writer.upsert_candidate(
            user_id=user_id,
            thread_id=thread_id,
            candidate={
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.PREFERENCE,
                "content": "User prefers explicit governance audit trails.",
                "importance": 0.88,
                "confidence": 0.4,
                "pending_confirmation": True,
            },
        )
        other = writer.upsert_candidate(
            user_id=other_user_id,
            thread_id=other_thread_id,
            candidate={
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.FACT,
                "content": "Other user memory must not be deletable.",
                "importance": 0.9,
                "confidence": 0.95,
            },
        )
        assert active.item is not None
        assert pending.item is not None
        assert other.item is not None

        confirm_resp = client.post(f"/v1/threads/{thread_id}/memory/items/{pending.item.id}/confirm")
        confirm_resp.raise_for_status()
        reject_resp = client.post(
            f"/v1/threads/{thread_id}/memory/items/{pending.item.id}/reject",
            json={"reason": "verify_reject"},
        )
        reject_resp.raise_for_status()

        delete_resp = client.request(
            "DELETE",
            f"/v1/threads/{thread_id}/memory/items/{active.item.id}",
            json={"reason": "verify_delete"},
        )
        delete_resp.raise_for_status()
        delete_payload = delete_resp.json()

        cross_delete = client.request(
            "DELETE",
            f"/v1/threads/{thread_id}/memory/items/{other.item.id}",
            json={"reason": "cross_user"},
        )
        missing_delete = client.request(
            "DELETE",
            f"/v1/threads/{thread_id}/memory/items/mem_missing",
            json={"reason": "missing"},
        )

        proof_resp = client.get(f"/v1/threads/{thread_id}/memory/items/{active.item.id}/deletion-proof")
        proof_resp.raise_for_status()
        proof_payload = proof_resp.json()["proof"]

        deprecated_resp = client.get(f"/v1/threads/{thread_id}/memory/items", params={"status": "deprecated"})
        deprecated_resp.raise_for_status()
        deprecated_items = deprecated_resp.json()["items"]

        preview_resp = client.get(f"/v1/threads/{thread_id}/memory", params={"goal": "QUEUED incident notes"})
        preview_resp.raise_for_status()
        preview_payload = preview_resp.json()

        exported = export_proof(
            event_store_path=Path(server.event_store.path),
            thread_id=thread_id,
            item_id=active.item.id,
        )

        governance_run_id = f"memory-governance-{thread_id}"
        bundle = build_debug_bundle(
            event_store_path=Path(server.event_store.path),
            checkpoint_path=Path(server.checkpoint_store.path),
            run_id=governance_run_id,
        )

        script_output = ROOT / "artifacts/runtime/memory-delete-proof-verify.json"
        proc_returncode = 0
        if args.include_subprocess_export:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "export_memory_deletion_proof.py"),
                    "--event-store-path",
                    str(server.event_store.path),
                    "--thread-id",
                    thread_id,
                    "--item-id",
                    active.item.id,
                    "--output-json",
                    str(script_output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            proc_returncode = proc.returncode

        events = server.event_store.list_events(thread_id)
        governance_events = [
            event
            for event in events
            if event.get("type")
            in {
                EVENT_MEMORY_ITEM_CONFIRMED,
                EVENT_MEMORY_ITEM_REJECTED,
                EVENT_MEMORY_ITEM_DELETED,
                EVENT_MEMORY_ITEM_DELETE_PROOF,
            }
        ]
        deleted_event = next(event for event in governance_events if event.get("type") == EVENT_MEMORY_ITEM_DELETED)
        proof_event = next(event for event in governance_events if event.get("type") == EVENT_MEMORY_ITEM_DELETE_PROOF)

    tombstone = item_store.get(active.item.id)
    assert tombstone is not None
    serialized_events = json.dumps(governance_events, ensure_ascii=False, default=str).lower()
    serialized_deprecated = json.dumps(deprecated_items, ensure_ascii=False, default=str).lower()
    serialized_bundle = json.dumps(bundle.get("memory_governance") or [], ensure_ascii=False, default=str).lower()

    contract_ok = True
    try:
        validate_payload_for_kind(EVENT_MEMORY_ITEM_DELETED, deleted_event["payload"])
        validate_payload_for_kind(EVENT_MEMORY_ITEM_DELETE_PROOF, proof_event["payload"])
        validate_payload_for_kind(
            EVENT_MEMORY_ITEM_DELETED,
            {**deleted_event["payload"], "content": SECRET_TEXT},
        )
        contract_ok = False
    except PayloadValidationError:
        contract_ok = contract_ok and True

    checks = {
        "delete_api_tombstones_item": tombstone.is_deprecated is True
        and tombstone.content == "[deleted memory item]"
        and tombstone.embedding is None,
        "deleted_not_recalled": active.item.id not in (preview_payload.get("sources", {}).get("memory_item_ids") or []),
        "deprecated_list_redacted": any(
            item.get("id") == active.item.id and item.get("content_redacted") is True and "content" not in item
            for item in deprecated_items
        ),
        "deleted_event_written": deleted_event.get("payload", {}).get("content_redacted") is True,
        "proof_event_written": proof_payload.get("delete_event_id") == deleted_event.get("id"),
        "proof_api_matches_export": proof_payload == exported.get("proof"),
        "export_helper_found": exported.get("status") == "FOUND" and exported.get("proof") == proof_payload,
        "export_script_found": True
        if not args.include_subprocess_export
        else proc_returncode == 0 and script_output.is_file(),
        "confirm_reject_events_written": {EVENT_MEMORY_ITEM_CONFIRMED, EVENT_MEMORY_ITEM_REJECTED}.issubset(
            {str(event.get("type") or "") for event in governance_events}
        ),
        "audit_payload_sanitized": SECRET_TEXT.lower() not in serialized_events
        and "cookie" not in serialized_events
        and "embedding_json" not in serialized_events
        and "raw_prompt" not in serialized_events,
        "api_payload_sanitized": SECRET_TEXT.lower() not in serialized_deprecated,
        "debug_bundle_memory_summary": (bundle.get("memory_governance_summary") or {}).get("deleted") == 1
        and (bundle.get("latest_memory_delete_proof") or {}).get("item_id") == active.item.id,
        "debug_bundle_sanitized": SECRET_TEXT.lower() not in serialized_bundle,
        "missing_returns_404": missing_delete.status_code == 404,
        "cross_user_returns_404": cross_delete.status_code == 404,
        "strict_contract": contract_ok,
        "delete_response_has_proof": (delete_payload.get("deletion_proof") or {}).get("item_id") == active.item.id,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "memory_governance_v1",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "governance_run_id": governance_run_id,
        "include_subprocess_export": bool(args.include_subprocess_export),
    }
    summary_path = ROOT / "artifacts/runtime/memory-governance-v1-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_memory_governance_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
