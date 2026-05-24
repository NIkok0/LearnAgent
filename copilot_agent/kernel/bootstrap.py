from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copilot_agent.agent.tool_handlers import ToolHandlers
from copilot_agent.credentials import CredentialManager
from copilot_agent.llm import LLMProvider
from copilot_agent.memory import MemoryManager
from copilot_agent.policy import PolicyRegistry
from copilot_agent.rag import RagStore
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.scenario.loader import LoadedScenario
from copilot_agent.settings import settings
from copilot_agent.tools.capability import CapabilityContext, load_capability_packs
from copilot_agent.tools.extensions.mcp import McpRuntime
from copilot_agent.tools.http_tools import ScenarioHttpClient
from copilot_agent.scenario.http_paths import HttpPathPolicy, bind_http_path_policy
from copilot_agent.scenario.resources import resolve_api_base_url
from copilot_agent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class KernelDeps:
    """External resources injected into Kernel bootstrap (from server lifespan)."""

    rag_store: RagStore
    credential_manager: CredentialManager
    event_store: EventStore | None = None
    http: ScenarioHttpClient | None = None
    memory: MemoryManager | None = None
    llm_provider: LLMProvider | None = None
    mcp_runtime: McpRuntime | None = None


@dataclass
class KernelComponents:
    """Kernel-owned runtime components wired from Scenario + Capability packs."""

    scenario: LoadedScenario
    tool_handlers: ToolHandlers
    tool_registry: ToolRegistry
    policy: PolicyRegistry
    memory: MemoryManager
    llm_provider: LLMProvider
    mcp_runtime: McpRuntime | None = None

    @property
    def tools(self) -> list[Any]:
        return self.tool_registry.tools()


def build_kernel_components(
    scenario: LoadedScenario,
    deps: KernelDeps,
) -> KernelComponents:
    """Bootstrap Kernel layer: Scenario policy + Capability registration + PolicyGate."""
    path_policy = HttpPathPolicy.from_resources(scenario.resources)
    bind_http_path_policy(path_policy)
    llm_provider = deps.llm_provider or LLMProvider()
    memory = deps.memory or MemoryManager(
        rag_store=deps.rag_store,
        event_store=deps.event_store,
        checkpoint_path=settings.agent_checkpoint_path,
        policy=scenario.resolve_memory_policy(),
        llm_provider=llm_provider,
    )
    http = deps.http or ScenarioHttpClient(
        base_url=resolve_api_base_url(scenario.resources),
        dangerous_paths=tuple(scenario.policy.dangerous_paths or []),
        session_cookie_name=scenario.resources.credential_cookie_name,
        path_policy=path_policy,
    )
    tool_handlers = ToolHandlers(
        memory=memory,
        http=http,
        cookies=deps.credential_manager,
        scenario=scenario,
    )

    registry = ToolRegistry()
    cap_ctx = CapabilityContext(
        scenario=scenario,
        handlers=tool_handlers,
        mcp_runtime=deps.mcp_runtime,
    )
    load_capability_packs(registry, capabilities=settings.enabled_capabilities(), ctx=cap_ctx)

    policy = PolicyRegistry(
        registry,
        scenario_policy=scenario.policy,
        credential_manager=deps.credential_manager,
    )

    return KernelComponents(
        scenario=scenario,
        tool_handlers=tool_handlers,
        tool_registry=registry,
        policy=policy,
        memory=memory,
        llm_provider=llm_provider,
        mcp_runtime=deps.mcp_runtime,
    )
