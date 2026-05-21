from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ToolRouteKind = Literal[
    "knowledge",
    "live_status",
    "troubleshooting",
    "dangerous_execute",
    "safety_reject",
]


class MatchExpr(BaseModel):
    """Recursive match expression (regex / boolean composition / named predicate)."""

    match: str | None = None
    any: list[MatchExpr] = Field(default_factory=list)
    all: list[MatchExpr] = Field(default_factory=list)
    not_: MatchExpr | None = Field(default=None, alias="not")
    predicate: str | None = None
    has_uuid: bool | None = None
    confirm_dangerous: bool | None = None
    allow_job_post: bool | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DynamicPathRule(BaseModel):
    if_any: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class SuggestedPathsConfig(BaseModel):
    static: list[str] = Field(default_factory=list)
    prepend_if_uuid: list[str] = Field(default_factory=list)
    prepend: list[DynamicPathRule] = Field(default_factory=list)
    dynamic: list[DynamicPathRule] = Field(default_factory=list)


class RecommendedToolsOverride(BaseModel):
    if_any: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class RecommendedToolsConfig(BaseModel):
    default: list[str] = Field(default_factory=list)
    overrides: list[RecommendedToolsOverride] = Field(default_factory=list)


class RouterRule(BaseModel):
    id: str
    kind: ToolRouteKind
    when: MatchExpr | None = None
    recommended_tools: list[str] = Field(default_factory=list)
    recommended: RecommendedToolsConfig | None = None
    forbidden_tools: list[str] = Field(default_factory=list)
    suggested_paths: list[str] = Field(default_factory=list)
    paths: SuggestedPathsConfig | None = None
    rationale: str = ""

    model_config = ConfigDict(extra="forbid")


class RouterDefaults(BaseModel):
    kind: ToolRouteKind = "knowledge"
    recommended_tools: list[str] = Field(default_factory=lambda: ["search_docs"])
    forbidden_tools: list[str] = Field(default_factory=list)
    suggested_paths: list[str] = Field(default_factory=list)
    rationale: str = ""


class RouterRulesConfig(BaseModel):
    version: int = 1
    dangerous_job_path: str = ""
    predicates: dict[str, MatchExpr] = Field(default_factory=dict)
    empty_query: RouterDefaults | None = None
    defaults: RouterDefaults = Field(default_factory=RouterDefaults)
    rules: list[RouterRule] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
