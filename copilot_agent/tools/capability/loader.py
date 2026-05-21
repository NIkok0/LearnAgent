from __future__ import annotations

import logging

from copilot_agent.tools.capability.base import CapabilityContext, CapabilityPack
from copilot_agent.tools.capability.http import HttpCapability
from copilot_agent.tools.capability.mcp import McpCapability
from copilot_agent.tools.capability.rag import RagCapability
from copilot_agent.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

CAPABILITY_PACKS: dict[str, CapabilityPack] = {
    "rag": RagCapability(),
    "http": HttpCapability(),
    "mcp": McpCapability(),
}


def load_capability_packs(
    registry: ToolRegistry,
    *,
    capabilities: tuple[str, ...],
    ctx: CapabilityContext,
) -> ToolRegistry:
    """Register enabled capability packs declared by Scenario onto ToolRegistry."""
    enabled = {item.lower() for item in capabilities}
    for name in enabled:
        pack = CAPABILITY_PACKS.get(name)
        if pack is None:
            log.warning("unknown capability %r — skipped", name)
            continue
        pack.register(registry, ctx)
        log.debug("registered capability pack %s", name)
    return registry
