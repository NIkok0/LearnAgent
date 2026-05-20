from __future__ import annotations

import asyncio
import logging
import os
import socket
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, Optional, Type

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, BaseMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _convert_chunk_to_generation_chunk, _convert_message_to_dict

from copilot_agent.settings import settings

log = logging.getLogger(__name__)

_llm_inflight_semaphore: asyncio.Semaphore | None = None


def _llm_inflight_semaphore() -> asyncio.Semaphore | None:
    global _llm_inflight_semaphore
    limit = int(settings.max_llm_inflight)
    if limit <= 0:
        return None
    if _llm_inflight_semaphore is None:
        _llm_inflight_semaphore = asyncio.Semaphore(max(1, limit))
    return _llm_inflight_semaphore


class _InflightLimitedModel:
    """Wraps a LangChain chat model and limits concurrent ``ainvoke`` calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def bind_tools(self, tools: list[Any]) -> "_InflightLimitedModel":
        return _InflightLimitedModel(self._inner.bind_tools(tools))

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        semaphore = _llm_inflight_semaphore()
        if semaphore is None:
            return await self._inner.ainvoke(*args, **kwargs)
        async with semaphore:
            return await self._inner.ainvoke(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LLMProvider:
    """Thin OpenAI-compatible chat model provider."""

    def __init__(self) -> None:
        self.model_name = settings.openai_model
        self.base_url = settings.openai_base_url
        self.reasoning_effort = settings.openai_reasoning_effort
        self.thinking_type = settings.openai_thinking_type
        self.proxy_url = resolve_openai_proxy_url()
        self._chat_model: ChatOpenAI | None = None

    def get_chat_model(self) -> ChatOpenAI:
        if self._chat_model is None:
            self._chat_model = self._build_chat_model(include_thinking=True, **self._client_kwargs())
        return self._chat_model

    def get_tool_bound_model(self, tools: list[Any]):
        if settings.openai_disable_thinking_for_tools and settings.openai_thinking_type:
            model = self._build_chat_model(include_thinking=False, **self._client_kwargs()).bind_tools(tools)
        else:
            model = self.get_chat_model().bind_tools(tools)
        return _InflightLimitedModel(model)

    def metadata(self) -> dict[str, str | None]:
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "provider": settings.openai_provider,
            "reasoning_effort": self.reasoning_effort,
            "thinking_type": self.thinking_type,
            "proxy_url": self.proxy_url,
        }

    def _build_chat_model(self, *, include_thinking: bool, **client_kwargs: Any) -> ChatOpenAI:
        extra_body: dict[str, Any] = {}
        if include_thinking and settings.openai_thinking_type:
            extra_body["thinking"] = {"type": settings.openai_thinking_type}
        extra_body.update(_extra_body_from_settings())
        model_cls = DeepSeekCompatibleChatOpenAI if _is_deepseek_provider() else ChatOpenAI
        return model_cls(
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            reasoning_effort=settings.openai_reasoning_effort if include_thinking else None,
            extra_body=extra_body or None,
            streaming=True,
            **client_kwargs,
        )

    def _client_kwargs(self) -> dict[str, Any]:
        if not self.proxy_url:
            return {}
        log.info("OpenAI-compatible LLM traffic using proxy: %s", self.proxy_url)
        return {
            "http_client": httpx.Client(proxy=self.proxy_url, timeout=60.0),
            "http_async_client": httpx.AsyncClient(proxy=self.proxy_url, timeout=60.0),
        }


class DeepSeekCompatibleChatOpenAI(ChatOpenAI):
    """ChatOpenAI adapter for DeepSeek thinking-mode message round-trips.

    DeepSeek returns ``reasoning_content`` on assistant messages in thinking mode
    and requires callers to pass it back in later conversation turns. The
    upstream OpenAI message converter currently drops that provider-specific
    field, so this adapter preserves it without changing the rest of the
    ChatOpenAI/LangChain behavior.
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        messages = self._convert_input(input_).to_messages()
        if stop is not None:
            kwargs["stop"] = stop
        payload = {
            "messages": [_convert_message_to_deepseek_dict(message) for message in messages],
            **self._default_params,
            **kwargs,
        }
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload

    def _create_chat_result(
        self,
        response: dict[str, Any] | Any,
        generation_info: Optional[dict[str, Any]] = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info=generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, response_dict.get("choices", [])):
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            reasoning_content = message.get("reasoning_content") if isinstance(message, dict) else None
            if reasoning_content and isinstance(generation.message, AIMessage):
                generation.message.additional_kwargs["reasoning_content"] = str(reasoning_content)
        return result

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        default_chunk_class: Type[BaseMessageChunk] = AIMessageChunk
        base_generation_info: dict[str, Any] = {}

        if self.include_response_headers:
            raw_response = self.client.with_raw_response.create(**payload)
            response = raw_response.parse()
            base_generation_info = {"headers": dict(raw_response.headers)}
        else:
            response = self.client.create(**payload)
        with response:
            is_first_chunk = True
            for chunk in response:
                if not isinstance(chunk, dict):
                    chunk = chunk.model_dump()
                generation_chunk = _convert_chunk_to_generation_chunk(
                    chunk,
                    default_chunk_class,
                    base_generation_info if is_first_chunk else {},
                )
                if generation_chunk is None:
                    continue
                _attach_deepseek_reasoning_chunk(generation_chunk, chunk)
                default_chunk_class = generation_chunk.message.__class__
                logprobs = (generation_chunk.generation_info or {}).get("logprobs")
                if run_manager:
                    run_manager.on_llm_new_token(
                        generation_chunk.text,
                        chunk=generation_chunk,
                        logprobs=logprobs,
                    )
                is_first_chunk = False
                yield generation_chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        default_chunk_class: Type[BaseMessageChunk] = AIMessageChunk
        base_generation_info: dict[str, Any] = {}
        if self.include_response_headers:
            raw_response = await self.async_client.with_raw_response.create(**payload)
            response = raw_response.parse()
            base_generation_info = {"headers": dict(raw_response.headers)}
        else:
            response = await self.async_client.create(**payload)
        async with response:
            is_first_chunk = True
            async for chunk in response:
                if not isinstance(chunk, dict):
                    chunk = chunk.model_dump()
                generation_chunk = _convert_chunk_to_generation_chunk(
                    chunk,
                    default_chunk_class,
                    base_generation_info if is_first_chunk else {},
                )
                if generation_chunk is None:
                    continue
                _attach_deepseek_reasoning_chunk(generation_chunk, chunk)
                default_chunk_class = generation_chunk.message.__class__
                logprobs = (generation_chunk.generation_info or {}).get("logprobs")
                if run_manager:
                    await run_manager.on_llm_new_token(
                        generation_chunk.text,
                        chunk=generation_chunk,
                        logprobs=logprobs,
                    )
                is_first_chunk = False
                yield generation_chunk


def _convert_message_to_deepseek_dict(message: BaseMessage) -> dict[str, Any]:
    message_dict = _convert_message_to_dict(message)
    if isinstance(message, AIMessage):
        reasoning_content = message.additional_kwargs.get("reasoning_content")
        if reasoning_content:
            message_dict["reasoning_content"] = str(reasoning_content)
    return message_dict


def _attach_deepseek_reasoning_chunk(generation_chunk: ChatGenerationChunk, raw_chunk: dict[str, Any]) -> None:
    choices = raw_chunk.get("choices") or []
    if not choices:
        return
    delta = choices[0].get("delta") or {}
    if not isinstance(delta, dict):
        return
    reasoning_content = delta.get("reasoning_content")
    if reasoning_content and isinstance(generation_chunk.message, AIMessageChunk):
        generation_chunk.message.additional_kwargs["reasoning_content"] = str(reasoning_content)


def _extra_body_from_settings() -> dict[str, Any]:
    raw = (settings.openai_extra_body_json or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        log.warning("Ignoring invalid OPENAI_EXTRA_BODY_JSON")
        return {}
    return data if isinstance(data, dict) else {}


def _is_deepseek_provider() -> bool:
    provider = (settings.openai_provider or "").lower()
    base_url = str(settings.openai_base_url or "").lower()
    model = str(settings.openai_model or "").lower()
    return provider == "deepseek" or "deepseek" in base_url or model.startswith("deepseek")


def resolve_openai_proxy_url() -> str | None:
    explicit = (settings.openai_proxy_url or "").strip()
    if explicit:
        return _normalize_proxy_url(explicit)

    env_proxy = (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or ""
    ).strip()
    if env_proxy:
        return _normalize_proxy_url(env_proxy)

    if not settings.openai_auto_proxy:
        return None

    for host_port in _proxy_probe_hosts():
        host, port = host_port.rsplit(":", 1)
        if _tcp_port_open(host, int(port)):
            return f"http://{host}:{port}"
    return None


def _proxy_probe_hosts() -> list[str]:
    raw = settings.openai_proxy_probe_hosts or ""
    return [item.strip() for item in raw.split(",") if item.strip() and ":" in item]


def _tcp_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _normalize_proxy_url(value: str) -> str:
    if "://" in value:
        return value
    return f"http://{value}"
