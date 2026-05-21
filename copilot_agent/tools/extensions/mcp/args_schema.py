from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model

from copilot_agent.tools.extensions.mcp.schema import McpToolDefinition


def build_mcp_args_schema(server_name: str, tool: McpToolDefinition) -> type[BaseModel]:
    """Build a Pydantic args schema from MCP tool input_schema (JSON Schema subset)."""
    properties = {}
    if isinstance(tool.input_schema, dict):
        properties = tool.input_schema.get("properties") or {}
    required = set(tool.input_schema.get("required") or []) if isinstance(tool.input_schema, dict) else set()

    if not properties:
        model_name = f"Mcp_{server_name}_{tool.name}_Args".replace("-", "_")
        return create_model(  # type: ignore[call-overload]
            model_name,
            payload=(dict[str, Any], Field(default_factory=dict, description="Tool arguments")),
            __base__=BaseModel,
        )

    fields: dict[str, Any] = {}
    for field_name, spec in properties.items():
        if not isinstance(spec, dict):
            spec = {}
        description = str(spec.get("description") or "")
        if field_name in required:
            fields[field_name] = (str, Field(description=description))
        else:
            fields[field_name] = (str | None, Field(default=None, description=description))

    model_name = f"Mcp_{server_name}_{tool.name}_Args".replace("-", "_")
    return create_model(model_name, **fields, __base__=BaseModel)  # type: ignore[call-overload]
