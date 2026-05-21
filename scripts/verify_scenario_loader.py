#!/usr/bin/env python
"""Verify Scenario loader, policy tightening, and flat config (Phase A/B/C + 2-3-1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.context import ContextBundle  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.scenario import load_scenario, scenario_status  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402
from copilot_agent.tools.capability import CapabilityContext, load_capability_packs  # noqa: E402
from copilot_agent.tools.whitelist import validate_get_path, validate_post_path  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class _StubHandlers:
    async def search_docs(self, query: str, config=None):
        return {"ok": True}

    async def http_get(self, path: str, cookie_header=None, config=None):
        return {"ok": True}

    async def http_post(self, path: str, json_body=None, cookie_header=None, idempotency_key=None, config=None):
        return {"ok": True}


def main() -> int:
    watermark = load_scenario("watermark")
    minimal = load_scenario("minimal")
    apply_scenario_environment(watermark)

    wm_status = scenario_status(watermark)
    docs_dir = Path(str(wm_status["docs_dir"]))
    checks = {
        "watermark_loaded": watermark.name == "watermark",
        "watermark_flat_config": watermark.config_path is not None and watermark.config_path.name == "watermark.yaml",
        "watermark_deployment_capabilities": set(settings.enabled_capabilities()) >= {"rag", "http", "mcp"},
        "watermark_system_prompt": "Watermarking platform operations copilot" in watermark.system_prompt,
        "watermark_docs_resolved": docs_dir.is_dir(),
        "watermark_allowlist": {
            "search_docs",
            "http_get",
            "http_post",
            "mcp_watermark_ops_check_api_health",
            "mcp_watermark_ops_search_platform_docs",
        }.issubset(set(watermark.policy.tool_allowlist)),
        "watermark_router_rules": watermark.router_rules is not None and len(watermark.router_rules.rules) >= 6,
        "watermark_memory_overlay": watermark.memory_policy_overlay is not None,
        "watermark_memory_top_k": watermark.resolve_memory_policy().long_term_recall_top_k == 3,
        "watermark_resources_rag_rules": watermark.rag_rules is not None and len(watermark.rag_rules.rewrite_rules) > 0,
        "watermark_resources_diagnosis": watermark.diagnosis_templates is not None,
        "watermark_eval_golden": watermark.eval_path("golden") is not None,
        "watermark_eval_rag_cases": watermark.eval_path("rag_cases") is not None,
        "watermark_http_get_allowlist": validate_get_path("/api/v1/stats/dashboard") is None,
        "watermark_http_post_allowlist": validate_post_path("/api/v1/auth/login") is None,
        "minimal_loaded": minimal.name == "minimal",
        "minimal_policy_denylist": "search_docs" in minimal.policy.tool_denylist,
        "context_bundle_schema": ContextBundle.model_validate(
            {
                "thread_id": "t1",
                "run_id": "r1",
                "user_message": "hello",
                "budget": {"max_context_chars": 14000},
            }
        ).thread_id
        == "t1",
    }

    registry = ToolRegistry()
    load_capability_packs(
        registry,
        capabilities=("rag", "http"),
        ctx=CapabilityContext(scenario=watermark, handlers=_StubHandlers()),
    )
    checks["capability_registry_tools"] = set(registry.names()) >= {"search_docs", "http_get", "http_post"}

    gate = PolicyRegistry(registry, scenario_policy=minimal.policy)
    denied = gate.evaluate_tool_calls([{"name": "search_docs", "args": {"query": "x"}}])
    checks["scenario_policy_denies_minimal"] = not denied.allowed and denied.reason == "scenario_tool_denied"

    passed = all(checks.values())
    summary = {
        "suite_name": "scenario_loader",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "watermark": wm_status,
    }
    summary_path = ROOT / "artifacts/runtime/scenario-loader-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_scenario_loader={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
