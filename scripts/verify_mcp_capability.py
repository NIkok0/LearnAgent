#!/usr/bin/env python
"""Verify MCP Capability: mock, stdio SDK transport, policy, watermark scenario."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.tool_result import ToolResultModel  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.scenario import load_scenario, scenario_status  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.capability import CapabilityContext, load_capability_packs  # noqa: E402
from copilot_agent.tools.extensions.mcp import (  # noqa: E402
    McpRuntime,
    mcp_registry_tool_name,
    mcp_sdk_available,
)
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


async def _run_checks() -> dict[str, bool | str]:
    checks: dict[str, bool | str] = {"mcp_sdk_installed": mcp_sdk_available()}

    demo = load_scenario("mcp_demo")
    demo_runtime = await McpRuntime.start(demo.mcp, scenario_root=demo.root, connect=False)
    assert demo_runtime is not None
    echo_name = mcp_registry_tool_name("demo", "echo")
    echo_result = await demo_runtime.handlers.invoke(server="demo", tool="echo", arguments={"text": "hello-mcp"})
    checks.update(
        {
            "demo_scenario_loaded": demo.name == "mcp_demo",
            "demo_flat_config": demo.config_path.name == "mcp_demo.yaml",
            "demo_mock_echo_success": bool(echo_result.get("success")),
            "demo_mock_echo_payload": str((echo_result.get("data") or {}).get("echo", "")) == "hello-mcp",
        }
    )

    demo_registry = ToolRegistry()
    load_capability_packs(
        demo_registry,
        capabilities=("mcp",),
        ctx=CapabilityContext(scenario=demo, handlers=None, mcp_runtime=demo_runtime),
    )
    checks["demo_registry_has_echo"] = echo_name in demo_registry.names()

    from_mcp = ToolResultModel.from_mcp(
        {"success": True, "content": "ok", "structured": {"status": "up"}},
        server="demo",
        tool="echo",
        duration_ms=12,
        sanitized_args={"text": "ok"},
    )
    checks["from_mcp_unified_entry"] = (
        from_mcp.success
        and from_mcp.metadata.get("mcp_server") == "demo"
        and (from_mcp.data or {}).get("status") == "up"
    )

    gate = PolicyRegistry(demo_registry, scenario_policy=demo.policy)
    checks["demo_policy_allows_echo"] = gate.evaluate_tool_calls(
        [{"name": echo_name, "args": {"text": "ok"}}]
    ).allowed

    watermark = load_scenario("watermark")
    checks["watermark_has_mcp_config"] = watermark.mcp is not None
    checks["watermark_mcp_in_deployment"] = "mcp" in settings.enabled_capabilities()

    if mcp_sdk_available():
        wm_runtime = await McpRuntime.start(watermark.mcp, scenario_root=watermark.root)
        assert wm_runtime is not None
        health_name = mcp_registry_tool_name("watermark_ops", "check_api_health")
        docs_name = mcp_registry_tool_name("watermark_ops", "search_platform_docs")
        discovered = {tool.name for server in wm_runtime.config.enabled_servers() for tool in server.tools}
        checks["watermark_stdio_discover_tools"] = {
            "check_api_health",
            "search_platform_docs",
        }.issubset(discovered)

        wm_registry = ToolRegistry()
        load_capability_packs(
            wm_registry,
            capabilities=("mcp",),
            ctx=CapabilityContext(scenario=watermark, handlers=None, mcp_runtime=wm_runtime),
        )
        checks["watermark_registry_tools"] = health_name in wm_registry.names() and docs_name in wm_registry.names()

        docs_result = await wm_runtime.handlers.invoke(
            server="watermark_ops",
            tool="search_platform_docs",
            arguments={"query": "watermark API", "top_k": 2},
        )
        checks["watermark_stdio_docs_call"] = bool(docs_result.get("success"))

        wm_gate = PolicyRegistry(wm_registry, scenario_policy=watermark.policy)
        checks["watermark_policy_allows_mcp"] = wm_gate.evaluate_tool_calls(
            [{"name": docs_name, "args": {"query": "deploy", "top_k": 1}}]
        ).allowed

        await wm_runtime.aclose()
    else:
        checks["watermark_stdio_discover_tools"] = "skipped_no_sdk"
        checks["watermark_registry_tools"] = "skipped_no_sdk"
        checks["watermark_stdio_docs_call"] = "skipped_no_sdk"
        checks["watermark_policy_allows_mcp"] = "skipped_no_sdk"

    await demo_runtime.aclose()
    return checks


def main() -> int:
    checks = asyncio.run(_run_checks())
    passed = all(
        key == "mcp_sdk_installed" or value is True or str(value).startswith("skipped_")
        for key, value in checks.items()
    )
    summary = {
        "suite_name": "mcp_capability",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "scenario": scenario_status(load_scenario("watermark")),
    }
    summary_path = ROOT / "artifacts/runtime/mcp-capability-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_mcp_capability={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
