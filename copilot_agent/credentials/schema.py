from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CredentialBinding(BaseModel):
    """M14: metadata for a Scenario credential reference (never stores secret values)."""

    binding_id: str
    tenant_id: str | None = None
    user_id: str = ""
    thread_id: str | None = None
    provider: str = "scenario"
    credential_type: Literal["cookie", "api_key", "oauth_token", "session"] = "cookie"
    scopes: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    storage: Literal["memory", "encrypted_store", "external_secret_manager"] = "memory"
    audit_required: bool = True

    model_config = ConfigDict(extra="forbid")

    def audit_payload(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "provider": self.provider,
            "credential_type": self.credential_type,
            "scopes": list(self.scopes),
            "storage": self.storage,
            "thread_id": self.thread_id,
            "user_id": self.user_id or None,
        }
