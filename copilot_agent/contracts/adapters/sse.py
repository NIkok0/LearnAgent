from __future__ import annotations

import json
from typing import Any

from copilot_agent.contracts.base import RuntimeEvent


class SseAdapter:
    """Encode RuntimeEvent as SSE wire format."""

    @staticmethod
    def encode(event: RuntimeEvent) -> str:
        return _format_sse(event.kind, event.to_store_payload())


def _format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
