#!/usr/bin/env python
"""Verify MCP Capability: mock, stdio SDK transport, policy, watermark scenario."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

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


async def _verify_mcp_api_management(runtime: McpRuntime) -> dict[str, bool]:
    from copilot_agent import server as app_server  # noqa: WPS433

    previous_runtime = app_server.mcp_runtime
    app_server.mcp_runtime = runtime
    payload = {
        "name": "api_mock",
        "transport": "mock",
        "enabled": True,
        "tools": [
            {
                "name": "echo",
                "description": "Echo through runtime API mock",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "risk_level": "low",
                "requires_approval": False,
                "timeout_seconds": 10.0,
            }
        ],
        "resources": [
            {
                "name": "api-doc",
                "description": "Mock API resource",
                "uri": "mock://api-doc",
                "mimeType": "text/plain",
            }
        ],
        "prompts": [
            {
                "name": "api_prompt",
                "description": "Mock API prompt",
                "arguments": [{"name": "topic", "required": True}],
            }
        ],
    }
    try:
        transport = httpx.ASGITransport(app=app_server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            initial = await client.get("/v1/mcp/servers")
            added = await client.post("/v1/mcp/servers", json=payload)
            duplicate = await client.post("/v1/mcp/servers", json=payload)
            listed = await client.get("/v1/mcp/servers")
            reconnect = await client.post("/v1/mcp/servers/api_mock/reconnect")
            invoke_result = await runtime.handlers.invoke(
                server="api_mock",
                tool="echo",
                arguments={"text": "api-managed"},
            )
            removed = await client.delete("/v1/mcp/servers/api_mock")
            remove_missing = await client.delete("/v1/mcp/servers/api_mock")
        api_server = _server_by_name(listed.json().get("servers") or [], "api_mock")
        return {
            "mcp_api_initial_list": initial.status_code == 200,
            "mcp_api_add_server": added.status_code == 200 and added.json().get("added") == "api_mock",
            "mcp_api_duplicate_rejected": duplicate.status_code == 409,
            "mcp_api_list_added_config": api_server.get("transport") == "mock" and api_server.get("tools") == 1,
            "mcp_api_resource_prompt_counts": api_server.get("resources") == 1 and api_server.get("prompts") == 1,
            "mcp_api_added_handler_invokes": bool(invoke_result.get("success"))
            and (invoke_result.get("data") or {}).get("echo") == "api-managed",
            "mcp_api_mock_reconnect_rejected": reconnect.status_code == 400,
            "mcp_api_remove_server": removed.status_code == 200 and "api_mock" not in runtime.clients,
            "mcp_api_remove_missing_404": remove_missing.status_code == 404,
        }
    finally:
        app_server.mcp_runtime = previous_runtime


def _server_by_name(servers: list[object], name: str) -> dict[str, object]:
    for item in servers:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {}


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
    checks.update(await _verify_mcp_api_management(demo_runtime))

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
