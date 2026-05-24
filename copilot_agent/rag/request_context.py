from __future__ import annotations

from typing import Any

from copilot_agent.contracts.retrieval import RetrievalRequest
from copilot_agent.credentials import CredentialManager


def merge_retrieval_scopes(
    *,
    credential_manager: CredentialManager | None = None,
    scenario: Any | None = None,
    explicit_scopes: list[str] | None = None,
    user_id: str = "",
    tenant_id: str = "default",
) -> list[str]:
    """Build RAG ACL scopes from credential binding + scenario rag_allowed_scopes."""
    scopes: list[str] = []
    if explicit_scopes:
        scopes.extend(str(item) for item in explicit_scopes if str(item).strip())
    if credential_manager is not None:
        scopes.extend(str(item) for item in credential_manager.binding.scopes if str(item).strip())
    resources = getattr(scenario, "resources", None) if scenario is not None else None
    if resources is not None:
        tenant_id = str(getattr(resources, "default_tenant_id", None) or tenant_id)
        rag_scopes = getattr(resources, "rag_allowed_scopes", None) or []
        scopes.extend(str(item) for item in rag_scopes if str(item).strip())
    if user_id:
        scopes.append(f"user:{user_id}")
    if tenant_id:
        scopes.append(f"tenant:{tenant_id}")
    return list(dict.fromkeys(scopes))


def build_retrieval_request(
    *,
    query: str,
    ctx: dict[str, Any] | None = None,
    user_id: str = "local_user",
    purpose: str = "agent_context",
    defaults: dict[str, Any] | None = None,
    credential_manager: CredentialManager | None = None,
    scenario: Any | None = None,
) -> RetrievalRequest:
    context = dict(defaults or {})
    if ctx:
        context.update({key: value for key, value in ctx.items() if value is not None})
    tenant_id = str(context.get("tenant_id") or "default")
    explicit = context.get("allowed_scopes")
    explicit_scopes = [str(item) for item in explicit] if isinstance(explicit, list) else None
    allowed_scopes = merge_retrieval_scopes(
        credential_manager=credential_manager,
        scenario=scenario,
        explicit_scopes=explicit_scopes,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    return RetrievalRequest(
        tenant_id=tenant_id,
        user_id=user_id or "local_user",
        query=query,
        purpose=str(context.get("retrieval_purpose") or purpose),
        max_classification=str(context.get("max_classification") or "internal"),  # type: ignore[arg-type]
        allowed_scopes=allowed_scopes,
        allow_high_pii=bool(context.get("allow_high_pii", False)),
    )


def retrieval_defaults_from_scenario(
    scenario: Any,
    *,
    credential_manager: CredentialManager | None = None,
    thread_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    resources = getattr(scenario, "resources", None)
    if resources is None:
        tenant_id = "default"
        max_classification = "internal"
    else:
        tenant_id = str(getattr(resources, "default_tenant_id", None) or "default")
        max_classification = str(getattr(resources, "default_max_classification", None) or "internal")
    return {
        "tenant_id": tenant_id,
        "max_classification": max_classification,
        "allowed_scopes": merge_retrieval_scopes(
            credential_manager=credential_manager,
            scenario=scenario,
            user_id=user_id,
            tenant_id=tenant_id,
        ),
    }
