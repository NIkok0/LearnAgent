from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ScenarioEvalPaths(BaseModel):
    golden: str | None = None
    rag_cases: str | None = None


class ScenarioBudgets(BaseModel):
    max_context_tokens: int = 24000
    max_context_chars: int | None = None
    max_tool_calls: int = 8
    max_run_seconds: int = 120
    max_graph_rounds: int = 12


class ScenarioPolicyConfig(BaseModel):
    """Scenario-side policy overlay; may only tighten Kernel defaults."""

    tool_allowlist: list[str] = Field(default_factory=list)
    tool_denylist: list[str] = Field(default_factory=list)
    dangerous_paths: list[str] = Field(default_factory=list)
    require_approval_tools: list[str] = Field(default_factory=list)
    mcp_server_allowlist: list[str] = Field(default_factory=list)
    mcp_tool_allowlist: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ScenarioResourcesConfig(BaseModel):
    api_base_url_env: str = "API_BASE_URL"
    api_base_url: str | None = None
    default_api_base_url: str = ""
    docs_path_env: str = "COPILOT_DOCS_PATH"
    credential_binding: str = "default"
    credential_cookie_name: str = ""
    credential_provider: str = ""
    credential_scopes: list[str] = Field(default_factory=lambda: ["http:read", "http:write"])
    default_tenant_id: str = "default"
    default_max_classification: str = "internal"
    rag_allowed_scopes: list[str] = Field(default_factory=list)
    rag_embedding_model: str | None = None
    docs_fallback: str = ""
    rag_rules: str | None = None
    diagnosis: str | None = None
    http_get_actuator_paths: list[str] = Field(default_factory=lambda: ["/actuator/health"])
    http_get_patterns: list[str] = Field(default_factory=list)
    http_post_paths: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ScenarioConfig(BaseModel):
    """Business overlay: policy, prompt, router, docs binding — not capability installation."""

    name: str
    description: str = ""
    policy: ScenarioPolicyConfig | None = None
    policy_file: str | None = None
    prompt: str | None = None
    prompt_file: str | None = None
    router: str | None = None
    mcp: str | None = None
    docs_dir: str | None = None
    resources: ScenarioResourcesConfig | None = None
    memory_policy: str | dict[str, object] | None = None
    eval: ScenarioEvalPaths = Field(default_factory=ScenarioEvalPaths)
    budgets: ScenarioBudgets = Field(default_factory=ScenarioBudgets)

    model_config = ConfigDict(extra="ignore")
