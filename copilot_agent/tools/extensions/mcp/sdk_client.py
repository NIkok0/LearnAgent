from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from copilot_agent.tools.extensions.mcp.schema import (
    McpPromptDefinition,
    McpResourceDefinition,
    McpServerDefinition,
    McpToolDefinition,
)

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

# streamable-http client (may be in different modules across SDK versions)
try:
    from mcp.client.streamable_http import streamable_http_client  # type: ignore[import-not-found]

    _MCP_HTTP_CLIENT_AVAILABLE = True
except ImportError:
    try:
        from mcp.client.http import http_client as streamable_http_client  # type: ignore[import-not-found,no-redef]

        _MCP_HTTP_CLIENT_AVAILABLE = True
    except ImportError:
        streamable_http_client = None  # type: ignore[assignment]
        _MCP_HTTP_CLIENT_AVAILABLE = False

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False


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


def _map_listed_resource(resource: Any, *, fallback: McpResourceDefinition | None = None) -> McpResourceDefinition:
    name = str(getattr(resource, "name", "") or "")
    uri = str(getattr(resource, "uri", "") or "")
    description = str(getattr(resource, "description", "") or (fallback.description if fallback else ""))
    mime_type = getattr(resource, "mimeType", None) or (fallback.mime_type if fallback else None)
    return McpResourceDefinition(name=name, description=description, uri=uri, mimeType=mime_type)


def _map_listed_prompt(prompt: Any, *, fallback: McpPromptDefinition | None = None) -> McpPromptDefinition:
    name = str(getattr(prompt, "name", "") or "")
    description = str(getattr(prompt, "description", "") or (fallback.description if fallback else ""))
    raw_args = getattr(prompt, "arguments", None) or []
    from copilot_agent.tools.extensions.mcp.schema import PromptArgument

    arguments: list[PromptArgument] = []
    for arg in raw_args:
        if isinstance(arg, dict):
            arguments.append(
                PromptArgument(
                    name=str(arg.get("name", "")),
                    description=str(arg.get("description", "")),
                    required=bool(arg.get("required", False)),
                )
            )
        else:
            arguments.append(
                PromptArgument(
                    name=str(getattr(arg, "name", "")),
                    description=str(getattr(arg, "description", "")),
                    required=bool(getattr(arg, "required", False)),
                )
            )
    return McpPromptDefinition(name=name, description=description, arguments=arguments)


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
    """Long-lived MCP client using the official Python SDK (stdio, SSE, or streamable-http)."""

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
        # Reconnection state
        self._reconnect_config = server.reconnect
        self._failure_count = 0
        self._reconnect_task: asyncio.Task | None = None
        self._health_check_task: asyncio.Task | None = None

    # ── connection management ──────────────────────────────────────────

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            if self._server.transport == "stdio":
                await self._connect_stdio()
            elif self._server.transport == "sse":
                await self._connect_sse()
            elif self._server.transport == "streamable-http":
                await self._connect_http()
            else:
                raise ValueError(f"SdkMcpClient does not support transport={self._server.transport}")
            self._connected = True
            self._failure_count = 0
        if self._reconnect_config.enabled:
            self._start_health_check()

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

    async def _connect_http(self) -> None:
        """Connect via streamable-http transport using the MCP SDK or httpx fallback."""
        url = (self._server.url or "").strip()
        if not url:
            raise ValueError(f"MCP streamable-http server '{self.server_name}' requires url")
        headers = dict(self._server.headers or {})

        if _MCP_HTTP_CLIENT_AVAILABLE and streamable_http_client is not None:
            transport = await self._stack.enter_async_context(
                streamable_http_client(url, headers=headers or None)
            )
            read, write = transport
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            log.info("MCP streamable-http connected server=%s url=%s", self.server_name, url)
            return

        # Fallback: use httpx to connect via SSE-over-HTTP
        if _HTTPX_AVAILABLE and httpx is not None:
            from mcp.client.sse import sse_client as _sse_client

            transport = await self._stack.enter_async_context(
                _sse_client(
                    url,
                    headers=headers or None,
                    sse_read_timeout=float(self._server.sse_read_timeout),
                )
            )
            read, write = transport
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            log.info("MCP streamable-http (SSE fallback) connected server=%s url=%s", self.server_name, url)
            return

        raise RuntimeError(
            f"MCP streamable-http transport requires mcp SDK streamable-http client "
            f"or httpx (pip install httpx)"
        )

    async def aclose(self) -> None:
        self._stop_health_check()
        self._cancel_reconnect()
        async with self._connect_lock:
            if not self._connected:
                return
            await self._stack.aclose()
            self._session = None
            self._connected = False
            self._stack = AsyncExitStack()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── tool operations ─────────────────────────────────────────────────

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
        except Exception as exc:
            self._on_call_failure()
            return {"success": False, "error": str(exc), "tool": tool_name}
        return map_call_tool_result(result)

    # ── resource operations ─────────────────────────────────────────────

    async def list_resources(self) -> list[McpResourceDefinition]:
        await self.connect()
        assert self._session is not None
        overrides = {res.uri: res for res in self._server.resources}
        try:
            response = await self._session.list_resources()
        except AttributeError:
            log.warning("MCP server %s does not support list_resources", self.server_name)
            return list(self._server.resources)
        discovered: list[McpResourceDefinition] = []
        for resource in response.resources if hasattr(response, "resources") else []:
            discovered.append(_map_listed_resource(resource, fallback=overrides.get(getattr(resource, "uri", ""))))
        if discovered:
            return discovered
        return list(self._server.resources)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        await self.connect()
        assert self._session is not None
        try:
            result = await self._session.read_resource(uri)
        except AttributeError:
            return {"success": False, "error": f"MCP server {self.server_name} does not support read_resource"}
        except Exception as exc:
            self._on_call_failure()
            return {"success": False, "error": str(exc), "uri": uri}
        contents: list[dict[str, Any]] = []
        for item in getattr(result, "contents", []):
            contents.append({
                "uri": getattr(item, "uri", uri),
                "mime_type": getattr(item, "mimeType", None),
                "text": getattr(item, "text", str(item)),
            })
        return {"success": True, "uri": uri, "contents": contents}

    # ── prompt operations ───────────────────────────────────────────────

    async def list_prompts(self) -> list[McpPromptDefinition]:
        await self.connect()
        assert self._session is not None
        overrides = {prompt.name: prompt for prompt in self._server.prompts}
        try:
            response = await self._session.list_prompts()
        except AttributeError:
            log.warning("MCP server %s does not support list_prompts", self.server_name)
            return list(self._server.prompts)
        discovered: list[McpPromptDefinition] = []
        for prompt in response.prompts if hasattr(response, "prompts") else []:
            discovered.append(_map_listed_prompt(prompt, fallback=overrides.get(getattr(prompt, "name", ""))))
        if discovered:
            return discovered
        return list(self._server.prompts)

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        await self.connect()
        assert self._session is not None
        try:
            result = await self._session.get_prompt(name, arguments or {})
        except AttributeError:
            return {"success": False, "error": f"MCP server {self.server_name} does not support get_prompt"}
        except Exception as exc:
            self._on_call_failure()
            return {"success": False, "error": str(exc), "name": name}
        messages: list[dict[str, Any]] = []
        for msg in getattr(result, "messages", []):
            messages.append({
                "role": str(getattr(msg, "role", "user")),
                "content": str(getattr(getattr(msg, "content", None), "text", str(getattr(msg, "content", "")))),
            })
        return {"success": True, "name": name, "messages": messages}

    # ── health check & reconnection (infrastructure) ────────────────────

    def _on_call_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= 2:
            log.warning("MCP server %s: %d consecutive failures, marking disconnected", self.server_name, self._failure_count)
            self._connected = False

    async def _ping(self) -> bool:
        """Lightweight health check — returns True if server is reachable."""
        if not self._connected or self._session is None:
            return False
        try:
            await asyncio.wait_for(self._session.list_tools(), timeout=5.0)
            self._failure_count = 0
            return True
        except Exception:
            self._on_call_failure()
            return False

    def _start_health_check(self) -> None:
        if self._health_check_task is not None and not self._health_check_task.done():
            return

        async def _loop() -> None:
            while self._connected or self._failure_count < self._reconnect_config.max_retries:
                await asyncio.sleep(self._reconnect_config.health_check_interval_s)
                if not await self._ping():
                    await self._try_reconnect()

        self._health_check_task = asyncio.create_task(_loop())

    def _stop_health_check(self) -> None:
        if self._health_check_task is not None and not self._health_check_task.done():
            self._health_check_task.cancel()
        self._health_check_task = None

    def _cancel_reconnect(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None

    async def _try_reconnect(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return  # already reconnecting
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Exponential backoff reconnection loop."""
        attempt = 0
        max_retries = self._reconnect_config.max_retries
        base = self._reconnect_config.backoff_base_s
        ceiling = self._reconnect_config.backoff_max_s

        while attempt < max_retries:
            attempt += 1
            delay = min(base * (2 ** (attempt - 1)), ceiling)
            jitter = delay * 0.1 * (hash(str(attempt)) % 100) / 100  # simple jitter
            wait_s = delay + jitter
            log.info("MCP %s reconnecting attempt %d/%d in %.1fs", self.server_name, attempt, max_retries, wait_s)
            await asyncio.sleep(wait_s)

            try:
                # close stale resources
                async with self._connect_lock:
                    if self._connected:
                        return  # someone else reconnected us
                await self._stack.aclose()
                self._stack = AsyncExitStack()
                self._session = None
                await self.connect()
                log.info("MCP %s reconnected successfully (attempt %d)", self.server_name, attempt)
                return
            except Exception as exc:
                log.warning("MCP %s reconnect attempt %d failed: %s", self.server_name, attempt, exc)

        log.error("MCP %s exhausted reconnect attempts (%d)", self.server_name, max_retries)
        self._reconnect_task = None

    async def force_reconnect(self) -> bool:
        """Public API: manually disconnect and reconnect."""
        self._stop_health_check()
        self._cancel_reconnect()
        async with self._connect_lock:
            if self._connected:
                try:
                    await self._stack.aclose()
                except Exception:
                    pass
                self._stack = AsyncExitStack()
                self._session = None
                self._connected = False
        try:
            await self.connect()
            return True
        except Exception as exc:
            log.error("MCP %s force reconnect failed: %s", self.server_name, exc)
            return False
