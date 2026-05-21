from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from copilot_agent.scenario.overlay_types import DiagnosisTemplate

_DIAGNOSIS_MARKER = "Diagnosis outline for final answer"

_DEFAULT_UNKNOWN = DiagnosisTemplate(
    doc_causes=("任务状态未知或未返回 status 字段", "需结合文档与实时 API 响应判断"),
    doc_sources=("RUNBOOK.md",),
    checklist=("先检索相关文档", "再调用 API 获取实时状态"),
)

_STATUS_TEMPLATES: dict[str, DiagnosisTemplate] = {
    "UNKNOWN": _DEFAULT_UNKNOWN,
}


def configure_diagnosis_templates(templates: dict[str, DiagnosisTemplate] | None) -> None:
    global _STATUS_TEMPLATES
    if not templates:
        _STATUS_TEMPLATES = {"UNKNOWN": _DEFAULT_UNKNOWN}
        return
    merged = dict(templates)
    merged.setdefault("UNKNOWN", _DEFAULT_UNKNOWN)
    _STATUS_TEMPLATES = merged


@dataclass(frozen=True)
class DiagnosisOutline:
    status: str
    doc_causes: tuple[str, ...]
    doc_sources: tuple[str, ...]
    checklist: tuple[str, ...]
    live_status: str = ""
    live_error_code: str = ""

    def to_system_message(self) -> str:
        lines = [
            _DIAGNOSIS_MARKER,
            "Structure your final answer with these markdown sections:",
            "## 文档依据",
            "## 当前任务状态",
            "## 可能原因",
            "## 建议排查步骤",
            "",
            f"Detected job status: {self.status}",
        ]
        if self.live_status:
            lines.append(f"Live API status field: {self.live_status}")
        if self.live_error_code:
            lines.append(f"Live API errorCode: {self.live_error_code}")
        lines.extend(
            [
                "",
                "Document causes (cite filenames when used):",
                *[f"- {item}" for item in self.doc_causes],
                "",
                "Preferred doc sources:",
                *[f"- {item}" for item in self.doc_sources],
                "",
                "Checklist:",
                *[f"- {item}" for item in self.checklist],
            ]
        )
        return "\n".join(lines)


def _normalize_status(raw: str) -> str:
    value = str(raw or "").strip().upper()
    if value in _STATUS_TEMPLATES:
        return value
    return "UNKNOWN"


def _extract_job_fields_from_messages(messages: list[BaseMessage]) -> tuple[str, str]:
    status = ""
    error_code = ""
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.name != "http_get":
            continue
        payload = _parse_tool_payload(message.content)
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload.get("body")
        if not isinstance(body, dict):
            body = payload if isinstance(payload, dict) else {}
        if isinstance(body.get("body"), dict):
            body = body["body"]
        status = str(body.get("status") or status or "")
        error_code = str(body.get("errorCode") or body.get("error_code") or error_code or "")
        if status or error_code:
            break
    return status, error_code


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}
        return parsed if isinstance(parsed, dict) else {"text": content}
    return {}


def _extract_retrieval_sources(messages: list[BaseMessage]) -> list[str]:
    sources: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.name != "search_docs":
            continue
        payload = _parse_tool_payload(message.content)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            for item in data.get("sources") or []:
                sources.append(str(item))
            for hint in data.get("suggested_api_paths") or []:
                if isinstance(hint, dict) and hint.get("source_file"):
                    sources.append(str(hint["source_file"]))
        break
    return list(dict.fromkeys(sources))


def has_diagnosis_outline(messages: list[BaseMessage]) -> bool:
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str) and _DIAGNOSIS_MARKER in content:
            return True
    return False


def should_inject_diagnosis(*, route_kind: str, messages: list[BaseMessage]) -> bool:
    if route_kind != "troubleshooting":
        return False
    tool_names = [getattr(message, "name", "") for message in messages if isinstance(message, ToolMessage)]
    if "search_docs" not in tool_names:
        return False
    if "http_get" not in tool_names:
        return False
    if has_diagnosis_outline(messages):
        return False
    last = messages[-1] if messages else None
    return isinstance(last, ToolMessage)


def build_diagnosis_outline(
    *,
    route_kind: str,
    messages: list[BaseMessage],
    question: str = "",
) -> DiagnosisOutline | None:
    if not should_inject_diagnosis(route_kind=route_kind, messages=messages):
        return None

    live_status, live_error = _extract_job_fields_from_messages(messages)
    inferred = live_status or _infer_status_from_question(question)
    status = _normalize_status(inferred)
    template = _STATUS_TEMPLATES.get(status, _STATUS_TEMPLATES["UNKNOWN"])

    retrieval_sources = _extract_retrieval_sources(messages)
    doc_sources = tuple(dict.fromkeys([*retrieval_sources, *template.doc_sources]))

    return DiagnosisOutline(
        status=status,
        doc_causes=template.doc_causes,
        doc_sources=doc_sources,
        checklist=template.checklist,
        live_status=live_status,
        live_error_code=live_error,
    )


def _infer_status_from_question(question: str) -> str:
    text = question or ""
    if re.search(r"\bQUEUED\b", text, flags=re.IGNORECASE):
        return "QUEUED"
    if re.search(r"\bPROCESSING\b", text, flags=re.IGNORECASE):
        return "PROCESSING"
    if re.search(r"\bFAILED\b|失败", text, flags=re.IGNORECASE):
        return "FAILED"
    return "UNKNOWN"
