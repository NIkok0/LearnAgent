from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PolicyDecision(BaseModel):
    allowed: bool = True
    requires_approval: bool = False
    decision: str = "allow"
    message: str = ""
    reason: str = ""
    tool_name: str = ""
    call_id: str = ""
    policy_source: str = "kernel"
    credential_audits: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
