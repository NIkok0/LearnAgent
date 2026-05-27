#!/usr/bin/env python
"""Verify Scenario Skill Packs v1."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SCENARIO"] = "watermark"
os.environ["COPILOT_CAPABILITIES"] = "rag,http"

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from copilot_agent import server  # noqa: E402
from copilot_agent.context import ContextManager  # noqa: E402
from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_SKILL_SELECTED  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.scenario import load_scenario, scenario_status  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402
from copilot_agent.skills import load_skill_specs, select_skills  # noqa: E402
from copilot_agent.tools.audit import audit_payload_has_secret  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class _MockGraph:
    async def aget_state(self, _config):
        class _State:
            values: dict = {"messages": []}

        return _State()


async def _context_checks() -> dict[str, object]:
    scenario = load_scenario("watermark")
    apply_scenario_environment(scenario)
    event_store_path = ROOT / "artifacts/runtime/skills-v1-events.sqlite"
    event_store = EventStore(str(event_store_path))
    thread_id = f"skills-{uuid.uuid4().hex[:8]}"
    run = event_store.create_run(thread_id)
    run_id = str(run["id"])
    event_store.update_run_status(run_id, RUN_STATUS_RUNNING)
    memory = MemoryManager(
        rag_store=RagStore([DocChunk(source="RUNBOOK.md", start_line=1, text="Watermark QUEUED runbook")]),
        event_store=event_store,
        checkpoint_path=str(event_store_path),
    )
    ctx = ContextManager(
        scenario=scenario,
        memory=memory,
        tool_registry=ToolRegistry(),
        graph=_MockGraph(),
    )
    bundle = await ctx.assemble(
        thread_id=thread_id,
        run_id=run_id,
        turn_messages=[HumanMessage(content="watermark job queued troubleshooting")],
        goal="watermark job queued troubleshooting",
    )
    events = event_store.list_events(thread_id, run_id=run_id)
    skill_events = [event for event in events if event.get("type") == EVENT_SKILL_SELECTED]
    payload = skill_events[0].get("payload") if skill_events else {}
    skill_messages = [
        str(getattr(message, "content", "") or "")
        for message in bundle.graph_messages
        if isinstance(message, SystemMessage) and str(getattr(message, "content", "") or "").startswith("[Skills]")
    ]
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "bundle": bundle.model_dump(exclude={"graph_messages"}),
        "skill_messages": skill_messages,
        "skill_event_payload": payload,
        "skill_event_count": len(skill_events),
        "event_contract": validate_payload_for_kind(EVENT_SKILL_SELECTED, payload) if payload else {},
    }


def main() -> int:
    specs, warnings = load_skill_specs(repo_root=ROOT)
    watermark = load_scenario("watermark")
    minimal = load_scenario("minimal")
    selected = select_skills(
        watermark.skill_registry.enabled(watermark.config.skills) if watermark.skill_registry else [],
        goal="watermark job queued troubleshooting",
        route_kind="knowledge",
        enabled_capabilities=("rag", "http"),
    )
    missing_capability = select_skills(
        watermark.skill_registry.enabled(watermark.config.skills) if watermark.skill_registry else [],
        goal="watermark job queued troubleshooting",
        route_kind="knowledge",
        enabled_capabilities=("rag",),
    )
    context = asyncio.run(_context_checks())

    with TestClient(server.app) as client:
        thread_resp = client.post("/v1/threads", json={"title": "skills verify"})
        thread_resp.raise_for_status()
        thread_id = str(thread_resp.json()["id"])
        skills_resp = client.get("/v1/skills")
        preview_resp = client.get(
            f"/v1/threads/{thread_id}/skills",
            params={"goal": "watermark job queued troubleshooting"},
        )
        skills_payload = skills_resp.json()
        preview_payload = preview_resp.json()

    skill_event_payload = context["skill_event_payload"] if isinstance(context["skill_event_payload"], dict) else {}
    skill_surfaces = {
        "skill_messages": context["skill_messages"],
        "skill_event_payload": skill_event_payload,
        "skill_injections": context["bundle"].get("skill_injections", []),
    }
    encoded_context = json.dumps(skill_surfaces, ensure_ascii=False, default=str).lower()
    encoded_api = json.dumps({"skills": skills_payload, "preview": preview_payload}, ensure_ascii=False).lower()
    sensitive_terms = ("secret", "cookie", "raw_response", "raw_prompt", "request_body", "json_body")
    checks = {
        "manifest_loaded": "watermark_diagnosis" in specs and "rag_governance" in specs and not warnings,
        "scenario_enables_skill": "watermark_diagnosis" in watermark.config.skills
        and any(item.get("name") == "watermark_diagnosis" for item in scenario_status(watermark)["skills"]["available"]),
        "minimal_has_no_skills": minimal.config.skills == [],
        "selector_matches_goal": any(item.get("name") == "watermark_diagnosis" for item in selected),
        "missing_capability_warns": any(item.get("missing_capabilities") == ["http"] for item in missing_capability),
        "context_injects_skill": any("watermark_diagnosis" in message for message in context["skill_messages"])
        and any(item.get("name") == "watermark_diagnosis" for item in context["bundle"].get("skill_injections", [])),
        "skill_event_written": context["skill_event_count"] == 1
        and "watermark_diagnosis" in (skill_event_payload.get("skills") or []),
        "skill_event_contract": context["event_contract"].get("skills") == ["watermark_diagnosis"],
        "api_lists_skills": skills_resp.status_code == 200
        and any(item.get("name") == "watermark_diagnosis" for item in skills_payload.get("skills", [])),
        "api_preview_matches": preview_resp.status_code == 200
        and any(item.get("name") == "watermark_diagnosis" for item in preview_payload.get("skills", [])),
        "sanitized": not audit_payload_has_secret(skill_surfaces)
        and not audit_payload_has_secret({"api": skills_payload, "preview": preview_payload})
        and not any(term in encoded_context for term in sensitive_terms)
        and not any(term in encoded_api for term in sensitive_terms),
        "policy_not_bypassed": "http_post" not in [tool for item in selected for tool in item.get("tool_allowlist", [])],
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "skills_v1",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "context": context,
        "api": {"skills": skills_payload, "preview": preview_payload},
    }
    summary_path = ROOT / "artifacts/runtime/skills-v1-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_skills_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
