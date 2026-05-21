from __future__ import annotations

from typing import Any, Literal

from copilot_agent.credentials.schema import CredentialBinding

CredentialAuditAction = Literal[
    "scope_allowed",
    "scope_denied",
    "credential_set",
    "credential_read_denied",
]


def build_credential_audit_payload(
    *,
    action: CredentialAuditAction,
    binding: CredentialBinding,
    tool_name: str = "",
    required_scopes: tuple[str, ...] | list[str] = (),
    reason: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """Audit payload for EventStore — never includes secret values."""
    payload: dict[str, Any] = {
        "action": action,
        "binding_id": binding.binding_id,
        "provider": binding.provider,
        "credential_type": binding.credential_type,
        "granted_scopes": list(binding.scopes),
        "required_scopes": list(required_scopes),
    }
    if tool_name:
        payload["tool_name"] = tool_name
    if reason:
        payload["reason"] = reason
    if user_id:
        payload["user_id"] = user_id
    elif binding.user_id:
        payload["user_id"] = binding.user_id
    return payload
