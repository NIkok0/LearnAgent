from __future__ import annotations

import re


def mcp_registry_tool_name(server: str, tool: str) -> str:
    safe_server = re.sub(r"[^a-zA-Z0-9_]+", "_", server.strip()).strip("_").lower()
    safe_tool = re.sub(r"[^a-zA-Z0-9_]+", "_", tool.strip()).strip("_").lower()
    return f"mcp_{safe_server}_{safe_tool}"


def parse_mcp_registry_tool_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp_"):
        return None
    parts = name.split("_", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]
