from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from copilot_agent.settings import settings


class LLMProvider:
    """Thin OpenAI-compatible chat model provider."""

    def __init__(self) -> None:
        self.model_name = settings.openai_model
        self.base_url = settings.openai_base_url
        self.reasoning_effort = settings.openai_reasoning_effort
        self.thinking_type = settings.openai_thinking_type
        self._chat_model: ChatOpenAI | None = None

    def get_chat_model(self) -> ChatOpenAI:
        if self._chat_model is None:
            extra_body: dict[str, Any] = {}
            if settings.openai_thinking_type:
                extra_body["thinking"] = {"type": settings.openai_thinking_type}
            self._chat_model = ChatOpenAI(
                api_key=settings.openai_api_key or None,
                base_url=settings.openai_base_url,
                model=settings.openai_model,
                reasoning_effort=settings.openai_reasoning_effort,
                extra_body=extra_body or None,
                streaming=True,
            )
        return self._chat_model

    def get_tool_bound_model(self, tools: list[Any]):
        return self.get_chat_model().bind_tools(tools)

    def metadata(self) -> dict[str, str | None]:
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "reasoning_effort": self.reasoning_effort,
            "thinking_type": self.thinking_type,
        }
