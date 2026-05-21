from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

_DIAGNOSIS_MARKER = "Diagnosis outline for final answer"

_STATUS_TEMPLATES: dict[str, dict[str, object]] = {
    "QUEUED": {
        "doc_causes": [
            "Worker 进程未启动或未订阅 Redis Stream",
            "Redis Stream 消费者组异常或 pending 堆积",
            "Worker 环境变量 WM_JOBS_* 配置错误",
        ],
        "doc_sources": ["DEPLOY-SERVER.md", "watermark-java-backend-tech-selection.md", "RUNBOOK.md"],
        "checklist": [
            "检查 GET /actuator/health 确认 Java API 存活",
            "确认 Worker 已启动并连接 WM_JOBS_STREAM",
            "检查 Redis Stream pending 与消费者组 wm-workers",
            "核对 WM_JOBS_GROUP / WM_JOBS_WORKER_COUNT 环境变量",
        ],
    },
    "PROCESSING": {
        "doc_causes": [
            "算法推理或文件下载耗时较长",
            "对象存储读取慢导致处理停滞",
            "Worker 卡住或未更新任务进度",
        ],
        "doc_sources": ["RUNBOOK.md", "README_ALGORITHM.md"],
        "checklist": [
            "查询任务详情确认 status=PROCESSING 与 updatedAt",
            "检查对象存储连通性与文件大小",
            "查看 Worker 日志是否有算法或下载超时",
        ],
    },
    "FAILED": {
        "doc_causes": [
            "文件格式不在支持列表",
            "对象存储读取失败",
            "算法异常或请求参数非法",
        ],
        "doc_sources": ["RUNBOOK.md", "API-CONTRACT.md", "README_ALGORITHM.md"],
        "checklist": [
            "读取任务 errorCode / errorMessage 字段",
            "对照 API Error Model 与 RUNBOOK 失败表格",
            "确认 fileId 与 algorithmType 合法",
        ],
    },
    "UNKNOWN": {
        "doc_causes": [
            "任务状态未知或未返回 status 字段",
            "需结合 Runbook 与实时 API 响应判断",
        ],
        "doc_sources": ["RUNBOOK.md", "DEPLOY-SERVER.md"],
        "checklist": [
            "先检索部署/Runbook 文档",
            "再调用 GET /api/v1/jobs/{id} 获取实时状态",
        ],
    },
}


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
    template = _STATUS_TEMPLATES[status]

    retrieval_sources = _extract_retrieval_sources(messages)
    doc_sources = tuple(dict.fromkeys([*retrieval_sources, *template["doc_sources"]]))  # type: ignore[list-item]

    return DiagnosisOutline(
        status=status,
        doc_causes=tuple(template["doc_causes"]),  # type: ignore[arg-type]
        doc_sources=doc_sources,
        checklist=tuple(template["checklist"]),  # type: ignore[arg-type]
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
