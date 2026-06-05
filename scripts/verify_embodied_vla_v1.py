#!/usr/bin/env python
"""Verify Embodied/VLA capability skeleton without real robot hardware."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.credentials import CredentialManager  # noqa: E402
from copilot_agent.credentials.schema import CredentialBinding  # noqa: E402
from copilot_agent.credentials.store import InMemoryCredentialStore  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.runtime.event_schema import (  # noqa: E402
    EVENT_ROBOT_ACTION_EXECUTED,
    EVENT_ROBOT_EPISODE_LABELED,
    EVENT_ROBOT_OBSERVATION_RECORDED,
    EVENT_VLA_POLICY_INFERRED,
)
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.scenario import load_scenario, scenario_status  # noqa: E402
from copilot_agent.tools.capability import CapabilityContext, load_capability_packs  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


async def _run_tool(registry: ToolRegistry, name: str, args: dict) -> dict:
    tools = {tool.name: tool for tool in registry.tools()}
    result = await tools[name].ainvoke(args)
    return result if isinstance(result, dict) else {"success": False, "error": "non_dict_result"}


async def verify() -> dict:
    scenario = load_scenario("embodied_so101")
    registry = ToolRegistry()
    load_capability_packs(
        registry,
        capabilities=("embodied",),
        ctx=CapabilityContext(scenario=scenario, handlers=None),
    )
    binding = CredentialBinding(
        binding_id="robot_local",
        provider="local_robot_lab",
        credential_type="session",
        scopes=["robot:observe", "robot:infer", "robot:move", "robot:stop", "robot:label"],
    )
    gate = PolicyRegistry(
        registry,
        scenario_policy=scenario.policy,
        credential_manager=CredentialManager(binding=binding, store=InMemoryCredentialStore(ttl_seconds=3600)),
    )

    observation = await _run_tool(
        registry,
        "observe_scene",
        {"instruction": "pick the red block and place it in the box"},
    )
    obs_data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
    policy = await _run_tool(
        registry,
        "run_vla_policy",
        {
            "instruction": "pick the red block and place it in the box",
            "frame_id": obs_data.get("frame_id"),
            "max_steps": 4,
        },
    )
    policy_data = policy.get("data") if isinstance(policy.get("data"), dict) else {}
    action_chunk = policy_data.get("action_chunk") if isinstance(policy_data.get("action_chunk"), list) else []
    execution = await _run_tool(
        registry,
        "execute_action_chunk",
        {"action_chunk": action_chunk, "idempotency_key": "embodied-demo-1"},
    )
    label = await _run_tool(
        registry,
        "label_episode_result",
        {"episode_id": "episode-001", "success": True},
    )
    stop = await _run_tool(registry, "stop_robot", {"reason": "verify_complete"})

    move_decision = gate.evaluate_tool_calls(
        [{"name": "execute_action_chunk", "args": {"action_chunk": action_chunk}}],
        confirm_dangerous=False,
    )
    stop_decision = gate.evaluate_tool_calls([{"name": "stop_robot", "args": {"reason": "test"}}])

    store_path = ROOT / "artifacts/runtime/embodied-vla-v1-events.sqlite"
    store = EventStore(str(store_path))
    thread_id = f"embodied-{uuid.uuid4().hex[:8]}"
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)

    obs_payload = {
        "robot_type": scenario.resources.robot_type,
        "adapter": scenario.resources.robot_adapter,
        "frame_id": obs_data.get("frame_id"),
        "input_image_hash": obs_data.get("input_image_hash"),
        "sensor": scenario.resources.robot_camera,
        "object_count": len(obs_data.get("objects") or []),
        "workspace_bounds": obs_data.get("workspace_bounds") or {},
        "raw_image_recorded": False,
    }
    policy_payload = {
        "policy_checkpoint_id": policy_data.get("policy_checkpoint_id"),
        "frame_id": policy_data.get("frame_id"),
        "instruction_hash": policy_data.get("instruction_hash"),
        "action_count": len(action_chunk),
        "confidence": policy_data.get("confidence"),
        "fallback_reason": policy_data.get("fallback_reason") or "",
        "model_family": "smolvla",
    }
    exec_data = execution.get("data") if isinstance(execution.get("data"), dict) else {}
    action_payload = {
        "policy_checkpoint_id": policy_data.get("policy_checkpoint_id"),
        "frame_id": policy_data.get("frame_id"),
        "action_count": len(action_chunk),
        "execution_status": exec_data.get("execution_status") or "completed",
        "success": bool(execution.get("success")),
        "safety_decision": "allow",
        "reason": exec_data.get("reason") or "",
        "idempotency_key": exec_data.get("idempotency_key"),
    }
    label_data = label.get("data") if isinstance(label.get("data"), dict) else {}
    label_payload = {
        "episode_id": label_data.get("episode_id"),
        "success": label_data.get("label") == "success",
        "failure_type": label_data.get("failure_type") or "",
        "policy_checkpoint_id": policy_data.get("policy_checkpoint_id"),
        "dataset_dir": scenario.resources.robot_dataset_dir,
    }

    for kind, payload in (
        (EVENT_ROBOT_OBSERVATION_RECORDED, obs_payload),
        (EVENT_VLA_POLICY_INFERRED, policy_payload),
        (EVENT_ROBOT_ACTION_EXECUTED, action_payload),
        (EVENT_ROBOT_EPISODE_LABELED, label_payload),
    ):
        validate_payload_for_kind(kind, payload)
        store.append_event(thread_id, run_id, kind, payload)
    store.complete_run(run_id)
    timeline = TimelineProjector().project_run(store.get_run(run_id) or {}, store.list_run_events(run_id))

    robot_summary = timeline.get("debugger", {}).get("robot_rollout", {})
    checks = {
        "scenario_loaded": scenario.name == "embodied_so101",
        "scenario_robot_resources": scenario.resources.robot_type == "SO-ARM101"
        and scenario.resources.robot_camera == "rgbd",
        "registry_tools": {
            "observe_scene",
            "run_vla_policy",
            "execute_action_chunk",
            "stop_robot",
            "recover_home",
            "label_episode_result",
        }.issubset(set(registry.names())),
        "tool_scopes_declared": all((registry.get_spec(name).required_scopes if registry.get_spec(name) else ()) for name in registry.names()),
        "move_requires_approval": registry.get_spec("execute_action_chunk").requires_approval_for({}) is True,
        "stop_not_approval_blocked": registry.get_spec("stop_robot").requires_approval_for({}) is False and stop_decision.allowed,
        "policy_gate_asks_for_motion": move_decision.allowed and move_decision.decision == "ask",
        "observation_hash_only": observation.get("success") is True
        and bool(obs_data.get("input_image_hash"))
        and "raw_image_bytes" not in json.dumps(observation, ensure_ascii=False).lower()
        and "image_data" not in json.dumps(observation, ensure_ascii=False).lower(),
        "policy_action_chunk": policy.get("success") is True and len(action_chunk) == 4,
        "execution_success": execution.get("success") is True,
        "episode_labeled": label.get("success") is True,
        "stop_success": stop.get("success") is True,
        "event_contracts": True,
        "timeline_robot_items": robot_summary.get("observations") == 1
        and robot_summary.get("policy_inferences") == 1
        and robot_summary.get("actions") == 1
        and robot_summary.get("labels") == 1,
    }
    return {
        "suite_name": "embodied_vla_v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "tools": registry.public_specs(),
        "scenario": scenario_status(scenario),
        "timeline_robot_rollout": robot_summary,
        "embodied_vla_v1": "PASS" if all(checks.values()) else "FAIL",
    }


def main() -> int:
    summary = asyncio.run(verify())
    path = ROOT / "artifacts/runtime/embodied-vla-v1-summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(summary['checks'], ensure_ascii=False)}")
    print(f"summary_json={path}")
    print(f"embodied_vla_v1={summary['embodied_vla_v1']}")
    return 0 if summary["embodied_vla_v1"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
