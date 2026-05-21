from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field

from copilot_agent.rag.schema import ApiEndpoint, ApiErrorCode, ApiField

log = logging.getLogger(__name__)

_ENDPOINT_TITLE = re.compile(r"^(GET|POST)\s+(/[^\s]+)$", re.IGNORECASE)
_HEADING_LINE = re.compile(r"^#{1,6}\s+(GET|POST)\s+(/[^\s]+)\s*$", re.IGNORECASE | re.MULTILINE)
_TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)(?:\s*\|\s*([^|]*?))?\s*\|\s*$")
_TABLE_SEP = re.compile(r"^\|\s*-+")
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.IGNORECASE | re.DOTALL)
_INLINE_RESPONSE_JSON = re.compile(
    r"Response[：:]\s*`(\{[^`]+\})`",
    re.IGNORECASE,
)


def _parse_yes(value: str) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "y", "1", "required"}


@dataclass
class ApiSectionMeta:
    api_endpoint: ApiEndpoint | None = None
    request_fields: list[ApiField] = field(default_factory=list)
    response_fields: list[ApiField] = field(default_factory=list)
    error_codes: list[ApiErrorCode] = field(default_factory=list)


def _endpoint_from_title(title: str) -> ApiEndpoint | None:
    match = _ENDPOINT_TITLE.match(str(title or "").strip())
    if not match:
        return None
    return ApiEndpoint(method=match.group(1).upper(), path=match.group(2))


def _endpoint_from_text(text: str) -> ApiEndpoint | None:
    match = _HEADING_LINE.search(text)
    if not match:
        return None
    return ApiEndpoint(method=match.group(1).upper(), path=match.group(2))


def _parse_markdown_table(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        if _TABLE_SEP.match(line.strip()):
            continue
        match = _TABLE_ROW.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.groups() if cell is not None]
        rows.append(cells)
    return rows


def _parse_field_table(text: str) -> list[ApiField]:
    rows = _parse_markdown_table(text)
    if not rows:
        return []
    header = [cell.lower() for cell in rows[0]]
    if "field" not in header[0] and "name" not in header[0]:
        return []
    fields: list[ApiField] = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        name = row[0]
        if not name or name.lower() in {"field", "name"}:
            continue
        field_type = row[1] if len(row) > 1 else "string"
        required_raw = row[2] if len(row) > 2 else "no"
        description = row[3] if len(row) > 3 else ""
        fields.append(
            ApiField(
                name=name,
                field_type=field_type,
                required=_parse_yes(required_raw),
                description=description,
            )
        )
    return fields


def _json_type_name(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _fields_from_json_object(raw: object) -> list[ApiField]:
    if not isinstance(raw, dict):
        return []
    fields: list[ApiField] = []
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        fields.append(
            ApiField(
                name=name,
                field_type=_json_type_name(value),
                required=True,
                description="",
            )
        )
    return fields


def _parse_response_json_blocks(text: str) -> list[ApiField]:
    fields: list[ApiField] = []
    seen: set[str] = set()

    def _add(parsed_fields: list[ApiField]) -> None:
        for item in parsed_fields:
            if item.name in seen:
                continue
            seen.add(item.name)
            fields.append(item)

    for match in _JSON_FENCE.finditer(text):
        block = match.group(1).strip()
        if not block.startswith("{"):
            continue
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        _add(_fields_from_json_object(parsed))

    for match in _INLINE_RESPONSE_JSON.finditer(text):
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        _add(_fields_from_json_object(parsed))

    return fields


def _parse_error_table(text: str) -> list[ApiErrorCode]:
    rows = _parse_markdown_table(text)
    if not rows:
        return []
    header = [cell.lower() for cell in rows[0]]
    if not any("http" in cell or "code" in cell for cell in header):
        return []
    codes: list[ApiErrorCode] = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        http_raw, code, meaning = row[0], row[1], row[2]
        if code.lower() == "code":
            continue
        try:
            http_status = int(http_raw)
        except ValueError:
            continue
        if not code:
            continue
        codes.append(ApiErrorCode(http_status=http_status, code=code, meaning=meaning))
    return codes


def parse_api_section(
    *,
    section_title: str,
    text: str,
    heading_path: str = "",
) -> ApiSectionMeta:
    endpoint = _endpoint_from_title(section_title) or _endpoint_from_text(text)
    request_fields = _parse_field_table(text)
    response_fields = _parse_response_json_blocks(text)
    error_codes: list[ApiErrorCode] = []

    if "error model" in section_title.lower() or heading_path.lower().endswith("error model"):
        error_codes = _parse_error_table(text)
    elif endpoint is None and _parse_error_table(text):
        error_codes = _parse_error_table(text)

    return ApiSectionMeta(
        api_endpoint=endpoint,
        request_fields=request_fields,
        response_fields=response_fields,
        error_codes=error_codes,
    )
