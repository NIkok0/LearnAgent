#!/usr/bin/env python
"""Verify RAG document list/delete lifecycle and deletion audit payload."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.rag.document_lifecycle import delete_rag_document, list_rag_documents  # noqa: E402
from copilot_agent.rag.reload import RagStoreManager  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_RAG_DOCUMENT_DELETED  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.audit import audit_payload_has_secret  # noqa: E402


def _write_docs(base: Path) -> None:
    (base / "keep.md").write_text("# Keep\n\nstable public redis runbook", encoding="utf-8")
    (base / "delete-me.md").write_text(
        "# Delete Me\n\nunique_lifecycle_delete_token secret should disappear from retrieval",
        encoding="utf-8",
    )
    manifest = {
        "version": 1,
        "load_order": ["keep.md", "delete-me.md"],
        "doc_types": {"keep.md": "runbook", "delete-me.md": "runbook"},
        "include_glob": "*.md",
        "doc_security": {
            "keep.md": {
                "doc_id": "keep-doc",
                "tenant_id": "tenant-a",
                "classification": "internal",
                "pii_level": "none",
            },
            "delete-me.md": {
                "doc_id": "delete-doc",
                "tenant_id": "tenant-a",
                "classification": "confidential",
                "pii_level": "medium",
                "retention_policy": "delete-test",
            },
        },
    }
    (base / "docs_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> int:
    old_docs_path = os.environ.get("COPILOT_DOCS_PATH")
    old_vector = settings.rag_use_vector
    old_rebuild = settings.rag_rebuild_index
    old_hot_reload = settings.rag_hot_reload_enabled
    settings.rag_use_vector = False
    settings.rag_rebuild_index = False
    settings.rag_hot_reload_enabled = False
    store = EventStore(str(ROOT / "storage" / "verify-rag-document-lifecycle-events.sqlite"))
    try:
        with tempfile.TemporaryDirectory(prefix="learnagent-rag-life-") as tmp:
            docs_dir = Path(tmp)
            _write_docs(docs_dir)
            os.environ["COPILOT_DOCS_PATH"] = str(docs_dir)
            manager = RagStoreManager(trigger="api")

            before = list_rag_documents()
            before_hits = manager.store.search("unique_lifecycle_delete_token", top_k=4)
            result = delete_rag_document("delete-doc", manager=manager, reason="verify_delete", sync_vector=True)
            payload = result.audit_payload()
            audit_thread_id = f"__rag_audit_verify__-{uuid.uuid4().hex[:8]}"
            store.ensure_thread(audit_thread_id, title="RAG audit verify")
            run = store.create_run(audit_thread_id, run_id=f"rag-audit-{uuid.uuid4().hex[:8]}")
            run_id = str(run["id"])
            store.update_run_status(run_id, "running")
            store.append_event(audit_thread_id, run_id, EVENT_RAG_DOCUMENT_DELETED, payload)
            store.complete_run(run_id)

            after = list_rag_documents()
            after_hits = manager.store.search("unique_lifecycle_delete_token", top_k=4)
            manifest_text = (docs_dir / "docs_manifest.json").read_text(encoding="utf-8")
            validated = validate_payload_for_kind(EVENT_RAG_DOCUMENT_DELETED, payload)
            encoded = json.dumps(validated, ensure_ascii=False)
            audit_event = next(
                event
                for event in store.list_run_events(run_id)
                if event.get("type") == EVENT_RAG_DOCUMENT_DELETED
            )
            checks = {
                "documents_listed": before.get("document_count") == 2
                and any(item.get("doc_id") == "delete-doc" for item in before.get("documents", [])),
                "metadata_only": "unique_lifecycle_delete_token" not in json.dumps(before, ensure_ascii=False),
                "search_before_delete": any(chunk.source == "delete-me.md" for chunk in before_hits),
                "search_after_delete": not after_hits
                or all(chunk.source != "delete-me.md" for chunk in after_hits),
                "chunk_count_decreased": int(after.get("chunk_count") or 0) < int(before.get("chunk_count") or 0),
                "manifest_removed": "delete-me.md" not in json.loads(manifest_text).get("load_order", []),
                "file_removed": not (docs_dir / "delete-me.md").exists(),
                "audit_payload_shape": validated.get("doc_id") == "delete-doc"
                and validated.get("deleted_chunk_count", 0) >= 1
                and validated.get("vector_delete_attempted") is False
                and validated.get("vector_delete_success") is True,
                "audit_no_raw_text": "unique_lifecycle_delete_token" not in encoded
                and not audit_payload_has_secret(validated),
                "audit_event_written": audit_event.get("payload", {}).get("doc_id") == "delete-doc",
                "status_has_counts": "document_count" in result.rag_status
                and "deleted_document_count" in result.rag_status,
            }
            passed = all(checks.values())
            summary = {
                "suite_name": "rag_document_lifecycle_v1",
                "status": "PASS" if passed else "FAIL",
                "checks": checks,
                "before": before,
                "after": after,
                "audit_payload": payload,
            }
    finally:
        if old_docs_path is None:
            os.environ.pop("COPILOT_DOCS_PATH", None)
        else:
            os.environ["COPILOT_DOCS_PATH"] = old_docs_path
        settings.rag_use_vector = old_vector
        settings.rag_rebuild_index = old_rebuild
        settings.rag_hot_reload_enabled = old_hot_reload

    summary_path = ROOT / "artifacts" / "runtime" / "rag-document-lifecycle-v1-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"rag_document_lifecycle_v1={summary['status']}")
    print(f"summary_json={summary_path}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
