from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PlanStepStatus = Literal["pending", "in_progress", "completed", "skipped", "failed"]


class PlanStepModel(BaseModel):
    id: str
    goal: str
    tool_hint: str | None = None
    status: PlanStepStatus = "pending"
    outcome: str | None = None


class PlanModel(BaseModel):
    goal: str = ""
    route_kind: str | None = None
    steps: list[PlanStepModel] = Field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return self.model_dump()
