from __future__ import annotations

import json
from typing import Any

from copilot_agent.settings import settings


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": value}


def summarize_tool_llm_payload(tool_name: str, payload: Any) -> Any:
    """Shrink ToolMessage content for LLM context when mode=summary."""
    if settings.agent_tool_message_mode != "summary":
        return payload

    data = _as_dict(payload)
    max_chars = max(256, int(settings.agent_tool_message_max_chars))

    if tool_name == "search_docs":
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        excerpt = str(inner.get("excerpts_markdown") or "")
        summarized = {
            "success": data.get("success", True),
            "data": {
                "excerpts_markdown": _truncate_text(excerpt, max_chars),
                "sources": inner.get("sources") or [],
                "citations": inner.get("citations") or data.get("metadata", {}).get("citations") or [],
                "suggested_api_paths": inner.get("suggested_api_paths") or [],
            },
            "metadata": {
                "summary_mode": True,
                "excerpt_chars": len(excerpt),
                "excerpt_truncated": len(excerpt) > max_chars,
            },
        }
        if data.get("error"):
            summarized["error"] = data["error"]
        return summarized

    if tool_name in {"http_get", "http_post"}:
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        body = inner.get("body")
        body_text = json.dumps(body, ensure_ascii=False) if body is not None else ""
        summarized = {
            "success": data.get("success", True),
            "data": {
                "path": inner.get("path"),
                "status_code": inner.get("status_code") or data.get("metadata", {}).get("status_code"),
                "body_preview": _truncate_text(body_text, min(max_chars, 1200)),
            },
            "metadata": {"summary_mode": True, "body_truncated": len(body_text) > min(max_chars, 1200)},
        }
        if data.get("error"):
            summarized["error"] = data["error"]
        return summarized

    serialized = json.dumps(data, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return payload
    return {
        "success": data.get("success", True),
        "data": {"preview": _truncate_text(serialized, max_chars)},
        "metadata": {"summary_mode": True},
        "error": data.get("error"),
    }
