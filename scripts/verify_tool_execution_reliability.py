#!/usr/bin/env python
"""Verify ToolRegistry timeout, retry, and idempotency metadata contracts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class EmptyArgs(BaseModel):
    pass


class PostArgs(BaseModel):
    path: str
    json_body: dict[str, Any] = {}
    idempotency_key: str | None = None


async def verify() -> dict[str, Any]:
    retry_calls = {"count": 0}
    write_calls = {"count": 0}

    async def flaky_read() -> dict[str, Any]:
        retry_calls["count"] += 1
        if retry_calls["count"] == 1:
            raise RuntimeError("transient read failure")
        return {"ok": True, "attempt": retry_calls["count"]}

    async def slow_read() -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"ok": True}

    async def write_tool(path: str, json_body: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        del path, json_body, idempotency_key
        write_calls["count"] += 1
        raise RuntimeError("write failed")

    registry = ToolRegistry()
    registry.register_async(
        name="flaky_read",
        description="flaky read",
        coroutine=flaky_read,
        args_schema=EmptyArgs,
        category="test",
        risk_level="low",
        timeout_seconds=1.0,
        max_retries=1,
    )
    registry.register_async(
        name="slow_read",
        description="slow read",
        coroutine=slow_read,
        args_schema=EmptyArgs,
        category="test",
        risk_level="low",
        timeout_seconds=0.01,
        max_retries=0,
    )
    registry.register_async(
        name="write_tool",
        description="write tool",
        coroutine=write_tool,
        args_schema=PostArgs,
        category="http",
        risk_level="high",
        timeout_seconds=1.0,
        max_retries=0,
        idempotency_key_field="idempotency_key",
    )

    tools = {tool.name: tool for tool in registry.tools()}
    retry_result = await tools["flaky_read"].ainvoke({})
    timeout_error = ""
    try:
        await tools["slow_read"].ainvoke({})
    except Exception as exc:
        timeout_error = str(exc)
    write_error = ""
    try:
        await tools["write_tool"].ainvoke(
            {"path": "/write", "json_body": {"x": 1}, "idempotency_key": "idem-123"}
        )
    except Exception as exc:
        write_error = str(exc)

    write_spec = registry.get_spec("write_tool")
    return {
        "retry_result": retry_result,
        "retry_calls": retry_calls["count"],
        "timeout_error": timeout_error,
        "write_calls": write_calls["count"],
        "write_error": write_error,
        "write_public_spec": write_spec.public_dict({"idempotency_key": "idem-123"}) if write_spec else {},
        "tool_count": len(registry.tools()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent tool execution reliability v1.")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/tool-execution-reliability-summary.json"),
    )
    parser.add_argument("--run-id", default=f"tool-reliability-{uuid.uuid4().hex[:8]}")
    args = parser.parse_args()
    del args.run_id

    summary = asyncio.run(verify())
    checks = {
        "read_retried_once": summary["retry_calls"] == 2 and summary["retry_result"].get("attempt") == 2,
        "timeout_enforced": "timed out" in summary["timeout_error"],
        "write_not_retried": summary["write_calls"] == 1 and "write failed" in summary["write_error"],
        "idempotency_declared": summary["write_public_spec"].get("idempotency_key_field") == "idempotency_key",
        "retry_declared": summary["write_public_spec"].get("max_retries") == 0,
    }
    summary["checks"] = checks
    summary["tool_execution_reliability"] = "PASS" if all(checks.values()) else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"tool_execution_reliability={summary['tool_execution_reliability']}")
    return 0 if summary["tool_execution_reliability"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
