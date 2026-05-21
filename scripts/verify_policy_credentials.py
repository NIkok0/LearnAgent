#!/usr/bin/env python
"""Verify M14 CredentialManager + M12 PolicyGate scopes and credential_binding_audit events."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.validate import validate_stored_event  # noqa: E402
from copilot_agent.credentials import CredentialBinding, CredentialManager  # noqa: E402
from copilot_agent.credentials.audit import build_credential_audit_payload  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_CREDENTIAL_BINDING_AUDIT  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402
from copilot_agent.tools.capability import CapabilityContext, load_capability_packs  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class _StubHandlers:
    async def search_docs(self, query: str, config=None):
        return {"excerpts_markdown": query}

    async def http_get(self, path: str, cookie_header=None, config=None):
        return {"path": path}

    async def http_post(self, path: str, json_body=None, cookie_header=None, idempotency_key=None, config=None):
        return {"path": path}


def _bootstrap_watermark():
    scenario = load_scenario("watermark")
    apply_scenario_environment(scenario)
    return scenario


def _build_registry(scenario) -> ToolRegistry:
    registry = ToolRegistry()
    load_capability_packs(
        registry,
        capabilities=("rag", "http"),
        ctx=CapabilityContext(scenario=scenario, handlers=_StubHandlers()),
    )
    return registry


def run_credentials_checks(scenario) -> dict[str, bool]:
    manager = CredentialManager.from_scenario_resources(scenario.resources, ttl_seconds=120)
    thread_id = "cred-verify-thread"
    manager.set_cookie(thread_id, user_id="user-1", cookie_header="WMSESSIONID=secret-value")
    stored = manager.get_cookie(thread_id, required_scopes=("http:read",))
    denied = manager.get_cookie(thread_id, required_scopes=("admin:delete",))

    return {
        "binding_id_from_scenario": manager.binding_id == "wmsession",
        "binding_scopes": "http:read" in manager.binding.scopes,
        "store_round_trip": stored == "WMSESSIONID=secret-value",
        "scope_gate_denies_unknown": denied is None,
        "audit_payload_no_secret": "secret" not in json.dumps(manager.audit_ref(thread_id=thread_id)),
        "schema_validates": CredentialBinding.model_validate(
            {
                "binding_id": "demo",
                "provider": "demo_api",
                "credential_type": "cookie",
                "scopes": ["http:read"],
            }
        ).binding_id
        == "demo",
        "mcp_server_outside_kernel": (ROOT / "scenarios/watermark/mcp/watermark_ops.py").is_file(),
        "legacy_shim_removed": not (
            ROOT / "copilot_agent/tools/extensions/mcp/servers/watermark_ops.py"
        ).exists(),
    }


def run_policy_audit_checks(scenario) -> dict[str, bool]:
    registry = _build_registry(scenario)

    full_manager = CredentialManager.from_scenario_resources(scenario.resources, ttl_seconds=120)
    read_only_manager = CredentialManager.from_scenario_resources(
        scenario.resources.model_copy(update={"credential_scopes": ["http:read"]}),
        ttl_seconds=120,
    )

    gate_ok = PolicyRegistry(registry, scenario_policy=scenario.policy, credential_manager=full_manager)
    gate_read_only = PolicyRegistry(registry, scenario_policy=scenario.policy, credential_manager=read_only_manager)
    gate_no_binding = PolicyRegistry(registry, scenario_policy=scenario.policy, credential_manager=None)

    http_get_call = [{"name": "http_get", "args": {"path": "/api/v1/users/me"}}]
    http_post_call = [{"name": "http_post", "args": {"path": "/api/v1/auth/login", "json_body": {}}}]
    docs_call = [{"name": "search_docs", "args": {"query": "health"}}]

    allow_get = gate_ok.evaluate_tool_calls(http_get_call)
    deny_post_read_only = gate_read_only.evaluate_tool_calls(http_post_call)
    allow_docs = gate_ok.evaluate_tool_calls(docs_call)
    deny_get_no_binding = gate_no_binding.evaluate_tool_calls(http_get_call)

    db_path = ROOT / "artifacts/runtime/policy-credential-audit-events.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.is_file():
        db_path.unlink()
    event_store = EventStore(str(db_path))
    thread_id = "policy-cred-audit-thread"
    event_store.ensure_thread(thread_id)
    run = event_store.create_run(thread_id, run_id="policy-cred-audit-run")
    run_id = str(run["id"])
    event_store.update_run_status(run_id, RUN_STATUS_RUNNING)

    decision = gate_ok.evaluate_tool_calls(http_get_call)
    for audit in decision.credential_audits:
        event_store.append_event(thread_id, run_id, EVENT_CREDENTIAL_BINDING_AUDIT, audit)

    event_store.append_event(
        thread_id,
        run_id,
        EVENT_CREDENTIAL_BINDING_AUDIT,
        build_credential_audit_payload(
            action="credential_set",
            binding=full_manager.binding,
            reason="verify_script",
            user_id="verify-user",
        ),
    )

    stored = event_store.list_run_events(run_id)
    audit_rows = [row for row in stored if row.get("type") == EVENT_CREDENTIAL_BINDING_AUDIT]
    contract_ok = all(
        validate_stored_event(kind=EVENT_CREDENTIAL_BINDING_AUDIT, payload=row.get("payload") or {})
        for row in audit_rows
    )

    http_get_spec = registry.get_spec("http_get")
    http_post_spec = registry.get_spec("http_post")

    return {
        "http_get_declares_read_scope": http_get_spec is not None and http_get_spec.required_scopes == ("http:read",),
        "http_post_declares_write_scope": http_post_spec is not None
        and http_post_spec.required_scopes == ("http:write",),
        "scope_allowed_emits_audit": any(a.get("action") == "scope_allowed" for a in allow_get.credential_audits),
        "scope_denied_on_write_with_read_only_binding": (
            not deny_post_read_only.allowed and deny_post_read_only.reason == "credential_scope_denied"
        ),
        "docs_has_no_scope_audit": allow_docs.credential_audits == [],
        "missing_binding_denies_http_get": (
            not deny_get_no_binding.allowed and deny_get_no_binding.reason == "credential_binding_missing"
        ),
        "eventstore_writes_audit_rows": len(audit_rows) >= 2,
        "audit_payload_contract_valid": contract_ok,
        "audit_payload_has_no_secret": "secret" not in json.dumps(audit_rows).lower(),
    }


def _emit_summary(suite_name: str, checks: dict[str, bool], summary_filename: str, pass_key: str) -> int:
    passed = all(checks.values())
    summary_path = ROOT / "artifacts/runtime" / summary_filename
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps({"suite_name": suite_name, "status": "PASS" if passed else "FAIL", "checks": checks}, indent=2),
        encoding="utf-8",
    )
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"{pass_key}={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def main(section: str = "all") -> int:
    scenario = _bootstrap_watermark()
    checks: dict[str, bool] = {}

    if section in {"all", "credentials"}:
        cred = run_credentials_checks(scenario)
        if section == "credentials":
            return _emit_summary("credentials_m14", cred, "credentials-m14-summary.json", "verify_policy_credentials")
        checks.update({f"credentials_{key}": value for key, value in cred.items()})

    if section in {"all", "policy"}:
        policy = run_policy_audit_checks(scenario)
        if section == "policy":
            return _emit_summary(
                "policy_credential_audit",
                policy,
                "policy-credential-audit-summary.json",
                "verify_policy_credentials",
            )
        checks.update({f"policy_{key}": value for key, value in policy.items()})

    return _emit_summary(
        "policy_credentials",
        checks,
        "policy-credentials-summary.json",
        "verify_policy_credentials",
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verify credential binding and policy scope audits.")
    parser.add_argument(
        "--section",
        choices=["all", "credentials", "policy"],
        default="all",
        help="Run all checks or a single legacy section.",
    )
    args = parser.parse_args()
    raise SystemExit(main(section=args.section))
