from __future__ import annotations

from typing import Any

from copilot_agent.contracts.policy import PolicyDecision
from copilot_agent.credentials.audit import build_credential_audit_payload
from copilot_agent.credentials.manager import CredentialManager
from copilot_agent.scenario.schema import ScenarioPolicyConfig
from copilot_agent.settings import settings
from copilot_agent.tools.registry import ToolRegistry, ToolSpec


class PolicyRegistry:
    """Kernel PolicyGate: final allow / ask / deny for tool calls."""

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        *,
        scenario_policy: ScenarioPolicyConfig | None = None,
        credential_manager: CredentialManager | None = None,
    ) -> None:
        self._tools = tool_registry
        self._scenario_policy = scenario_policy or ScenarioPolicyConfig(
            tool_allowlist=["search_docs", "http_get", "http_post"],
        )
        self._credentials = credential_manager

    def evaluate_required_scopes(self, tool_name: str, spec: ToolSpec | None) -> PolicyDecision | None:
        """M12 unified scope adjudication for ToolSpec.required_scopes."""
        if spec is None or not spec.required_scopes:
            return None

        required = tuple(spec.required_scopes)
        binding = self._credentials.binding if self._credentials is not None else None

        if binding is None:
            audit = build_credential_audit_payload(
                action="scope_denied",
                binding=_synthetic_binding(required),
                tool_name=tool_name,
                required_scopes=required,
                reason="credential_binding_missing",
            )
            return PolicyDecision(
                allowed=False,
                decision="deny",
                message=(
                    f"Tool '{tool_name}' requires credential scopes {list(required)} "
                    "but no credential binding is configured."
                ),
                reason="credential_binding_missing",
                tool_name=tool_name,
                policy_source="credential_scope",
                metadata={"required_scopes": list(required)},
                credential_audits=[audit],
            )

        if not self._credentials.authorize_scopes(required):
            audit = build_credential_audit_payload(
                action="scope_denied",
                binding=binding,
                tool_name=tool_name,
                required_scopes=required,
                reason="credential_scope_denied",
            )
            return PolicyDecision(
                allowed=False,
                decision="deny",
                message=(
                    f"Tool '{tool_name}' requires scopes {list(required)} "
                    f"not granted by binding '{binding.binding_id}'."
                ),
                reason="credential_scope_denied",
                tool_name=tool_name,
                policy_source="credential_scope",
                metadata={"required_scopes": list(required), "binding_id": binding.binding_id},
                credential_audits=[audit],
            )

        audit = build_credential_audit_payload(
            action="scope_allowed",
            binding=binding,
            tool_name=tool_name,
            required_scopes=required,
            reason="credential_scope_allowed",
        )
        return PolicyDecision(
            allowed=True,
            decision="allow",
            reason="credential_scope_allowed",
            tool_name=tool_name,
            policy_source="credential_scope",
            metadata={"required_scopes": list(required), "binding_id": binding.binding_id},
            credential_audits=[audit],
        )

    def evaluate_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        allow_job_post: bool | None = None,
        confirm_dangerous: bool = False,
    ) -> PolicyDecision:
        allow_job_post = settings.copilot_allow_job_post if allow_job_post is None else allow_job_post
        allowlist = set(self._scenario_policy.tool_allowlist or [])
        denylist = set(self._scenario_policy.tool_denylist or [])
        dangerous_paths = {
            str(path).split("?", 1)[0]
            for path in self._scenario_policy.dangerous_paths
            if str(path).strip()
        }
        credential_audits: list[dict[str, Any]] = []

        for call in tool_calls:
            name = str(call.get("name", ""))
            call_id = str(call.get("id") or call.get("call_id") or "")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}

            if denylist and name in denylist:
                return PolicyDecision(
                    allowed=False,
                    decision="deny",
                    message=f"Tool '{name}' is denied by scenario policy.",
                    reason="scenario_tool_denied",
                    tool_name=name,
                    call_id=call_id,
                    policy_source="scenario_tool_policy",
                    credential_audits=credential_audits,
                )
            if allowlist and name not in allowlist:
                return PolicyDecision(
                    allowed=False,
                    decision="deny",
                    message=f"Tool '{name}' is not enabled for this scenario.",
                    reason="scenario_tool_not_allowed",
                    tool_name=name,
                    call_id=call_id,
                    policy_source="scenario_tool_policy",
                    credential_audits=credential_audits,
                )

            spec = self._tools.get_spec(name) if self._tools is not None else None
            scope_decision = self.evaluate_required_scopes(name, spec)
            if scope_decision is not None:
                credential_audits.extend(scope_decision.credential_audits)
                if not scope_decision.allowed:
                    return PolicyDecision(
                        allowed=False,
                        decision="deny",
                        message=scope_decision.message,
                        reason=scope_decision.reason,
                        tool_name=name,
                        call_id=call_id,
                        policy_source=scope_decision.policy_source,
                        metadata=scope_decision.metadata,
                        credential_audits=credential_audits,
                    )

            if spec is not None and spec.category == "mcp":
                server_allow = set(self._scenario_policy.mcp_server_allowlist or [])
                tool_allow = set(self._scenario_policy.mcp_tool_allowlist or [])
                if server_allow and (spec.mcp_server or "") not in server_allow:
                    return PolicyDecision(
                        allowed=False,
                        decision="deny",
                        message=f"MCP server '{spec.mcp_server}' is not enabled for this scenario.",
                        reason="scenario_mcp_server_denied",
                        tool_name=name,
                        call_id=call_id,
                        policy_source="scenario_mcp_policy",
                        metadata={"mcp_server": spec.mcp_server},
                        credential_audits=credential_audits,
                    )
                if tool_allow and name not in tool_allow and (spec.mcp_tool or "") not in tool_allow:
                    return PolicyDecision(
                        allowed=False,
                        decision="deny",
                        message=f"MCP tool '{name}' is not enabled for this scenario.",
                        reason="scenario_mcp_tool_denied",
                        tool_name=name,
                        call_id=call_id,
                        policy_source="scenario_mcp_policy",
                        metadata={"mcp_tool": spec.mcp_tool},
                        credential_audits=credential_audits,
                    )

            path = str(args.get("path", ""))
            normalized_path = path.split("?", 1)[0]

            if name == "http_post" and normalized_path in dangerous_paths:
                if not allow_job_post:
                    return PolicyDecision(
                        allowed=False,
                        decision="deny",
                        message=(
                            f"POST {normalized_path} is disabled by deployment. "
                            "Enable COPILOT_ALLOW_JOB_POST=true, then retry with explicit confirmation."
                        ),
                        reason="job_post_disabled",
                        tool_name=name,
                        call_id=call_id,
                        policy_source="deployment_policy",
                        metadata={"path": normalized_path},
                        credential_audits=credential_audits,
                    )
                if not confirm_dangerous:
                    return PolicyDecision(
                        allowed=True,
                        requires_approval=spec.requires_approval_for(args) if spec is not None else True,
                        decision="ask",
                        message=(
                            f"POST {normalized_path} is gated by scenario policy. "
                            "Approve this run to continue."
                        ),
                        reason="dangerous_tool_requires_approval",
                        tool_name=name,
                        call_id=call_id,
                        policy_source="scenario_approval_policy",
                        metadata={"path": normalized_path},
                        credential_audits=credential_audits,
                    )
        return PolicyDecision(allowed=True, decision="allow", reason="tool_calls_allowed", credential_audits=credential_audits)


def _synthetic_binding(_required_scopes: tuple[str, ...]):
    from copilot_agent.credentials.schema import CredentialBinding

    return CredentialBinding(binding_id="missing", scopes=[])
