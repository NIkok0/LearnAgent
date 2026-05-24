from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from copilot_agent.scenario.router.types import ToolRoute


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_suggested_paths_from_messages(messages: list[BaseMessage]) -> list[str]:
    """Collect API path hints from recent search_docs ToolMessage payloads."""
    paths: list[str] = []
    seen: set[str] = set()
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        payload = _parse_tool_payload(message.content)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        hints = data.get("suggested_api_paths") if isinstance(data, dict) else None
        if not isinstance(hints, list):
            continue
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            path = str(hint.get("path") or "").strip()
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    paths.reverse()
    return paths


def merge_api_paths_into_route(route: ToolRoute, api_paths: list[str]) -> tuple[ToolRoute, bool]:
    if not api_paths:
        return route, False
    merged = list(route.suggested_paths)
    changed = False
    for path in api_paths:
        if path not in merged:
            merged.append(path)
            changed = True
    if not changed:
        return route, False
    return ToolRoute(
        kind=route.kind,
        recommended_tools=route.recommended_tools,
        forbidden_tools=route.forbidden_tools,
        suggested_paths=tuple(merged),
        rationale=route.rationale,
    ), True
