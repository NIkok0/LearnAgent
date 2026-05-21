from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from copilot_agent.contracts.envelope import EVENT_SCHEMA_VERSION, envelope_payload, payload_schema_version


class CorrelationIds(BaseModel):
    thread_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    tool_call_id: str | None = None


class RuntimeEvent(BaseModel):
    """Cross-boundary runtime event envelope (mapper -> runner -> EventStore -> SSE)."""

    schema_version: int = EVENT_SCHEMA_VERSION
    kind: str
    correlation: CorrelationIds = Field(default_factory=CorrelationIds)
    meta: dict[str, Any] = Field(default_factory=dict)
    content: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def type(self) -> str:
        """Legacy alias matching former DomainEvent['type']."""
        return self.kind

    @property
    def payload(self) -> dict[str, Any]:
        """Legacy flat payload for callers still using DomainEvent shape."""
        return self.to_store_payload()

    def to_store_payload(self) -> dict[str, Any]:
        """Flatten to EventStore / SSE JSON (backward compatible with pre-Phase-2 clients)."""
        out: dict[str, Any] = dict(self.data)
        if self.content is not None:
            if self.kind == "token":
                out["text"] = self.content
            elif self.kind == "error" and "error" not in out:
                out["error"] = self.content
            elif "message" not in out and "text" not in out:
                out["message"] = self.content
        if self.meta:
            for key, value in self.meta.items():
                out.setdefault(key, value)
        out["schema_version"] = self.schema_version
        return envelope_payload(self.kind, out)

    @classmethod
    def from_payload(
        cls,
        kind: str,
        payload: dict[str, Any] | None,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> RuntimeEvent:
        raw = dict(payload or {})
        schema_version = payload_schema_version(raw) or EVENT_SCHEMA_VERSION
        if "schema_version" in raw:
            raw = {k: v for k, v in raw.items() if k != "schema_version"}

        content: str | None = None
        if kind == "token" and "text" in raw:
            content = str(raw.pop("text"))
        elif kind == "error" and "error" in raw and len(raw) == 1:
            content = str(raw.get("error"))

        call_id = tool_call_id or raw.get("call_id")
        if call_id is not None:
            call_id = str(call_id)

        return cls(
            schema_version=schema_version,
            kind=kind,
            correlation=CorrelationIds(
                thread_id=thread_id,
                run_id=run_id,
                trace_id=trace_id,
                tool_call_id=call_id,
            ),
            data=raw,
            content=content,
        )

    @classmethod
    def from_stored(
        cls,
        *,
        kind: str,
        payload: dict[str, Any] | None,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> RuntimeEvent:
        return cls.from_payload(kind, payload, thread_id=thread_id, run_id=run_id)
