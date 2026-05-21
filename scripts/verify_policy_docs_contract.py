#!/usr/bin/env python
"""Verify DocsResolver and PolicyDecision contracts for MVP security boundaries."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SCENARIO", "watermark")

from copilot_agent.credentials import CredentialManager  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.rag.docs_resolver import resolve_docs_source  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.agent.tool_handlers import ToolHandlers  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.capability import CapabilityContext, load_capability_packs  # noqa: E402
from copilot_agent.tools.http_tools import ScenarioHttpClient  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


def main() -> int:
    previous_allow = settings.copilot_allow_job_post
    previous_capabilities = settings.copilot_capabilities
    try:
        settings.copilot_allow_job_post = True
        settings.copilot_capabilities = "rag,http"

        scenario = load_scenario("watermark")
        docs_source = resolve_docs_source(scenario_name="watermark")
        event_store = EventStore(str(ROOT / "storage/verify-policy-docs-events.sqlite"))
        memory = MemoryManager(
            rag_store=RagStore([DocChunk(source="README.md", start_line=1, text="policy docs")]),
            event_store=event_store,
            checkpoint_path=str(ROOT / "storage/verify-policy-docs-checkpoints.sqlite"),
        )
        credentials = CredentialManager.from_scenario_resources(scenario.resources, ttl_seconds=60)
        handlers = ToolHandlers(
            memory=memory,
            http=ScenarioHttpClient(
                base_url="http://127.0.0.1:0",
                dangerous_paths=tuple(scenario.policy.dangerous_paths),
            ),
            cookies=credentials,
        )
        registry = ToolRegistry()
        load_capability_packs(
            registry,
            capabilities=settings.enabled_capabilities(),
            ctx=CapabilityContext(scenario=scenario, handlers=handlers),
        )
        policy = PolicyRegistry(registry, scenario_policy=scenario.policy, credential_manager=credentials)

        dangerous_path = str(scenario.policy.dangerous_paths[0])
        decision = policy.evaluate_tool_calls(
            [
                {
                    "id": "call_policy_docs_1",
                    "name": "http_post",
                    "args": {"path": dangerous_path, "json_body": {"fileId": 1}},
                }
            ],
            allow_job_post=True,
            confirm_dangerous=False,
        )
        denied = policy.evaluate_tool_calls(
            [{"id": "call_unknown", "name": "unknown_tool", "args": {}}],
            allow_job_post=True,
            confirm_dangerous=False,
        )

        checks = {
            "docs_resolved": docs_source.available,
            "docs_scenario_name": docs_source.scenario_name == "watermark",
            "docs_source_kind": docs_source.source_kind in {"scenario", "env"},
            "docs_manifest_present": docs_source.manifest_path is not None,
            "policy_decision_ask": decision.decision == "ask",
            "policy_requires_approval": decision.requires_approval is True,
            "policy_source": decision.policy_source == "scenario_approval_policy",
            "policy_tool_name": decision.tool_name == "http_post",
            "policy_call_id": decision.call_id == "call_policy_docs_1",
            "policy_metadata_path": decision.metadata.get("path") == dangerous_path,
            "policy_unknown_denied": denied.decision == "deny" and not denied.allowed,
        }
        passed = all(checks.values())
        summary = {
            "suite_name": "policy_docs_contract",
            "status": "PASS" if passed else "FAIL",
            "checks": checks,
            "docs_source": docs_source.as_dict(),
            "policy_decision": decision.model_dump(),
        }
        summary_path = ROOT / "artifacts/runtime/policy-docs-contract-summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"checks={json.dumps(checks, ensure_ascii=False)}")
        print(f"summary_json={summary_path}")
        print(f"verify_policy_docs_contract={'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        settings.copilot_allow_job_post = previous_allow
        settings.copilot_capabilities = previous_capabilities


if __name__ == "__main__":
    raise SystemExit(main())
