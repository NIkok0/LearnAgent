#!/usr/bin/env python
"""Verify Private RAG Output Guard v1 blocks/degrades sensitive final output."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.stream.event_mapper import GraphEventMapper  # noqa: E402
from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_OUTPUT_GUARD_CHECKED  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class _FakeChunk:
    def __init__(self, text: str) -> None:
        self.content = text


class _FakeGraph:
    def __init__(self, text: str) -> None:
        self._text = text

    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": _FakeChunk(self._text)},
        }


async def _collect(text: str) -> list:
    store = EventStore(str(ROOT / "storage/verify-private-rag-output-guard.sqlite"))
    thread_id = f"output-guard-{uuid.uuid4().hex[:8]}"
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    memory = MemoryManager(
        rag_store=RagStore([]),
        event_store=store,
        checkpoint_path=str(ROOT / "storage/verify-private-rag-output-guard-checkpoints.sqlite"),
    )
    mapper = GraphEventMapper(memory=memory, tool_registry=ToolRegistry())
    events = []
    async for event in mapper.map(
        graph=_FakeGraph(text),
        graph_input={},
        graph_config={},
        thread_id=thread_id,
        run_id=run_id,
    ):
        events.append(event)
    return events


def _payloads(events, kind: str) -> list[dict]:
    return [event.to_store_payload() for event in events if event.kind == kind]


def main() -> int:
    safe_events = asyncio.run(_collect("Answer based on allowed.md."))
    unsafe_events = asyncio.run(_collect("Leaked sk-1234567890abcdef and set-cookie: a=b"))
    safe_guard = _payloads(safe_events, EVENT_OUTPUT_GUARD_CHECKED)[0]
    unsafe_guard = _payloads(unsafe_events, EVENT_OUTPUT_GUARD_CHECKED)[0]
    validate_payload_for_kind(EVENT_OUTPUT_GUARD_CHECKED, safe_guard)
    validate_payload_for_kind(EVENT_OUTPUT_GUARD_CHECKED, unsafe_guard)
    unsafe_tokens = "".join(payload.get("text", "") for payload in _payloads(unsafe_events, "token"))
    unsafe_done = _payloads(unsafe_events, "done")[0]
    unsafe_done_text = str((unsafe_done.get("assistant_message") or {}).get("content") or "")
    checks = {
        "safe_guard_allows": safe_guard.get("safe") is True and safe_guard.get("action") == "allow",
        "safe_token_emitted": "allowed.md" in "".join(payload.get("text", "") for payload in _payloads(safe_events, "token")),
        "unsafe_guard_degrades": unsafe_guard.get("safe") is False and unsafe_guard.get("action") == "degrade",
        "unsafe_token_redacted": "sk-1234567890abcdef" not in unsafe_tokens and "set-cookie" not in unsafe_tokens.lower(),
        "unsafe_done_redacted": "sk-1234567890abcdef" not in unsafe_done_text and "set-cookie" not in unsafe_done_text.lower(),
        "guard_before_done": [event.kind for event in unsafe_events].index(EVENT_OUTPUT_GUARD_CHECKED)
        < [event.kind for event in unsafe_events].index("done"),
    }
    passed = all(checks.values())
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"unsafe_guard={json.dumps(unsafe_guard, ensure_ascii=False, sort_keys=True)}")
    print(f"private_rag_output_guard_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
