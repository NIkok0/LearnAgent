from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copilot_agent.settings import settings
from copilot_agent.tools.registry import ToolRegistry

DANGEROUS_JOB_PATH = "/api/v1/jobs/watermark"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    message: str = ""
    reason: str = ""


class PolicyRegistry:
    """Thin policy registry for tool-call guardrails."""

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._tools = tool_registry

    def evaluate_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        allow_job_post: bool | None = None,
        confirm_dangerous: bool = False,
    ) -> PolicyDecision:
        allow_job_post = settings.copilot_allow_job_post if allow_job_post is None else allow_job_post
        for call in tool_calls:
            name = str(call.get("name", ""))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            path = str(args.get("path", ""))
            spec = self._tools.get_spec(name) if self._tools is not None else None
            if name == "http_post" and path.split("?", 1)[0] == DANGEROUS_JOB_PATH:
                if not allow_job_post:
                    return PolicyDecision(
                        allowed=False,
                        message=(
                            "POST /api/v1/jobs/watermark is disabled by deployment. "
                            "Enable COPILOT_ALLOW_JOB_POST=true, then retry with explicit confirmation."
                        ),
                        reason="job_post_disabled",
                    )
                if not confirm_dangerous:
                    return PolicyDecision(
                        allowed=True,
                        requires_approval=spec.requires_approval_for(args) if spec is not None else True,
                        message=(
                            "This action is gated. Approve this run if you want to enqueue a watermark job."
                        ),
                        reason="dangerous_tool_requires_approval",
                    )
        return PolicyDecision(allowed=True)
