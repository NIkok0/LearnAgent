from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copilot_agent.llm.provider import DeepSeekCompatibleChatOpenAI


def main() -> None:
    model = DeepSeekCompatibleChatOpenAI(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        streaming=True,
    )
    payload = model._get_request_payload(
        [
            HumanMessage(content="hello"),
            AIMessage(content="hi", additional_kwargs={"reasoning_content": "internal-state"}),
            HumanMessage(content="continue"),
        ]
    )
    assistant_messages = [message for message in payload["messages"] if message.get("role") == "assistant"]
    ok_reasoning_roundtrip = assistant_messages[-1].get("reasoning_content") == "internal-state"

    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "a"}) + AIMessageChunk(
        content="ok",
        additional_kwargs={"reasoning_content": "b"},
    )
    ok_chunk_merge = chunk.additional_kwargs.get("reasoning_content") == "ab"

    summary = {
        "payload_reasoning_content": assistant_messages[-1].get("reasoning_content"),
        "chunk_reasoning_content": chunk.additional_kwargs.get("reasoning_content"),
        "checks": {
            "reasoning_roundtrip": ok_reasoning_roundtrip,
            "stream_chunk_merge": ok_chunk_merge,
        },
    }
    print(summary)
    if not all(summary["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
