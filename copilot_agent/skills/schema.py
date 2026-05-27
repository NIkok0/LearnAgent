from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillTrigger(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class SkillSpec(BaseModel):
    name: str
    description: str = ""
    triggers: SkillTrigger = Field(default_factory=SkillTrigger)
    instructions: str = ""
    tool_allowlist: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    docs_dir: str | None = None
    risk_level: Literal["low", "medium", "high"] = "low"

    model_config = ConfigDict(extra="forbid")

    def public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "triggers": self.triggers.model_dump(),
            "tool_allowlist": list(self.tool_allowlist),
            "required_capabilities": list(self.required_capabilities),
            "docs_dir": self.docs_dir,
            "risk_level": self.risk_level,
        }
