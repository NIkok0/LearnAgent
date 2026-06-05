from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from copilot_agent.tools.capability.base import CapabilityContext
from copilot_agent.tools.embodied import MockEmbodiedRuntime, embodied_tool_result
from copilot_agent.tools.registry import ToolRegistry


class ObserveSceneArgs(BaseModel):
    instruction: str = Field(default="", description="Natural language robot task instruction.")


class RunVlaPolicyArgs(BaseModel):
    instruction: str
    frame_id: str
    policy_checkpoint_id: str | None = None
    max_steps: int = Field(default=8, ge=1, le=32)


class ExecuteActionChunkArgs(BaseModel):
    action_chunk: list[dict[str, Any]]
    idempotency_key: str | None = None


class StopRobotArgs(BaseModel):
    reason: str = "user_requested"


class RecoverHomeArgs(BaseModel):
    pass


class LabelEpisodeResultArgs(BaseModel):
    episode_id: str
    success: bool
    failure_type: str = ""


class EmbodiedCapability:
    name = "embodied"

    def __init__(self, runtime: MockEmbodiedRuntime | None = None) -> None:
        self._runtime = runtime or MockEmbodiedRuntime()

    def register(self, registry: ToolRegistry, ctx: CapabilityContext) -> None:
        del ctx
        registry.register_async(
            coroutine=self._observe_scene,
            name="observe_scene",
            description="Capture a sanitized robot scene observation. Returns frame id, image hash, object labels, and workspace bounds; never returns raw image bytes.",
            args_schema=ObserveSceneArgs,
            category="embodied",
            risk_level="low",
            requires_approval=False,
            required_scopes=("robot:observe",),
            timeout_seconds=5.0,
        )
        registry.register_async(
            coroutine=self._run_vla_policy,
            name="run_vla_policy",
            description="Run a VLA or imitation-learning policy on a scene frame and return a bounded action chunk plus confidence metadata.",
            args_schema=RunVlaPolicyArgs,
            category="embodied",
            risk_level="medium",
            requires_approval=False,
            required_scopes=("robot:infer",),
            timeout_seconds=30.0,
        )
        registry.register_async(
            coroutine=self._execute_action_chunk,
            name="execute_action_chunk",
            description="Execute a robot action chunk after safety checks. High-risk physical motion requires approval and idempotency.",
            args_schema=ExecuteActionChunkArgs,
            category="embodied",
            risk_level="high",
            requires_approval=True,
            required_scopes=("robot:move",),
            timeout_seconds=60.0,
            idempotency_key_field="idempotency_key",
        )
        registry.register_async(
            coroutine=self._stop_robot,
            name="stop_robot",
            description="Immediately stop robot motion. Emergency stop is always available and must not be delayed by approval.",
            args_schema=StopRobotArgs,
            category="embodied",
            risk_level="high",
            requires_approval=False,
            required_scopes=("robot:stop",),
            timeout_seconds=3.0,
        )
        registry.register_async(
            coroutine=self._recover_home,
            name="recover_home",
            description="Move the robot back to a known safe home pose after a failed rollout.",
            args_schema=RecoverHomeArgs,
            category="embodied",
            risk_level="medium",
            requires_approval=True,
            required_scopes=("robot:move",),
            timeout_seconds=20.0,
        )
        registry.register_async(
            coroutine=self._label_episode_result,
            name="label_episode_result",
            description="Record rollout success/failure metadata for VLA evaluation.",
            args_schema=LabelEpisodeResultArgs,
            category="embodied",
            risk_level="low",
            requires_approval=False,
            required_scopes=("robot:label",),
            timeout_seconds=5.0,
        )

    async def _observe_scene(self, instruction: str = "") -> dict[str, Any]:
        start = time.perf_counter()
        raw = await self._runtime.observe_scene(instruction=instruction)
        return embodied_tool_result(raw, duration_ms=_duration_ms(start))

    async def _run_vla_policy(
        self,
        instruction: str,
        frame_id: str,
        policy_checkpoint_id: str | None = None,
        max_steps: int = 8,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        raw = await self._runtime.run_vla_policy(
            instruction=instruction,
            frame_id=frame_id,
            policy_checkpoint_id=policy_checkpoint_id,
            max_steps=max_steps,
        )
        return embodied_tool_result(raw, duration_ms=_duration_ms(start))

    async def _execute_action_chunk(
        self,
        action_chunk: list[dict[str, Any]],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        raw = await self._runtime.execute_action_chunk(
            action_chunk=action_chunk,
            idempotency_key=idempotency_key,
        )
        return embodied_tool_result(raw, duration_ms=_duration_ms(start))

    async def _stop_robot(self, reason: str = "user_requested") -> dict[str, Any]:
        start = time.perf_counter()
        raw = await self._runtime.stop_robot(reason=reason)
        return embodied_tool_result(raw, duration_ms=_duration_ms(start))

    async def _recover_home(self) -> dict[str, Any]:
        start = time.perf_counter()
        raw = await self._runtime.recover_home()
        return embodied_tool_result(raw, duration_ms=_duration_ms(start))

    async def _label_episode_result(
        self,
        episode_id: str,
        success: bool,
        failure_type: str = "",
    ) -> dict[str, Any]:
        start = time.perf_counter()
        raw = await self._runtime.label_episode_result(
            episode_id=episode_id,
            success=success,
            failure_type=failure_type,
        )
        return embodied_tool_result(raw, duration_ms=_duration_ms(start))


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
