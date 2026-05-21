from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from copilot_agent.tools.extensions.mcp.schema import McpServerDefinition, McpToolDefinition

log = logging.getLogger(__name__)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.types import CallToolResult, TextContent

    _MCP_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional until requirements installed
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    sse_client = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment,misc]
    CallToolResult = Any  # type: ignore[assignment,misc]
    TextContent = Any  # type: ignore[assignment,misc]
    _MCP_SDK_AVAILABLE = False


def mcp_sdk_available() -> bool:
    return _MCP_SDK_AVAILABLE


def _content_to_text(content: Any) -> str:
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(str(block))
    return "\n".join(part for part in parts if part).strip()


def map_call_tool_result(result: CallToolResult) -> dict[str, Any]:
    text = _content_to_text(result.content)
    structured = getattr(result, "structuredContent", None)
    if result.isError:
        return {"success": False, "error": text or "MCP tool returned error", "structured": structured}
    payload: dict[str, Any] = {"success": True, "content": text}
    if structured is not None:
        payload["structured"] = structured
        if isinstance(structured, dict):
            payload.update(structured)
    elif text:
        payload["text"] = text
    return payload


def _map_listed_tool(tool: Any, *, fallback: McpToolDefinition | None = None) -> McpToolDefinition:
    input_schema = getattr(tool, "inputSchema", None) or {}
    if not isinstance(input_schema, dict):
        input_schema = {}
    return McpToolDefinition(
        name=str(getattr(tool, "name", "")),
        description=str(getattr(tool, "description", "") or (fallback.description if fallback else "")),
        input_schema=input_schema,
        risk_level=fallback.risk_level if fallback else "medium",
        requires_approval=fallback.requires_approval if fallback else False,
        timeout_seconds=fallback.timeout_seconds if fallback else 60.0,
    )


def _resolve_repo_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "copilot_agent").is_dir() and (base / "docs").is_dir():
            return base
    return here.parents[4]


def _resolve_cwd(server: McpServerDefinition, *, repo_root: Path, scenario_root: Path | None) -> Path | None:
    if not server.cwd:
        return scenario_root or repo_root
    cwd = Path(server.cwd)
    if cwd.is_absolute():
        return cwd
    base = scenario_root or repo_root
    return (base / cwd).resolve()


def _build_stdio_params(
    server: McpServerDefinition,
    *,
    repo_root: Path,
    scenario_root: Path | None,
) -> StdioServerParameters:
    command = (server.command or sys.executable).strip()
    args = list(server.args or [])
    if not args and server.command and " " in server.command.strip():
        split = shlex.split(server.command, posix=os.name != "nt")
        command = split[0]
        args = split[1:]
    env = {**os.environ, **(server.env or {})}
    env.setdefault("PYTHONPATH", str(repo_root))
    cwd = _resolve_cwd(server, repo_root=repo_root, scenario_root=scenario_root)
    return StdioServerParameters(command=command, args=args, env=env, cwd=str(cwd) if cwd else None)


class SdkMcpClient:
    """Long-lived MCP client using the official Python SDK (stdio or SSE)."""

    def __init__(
        self,
        server: McpServerDefinition,
        *,
        repo_root: Path | None = None,
        scenario_root: Path | None = None,
    ) -> None:
        if not _MCP_SDK_AVAILABLE:
            raise RuntimeError("MCP Python SDK is not installed; pip install mcp>=1.6.0")
        self.server_name = server.name
        self._server = server
        self._repo_root = _resolve_repo_root(repo_root)
        self._scenario_root = scenario_root.resolve() if scenario_root else None
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()
        self._connected = False

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            if self._server.transport == "stdio":
                await self._connect_stdio()
            elif self._server.transport == "sse":
                await self._connect_sse()
            else:
                raise ValueError(f"SdkMcpClient does not support transport={self._server.transport}")
            self._connected = True

    async def _connect_stdio(self) -> None:
        params = _build_stdio_params(self._server, repo_root=self._repo_root, scenario_root=self._scenario_root)
        transport = await self._stack.enter_async_context(stdio_client(params))
        read, write = transport
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        log.info("MCP stdio connected server=%s command=%s", self.server_name, params.command)

    async def _connect_sse(self) -> None:
        url = (self._server.url or "").strip()
        if not url:
            raise ValueError(f"MCP SSE server '{self.server_name}' requires url")
        headers = dict(self._server.headers or {})
        transport = await self._stack.enter_async_context(
            sse_client(
                url,
                headers=headers or None,
                sse_read_timeout=float(self._server.sse_read_timeout),
            )
        )
        read, write = transport
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        log.info("MCP SSE connected server=%s url=%s", self.server_name, url)

    async def aclose(self) -> None:
        async with self._connect_lock:
            if not self._connected:
                return
            await self._stack.aclose()
            self._session = None
            self._connected = False
            self._stack = AsyncExitStack()

    async def list_tools(self) -> list[McpToolDefinition]:
        await self.connect()
        assert self._session is not None
        overrides = {tool.name: tool for tool in self._server.tools}
        response = await self._session.list_tools()
        discovered: list[McpToolDefinition] = []
        for tool in response.tools:
            discovered.append(_map_listed_tool(tool, fallback=overrides.get(tool.name)))
        if discovered:
            return discovered
        return list(self._server.tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.connect()
        assert self._session is not None
        timeout = None
        for tool in self._server.tools:
            if tool.name == tool_name and tool.timeout_seconds:
                timeout = tool.timeout_seconds
                break
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return {"success": False, "error": f"MCP tool timeout after {timeout}s", "tool": tool_name}
        return map_call_tool_result(result)
