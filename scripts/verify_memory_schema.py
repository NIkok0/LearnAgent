from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402

from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.memory.checkpoint_compactor import (  # noqa: E402
    COMPACTION_PREFIX,
    build_checkpoint_summary_model,
    render_checkpoint_summary,
)
from copilot_agent.memory.item_store import MemoryItemStore, content_hash  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent.memory.schema import (  # noqa: E402
    CheckpointCompactionSummary,
    EpisodicInjectBundle,
    MemoryContext,
    MemoryItemRecord,
    MemoryScope,
    MemoryType,
    MemoryWriteResult,
    RecalledMemoryItem,
)
from copilot_agent.runtime.event_schema import EVENT_CHECKPOINT_COMPACTED  # noqa: E402
from copilot_agent.runtime.event_store import utc_now_iso  # noqa: E402


def main() -> int:
    now = utc_now_iso()
    record = MemoryItemRecord(
        id="mem_schema",
        user_id="user-schema",
        thread_id="thread-schema",
        scope=MemoryScope.USER,
        memory_type=MemoryType.FACT,
        content="redis stream default key is watermark:tasks",
        content_hash=content_hash("redis stream default key is watermark:tasks"),
        importance=0.9,
        confidence=0.95,
        version=1,
        supersedes_id=None,
        is_deprecated=False,
        expires_at=None,
        access_count=2,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
        source_run_id="run-schema",
        history=[{"action": "insert", "at": now}],
        embedding=[0.1, 0.2],
    )
    dumped_record = record.as_dict()

    write_result = MemoryWriteResult(action="insert", item=record)
    recalled = RecalledMemoryItem(
        item=record,
        score=0.91,
        keyword_score=0.8,
        time_factor=0.9,
        vector_score=0.7,
        type_boost=0.08,
        route_kind="troubleshooting",
        aging_factor=0.9,
        confidence_factor=0.95,
        access_factor=1.1,
    )
    bundle = EpisodicInjectBundle(
        thread_summary={"summary_type": "thread", "recent_goals": ["check redis stream"]},
        recalled_runs=[],
        recalled_long_term=[recalled.as_dict()],
        inject_preview="[MemoryContext]\nRelevant facts:\n- redis stream default key is watermark:tasks",
    )
    context = MemoryContext(
        working={"checkpoint_path": "storage/test.sqlite"},
        semantic={"rag": True},
        episodic=bundle.as_dict(),
    )

    db_path = ROOT / "artifacts" / "runtime" / f"memory-schema-{uuid.uuid4().hex[:8]}.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = MemoryItemStore(str(db_path))
    store.insert(record)
    loaded = store.get(record.id)

    policy = MemoryPolicyConfig(checkpoint_compact_keep_recent_turns=2, checkpoint_compact_summary_max_chars=800)
    summary_model = build_checkpoint_summary_model(
        [
            HumanMessage(content="How should we check redis stream queue health?"),
            AIMessage(content="Decision: use http_get for read-only task status checks."),
            ToolMessage(content="GET /api/tasks returned PROCESSING", tool_call_id="call_1"),
            SystemMessage(content="[MemoryContext]\nRelevant facts:\n- should not be duplicated"),
        ],
        policy=policy,
        kept_recent_turns=2,
    )
    rendered_summary = render_checkpoint_summary(summary_model, 800)
    event_payload = validate_payload_for_kind(
        EVENT_CHECKPOINT_COMPACTED,
        {
            "compacted": True,
            "thread_id": "thread-schema",
            "before_count": 8,
            "after_count": 3,
            "prefix_count": 5,
            "kept_count": 3,
            "summary_format": summary_model.format_version,
            "sections_present": ["Task Context", "Decisions Made", "Tool Results"],
            "summary_chars": len(rendered_summary),
            "summary_model": summary_model.model_dump(mode="json"),
        },
    )

    checks = {
        "memory_item_model_dump": dumped_record["scope"] == "user" and "embedding" not in dumped_record,
        "memory_write_result_compatible": write_result.as_dict()["action"] == "insert",
        "recalled_memory_explain_fields": {"aging_factor", "confidence_factor", "access_factor"}.issubset(
            recalled.as_dict()
        ),
        "episodic_bundle_model_dump": bundle.as_dict()["recalled_long_term"][0]["id"] == record.id,
        "memory_context_model_dump": context.as_dict()["episodic"]["inject_preview"].startswith("[MemoryContext]"),
        "sqlite_row_to_pydantic": isinstance(loaded, MemoryItemRecord) and loaded.id == record.id if loaded else False,
        "checkpoint_summary_model": isinstance(summary_model, CheckpointCompactionSummary)
        and summary_model.format_version == "structured_text_v1",
        "checkpoint_summary_render": rendered_summary.startswith("Earlier conversation summary (structured):")
        and "Task Context:" in rendered_summary
        and "[MemoryContext]" not in rendered_summary,
        "checkpoint_prompt_prefix_compatible": f"{COMPACTION_PREFIX}\n{rendered_summary}".startswith(COMPACTION_PREFIX),
        "checkpoint_event_payload_valid": event_payload["summary_format"] == "structured_text_v1",
    }
    passed = all(checks.values())
    summary = {
        "checks": checks,
        "rendered_summary": rendered_summary,
        "checkpoint_event_payload": event_payload,
        "verify_memory_schema": "PASS" if passed else "FAIL",
    }
    out_path = ROOT / "artifacts" / "runtime" / "memory-schema-summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={out_path}")
    print(f"verify_memory_schema={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
