from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from copilot_agent.contracts.tool_result import ToolResultModel
from copilot_agent.tools.sanitize import sanitize_tool_payload


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass
class MockEmbodiedRuntime:
    """Deterministic robot/VLA adapter used until real LeRobot hardware is bound."""

    policy_checkpoint_id: str = "mock-smolvla-so101-v0"
    workspace_bounds: dict[str, list[float]] = field(
        default_factory=lambda: {"x": [-0.25, 0.25], "y": [-0.35, 0.35], "z": [0.0, 0.35]}
    )
    frame_counter: int = 0
    last_action_chunk: list[dict[str, Any]] = field(default_factory=list)

    async def observe_scene(self, instruction: str = "") -> dict[str, Any]:
        self.frame_counter += 1
        frame_id = f"mock-frame-{self.frame_counter:04d}"
        frame_hash = _stable_hash(f"{frame_id}:{instruction}")
        return {
            "success": True,
            "frame_id": frame_id,
            "input_image_hash": frame_hash,
            "objects": [
                {"label": "red_block", "confidence": 0.92, "position": {"x": 0.08, "y": 0.12, "z": 0.02}},
                {"label": "box", "confidence": 0.95, "position": {"x": -0.12, "y": -0.1, "z": 0.02}},
            ],
            "workspace_bounds": self.workspace_bounds,
            "metadata": {"adapter": "mock", "sensor": "rgbd", "raw_image_recorded": False},
        }

    async def run_vla_policy(
        self,
        *,
        instruction: str,
        frame_id: str,
        policy_checkpoint_id: str | None = None,
        max_steps: int = 8,
    ) -> dict[str, Any]:
        checkpoint = (policy_checkpoint_id or self.policy_checkpoint_id).strip()
        action_count = max(1, min(int(max_steps), 8))
        action_chunk = [
            {
                "step": index,
                "target_pose": {"x": 0.08 - index * 0.01, "y": 0.12 - index * 0.02, "z": 0.05},
                "gripper": "close" if index >= action_count // 2 else "open",
            }
            for index in range(action_count)
        ]
        self.last_action_chunk = action_chunk
        return {
            "success": True,
            "policy_checkpoint_id": checkpoint,
            "frame_id": frame_id,
            "instruction_hash": _stable_hash(instruction),
            "action_chunk": action_chunk,
            "confidence": 0.84,
            "fallback_reason": "",
            "metadata": {
                "adapter": "mock",
                "model_family": "smolvla",
                "action_count": len(action_chunk),
                "raw_instruction_recorded": False,
            },
        }

    async def execute_action_chunk(
        self,
        *,
        action_chunk: list[dict[str, Any]],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        violations = _workspace_violations(action_chunk, self.workspace_bounds)
        if violations:
            return {
                "success": False,
                "execution_status": "blocked",
                "reason": "workspace_bounds_violation",
                "violations": violations,
                "idempotency_key": idempotency_key,
                "metadata": {"adapter": "mock", "side_effect": "blocked"},
            }
        return {
            "success": True,
            "execution_status": "completed",
            "steps_executed": len(action_chunk),
            "idempotency_key": idempotency_key,
            "metadata": {"adapter": "mock", "side_effect": "robot_motion", "executed_at_ms": int(time.time() * 1000)},
        }

    async def stop_robot(self, reason: str = "user_requested") -> dict[str, Any]:
        return {
            "success": True,
            "execution_status": "stopped",
            "reason": reason,
            "metadata": {"adapter": "mock", "emergency_stop": True},
        }

    async def recover_home(self) -> dict[str, Any]:
        return {
            "success": True,
            "execution_status": "home",
            "metadata": {"adapter": "mock", "recoverable": True},
        }

    async def label_episode_result(
        self,
        *,
        episode_id: str,
        success: bool,
        failure_type: str = "",
    ) -> dict[str, Any]:
        return {
            "success": True,
            "episode_id": episode_id,
            "label": "success" if success else "failure",
            "failure_type": failure_type,
            "metadata": {"adapter": "mock", "label_recorded": True},
        }


def embodied_tool_result(raw: dict[str, Any], *, duration_ms: int | None = None) -> dict[str, Any]:
    model = ToolResultModel.from_any(raw, duration_ms=duration_ms)
    safe_metadata = {
        **model.metadata,
        "embodied_result": True,
    }
    if isinstance(model.data, dict):
        safe_metadata.setdefault("policy_checkpoint_id", model.data.get("policy_checkpoint_id"))
        safe_metadata.setdefault("frame_id", model.data.get("frame_id"))
        safe_metadata.setdefault("input_image_hash", model.data.get("input_image_hash"))
    return model.model_copy(
        update={
            "metadata": safe_metadata,
            "sanitized_result": sanitize_tool_payload(raw),
        }
    ).to_llm_dict()


def _workspace_violations(action_chunk: list[dict[str, Any]], bounds: dict[str, list[float]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for item in action_chunk:
        pose = item.get("target_pose") if isinstance(item, dict) else None
        if not isinstance(pose, dict):
            continue
        for axis, pair in bounds.items():
            if axis not in pose or len(pair) != 2:
                continue
            value = float(pose[axis])
            if value < float(pair[0]) or value > float(pair[1]):
                violations.append({"step": item.get("step"), "axis": axis, "value": value, "bounds": pair})
    return violations
