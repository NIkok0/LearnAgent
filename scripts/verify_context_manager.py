#!/usr/bin/env python
"""Verify ContextManager: preretrieval, packing, context_built audit event."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from copilot_agent.context import ContextManager, pack_graph_messages  # noqa: E402
from copilot_agent.context.assemble import build_graph_turn_messages  # noqa: E402
from copilot_agent.context.checkpoint_pack import pack_checkpoint_for_budget, total_message_chars  # noqa: E402
from copilot_agent.context.constants import RAG_PRERETRIEVAL_PREFIX  # noqa: E402
from copilot_agent.context.preretrieval import should_preretrieve  # noqa: E402
from copilot_agent.context.preretrieval_dedupe import apply_preretrieval_dedupe, build_preretrieval_cache  # noqa: E402
from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.memory.policy import MemoryPolicyConfig  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402
from copilot_agent.scenario.router.types import ToolRoute  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class _MockGraph:
    async def aget_state(self, _config):
        class _State:
            values: dict = {"messages": []}

        return _State()


class _BudgetGraph:
    def __init__(self, messages: list) -> None:
        self._messages = list(messages)
        counter = 0
        for message in self._messages:
            if getattr(message, "id", None):
                continue
            counter += 1
            message.id = f"m{counter}"

    async def aget_state(self, _config):
        class _State:
            next = None
            values = {"messages": self._messages}

        return _State()

    async def aupdate_state(self, _config, update):
        from langgraph.graph.message import RemoveMessage

        incoming = list(update.get("messages") or [])
        remove_ids = {str(item.id) for item in incoming if isinstance(item, RemoveMessage)}
        kept = [message for message in self._messages if str(getattr(message, "id", "")) not in remove_ids]
        added = [message for message in incoming if not isinstance(message, RemoveMessage)]
        counter = len(self._messages)
        for message in added:
            if getattr(message, "id", None):
                continue
            counter += 1
            message.id = f"m{counter}"
        self._messages = kept + added


async def _run_checks() -> dict[str, bool]:
    apply_scenario_environment(load_scenario("watermark"))
    scenario = load_scenario("watermark")
    store_path = ROOT / "artifacts/runtime/verify-context-manager-events.sqlite"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    event_store = EventStore(str(store_path))
    thread_id = f"ctx-verify-{uuid.uuid4().hex[:8]}"
    run = event_store.create_run(thread_id)
    run_id = str(run["id"])
    event_store.update_run_status(run_id, RUN_STATUS_RUNNING)

    chunks = [
        DocChunk(
            source="RUNBOOK.md",
            start_line=1,
            text="QUEUED tasks may indicate worker or Redis Stream issues.",
            doc_type="runbook",
        )
    ]
    rag = RagStore(chunks)
    memory = MemoryManager(rag_store=rag, event_store=event_store, checkpoint_path=str(store_path))
    registry = ToolRegistry()
    graph = _MockGraph()
    ctx = ContextManager(scenario=scenario, memory=memory, tool_registry=registry, graph=graph)

    knowledge_route = ToolRoute(
        kind="knowledge",
        recommended_tools=("search_docs",),
        forbidden_tools=("http_get", "http_post"),
        suggested_paths=(),
        rationale="docs",
    )
    checks = {
        "should_preretrieve_knowledge": should_preretrieve(knowledge_route),
        "packing_truncates_rag": False,
        "context_built_event": False,
        "context_built_payload_valid": False,
        "preretrieval_in_bundle": False,
        "preretrieval_cache_in_bundle": False,
        "preretrieval_dedupe_skips_duplicate": False,
        "checkpoint_pack_compacts_history": False,
    }

    cache = build_preretrieval_cache(query="水印任务一直 QUEUED 怎么排查？", hits=chunks)
    _, dedupe_meta = apply_preretrieval_dedupe("水印任务一直 QUEUED 怎么排查？", chunks, cache)
    checks["preretrieval_dedupe_skips_duplicate"] = bool(dedupe_meta.get("skipped_all_duplicate"))

    history = []
    for index in range(8):
        history.append(HumanMessage(content=f"question {index} " + ("y" * 400)))
        history.append(AIMessage(content="answer " + ("z" * 600)))
    budget_graph = _BudgetGraph(history)
    policy = MemoryPolicyConfig(checkpoint_compact_keep_recent_turns=2, checkpoint_compact_summary_max_chars=500)
    pack_result = await pack_checkpoint_for_budget(
        budget_graph,
        "budget-thread",
        max_total_chars=2500,
        new_turn_chars=300,
        policy=policy,
    )
    checks["checkpoint_pack_compacts_history"] = bool(pack_result.get("compacted")) and total_message_chars(
        budget_graph._messages
    ) + 300 <= 2500

    packed = pack_graph_messages(
        [
            SystemMessage(content="system"),
            SystemMessage(content=f"{RAG_PRERETRIEVAL_PREFIX}\n" + ("x" * 5000)),
            HumanMessage(content="question"),
        ],
        max_chars=800,
        enabled=True,
    )
    checks["packing_truncates_rag"] = packed.truncated and packed.used_chars <= 800

    bundle = await ctx.assemble(
        thread_id=thread_id,
        run_id=run_id,
        turn_messages=[HumanMessage(content="水印任务一直 QUEUED 怎么排查？")],
        goal="水印任务一直 QUEUED 怎么排查？",
    )
    checks["preretrieval_in_bundle"] = len(bundle.retrieved_context) > 0 or bool(
        bundle.truncation_report.get("preretrieval_enabled")
    )
    checks["preretrieval_cache_in_bundle"] = isinstance(bundle.truncation_report.get("preretrieval_cache"), dict)

    events = event_store.list_events(thread_id, run_id=run_id)
    context_events = [event for event in events if event.get("type") == "context_built"]
    checks["context_built_event"] = len(context_events) == 1
    if context_events:
        payload = context_events[0].get("payload") or {}
        try:
            validate_payload_for_kind("context_built", payload)
            checks["context_built_payload_valid"] = True
        except Exception:
            checks["context_built_payload_valid"] = False

    prior_graph = _BudgetGraph(
        [
            SystemMessage(content=scenario.system_prompt),
            HumanMessage(content="turn-one"),
            AIMessage(content="reply-one"),
        ]
    )
    policy = MemoryPolicyConfig(inject_dedupe_system_prompt=True, inject_dedupe_memory_messages=True)
    first_turn = await build_graph_turn_messages(
        graph=_BudgetGraph([]),
        thread_id="dedupe-thread-first",
        system_prompt=scenario.system_prompt,
        memory_context={},
        turn_messages=[HumanMessage(content="turn-one")],
        policy=policy,
    )
    checks["first_turn_includes_system_prompt"] = any(
        isinstance(message, SystemMessage) and scenario.system_prompt.strip() in str(message.content)
        for message in first_turn
    )
    second_turn = await build_graph_turn_messages(
        graph=prior_graph,
        thread_id="dedupe-thread-second",
        system_prompt=scenario.system_prompt,
        memory_context={},
        turn_messages=[HumanMessage(content="turn-two")],
        policy=policy,
    )
    checks["continuation_skips_duplicate_system_prompt"] = not any(
        isinstance(message, SystemMessage) and scenario.system_prompt.strip() == str(message.content).strip()
        for message in second_turn
    )

    return checks


def main() -> int:
    import asyncio

    checks = asyncio.run(_run_checks())
    passed = all(checks.values())
    summary = {
        "suite_name": "context_manager",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
    }
    summary_path = ROOT / "artifacts/runtime/context-manager-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_context_manager={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
