from __future__ import annotations

AUTHORITY_BY_DOC_TYPE: dict[str, int] = {
    "api_contract": 90,
    "requirements": 90,
    "deploy": 80,
    "runbook": 80,
    "security": 80,
    "tech_selection": 75,
    "operations": 75,
    "algorithm": 75,
    "overview": 50,
    "doc": 50,
}


def resolve_authority(*, doc_type: str, security_meta: dict[str, object] | None) -> int:
    security = security_meta or {}
    raw = security.get("authority")
    if raw is not None:
        try:
            value = int(raw)
            return max(0, min(100, value))
        except (TypeError, ValueError):
            pass
    return AUTHORITY_BY_DOC_TYPE.get(doc_type, 50)
