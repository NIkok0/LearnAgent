from __future__ import annotations

from typing import Any

from copilot_agent.memory.item_schema import MemoryType
from copilot_agent.memory.episodic_recall import is_run_eligible_for_thread
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.memory.short_term_extractors import (
    final_answer_from_done,
    policy_decision,
    policy_seed,
    rag_artifact,
    rag_seed,
    retrieval_sources as extract_retrieval_sources,
    side_effect_action,
    side_effect_seed,
    tool_action,
    tool_seed,
)
from copilot_agent.memory.short_term_seed import (
    MAX_ITEMS_PER_SECTION,
    MAX_SEEDS,
    MAX_TEXT_CHARS,
    bounded_text,
    dedupe_records,
    dedupe_seeds,
    dedupe_strings,
    done_event_ids,
    event_id,
    seed,
)


def build_short_term_run_summary(
    events: list[dict[str, Any]],
    *,
    fallback_goal: str = "",
    policy: MemoryPolicyConfig,
) -> dict[str, Any]:
    """Build the run-level short-term memory summary used by episodic and long-term memory."""
    goal = _goal_from_events(events) or fallback_goal
    outcome = _outcome_from_events(events)
    tools: dict[str, dict[str, str]] = {}
    token_parts: list[str] = []
    errors: list[str] = []
    source_event_ids: list[int] = []
    completed_actions: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    retrieval_sources: list[dict[str, Any]] = []
    warnings: list[str] = []
    seeds: list[dict[str, Any]] = []
    final_answer = ""

    for event in events:
        current_event_id = event_id(event)
        if current_event_id is not None:
            source_event_ids.append(current_event_id)
        payload = event.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        event_type = str(event.get("type", ""))

        if event_type == "tool_start":
            name = str(payload.get("name", ""))
            if name:
                tools[name] = {
                    "name": name,
                    "category": str(payload.get("category", "")),
                    "risk_level": str(payload.get("risk_level", "")),
                }
        elif event_type == "tool_end":
            name = str(payload.get("name", ""))
            if name:
                tools.setdefault(
                    name,
                    {
                        "name": name,
                        "category": "",
                        "risk_level": "",
                    },
                )
            action = tool_action(payload, event_id=current_event_id)
            if action:
                completed_actions.append(action)
                candidate = tool_seed(action, event_id=current_event_id)
                if candidate:
                    seeds.append(candidate)
        elif event_type == "retrieval_completed":
            retrieval_sources.extend(retrieval_sources_from_payload(payload, event_id=current_event_id))
        elif event_type == "token":
            text = str(payload.get("text", ""))
            if text:
                token_parts.append(text)
        elif event_type == "done":
            final_answer = final_answer_from_done(payload) or final_answer
        elif event_type == "error":
            error = bounded_text(str(payload.get("error", "")), MAX_TEXT_CHARS)
            if error:
                errors.append(error)
        elif event_type == "policy_decision_recorded":
            decision = policy_decision(payload, event_id=current_event_id)
            if decision:
                decisions.append(decision)
                if decision.get("decision") in {"deny", "block", "redact"}:
                    warnings.append(str(decision.get("reason") or decision.get("decision")))
                candidate = policy_seed(decision, outcome=outcome, event_id=current_event_id)
                if candidate:
                    seeds.append(candidate)
        elif event_type == "tool_side_effect_recorded":
            action = side_effect_action(payload, event_id=current_event_id)
            if action:
                completed_actions.append(action)
                candidate = side_effect_seed(action, event_id=current_event_id)
                if candidate:
                    seeds.append(candidate)
                if action.get("status") == "unknown":
                    warnings.append("side_effect_unknown")
        elif event_type.startswith("rag_document_"):
            artifact = rag_artifact(event_type, payload, event_id=current_event_id)
            if artifact:
                artifacts.append(artifact)
                candidate = rag_seed(artifact, event_id=current_event_id)
                if candidate:
                    seeds.append(candidate)
        elif event_type == "approval_resolved" and payload.get("approved") is False:
            warnings.append("approval_rejected")

    fallback_output = bounded_text("".join(token_parts).strip(), policy.key_output_max_chars)
    answer = final_answer or fallback_output
    if answer and outcome == "completed":
        seeds.append(
            seed(
                content=f"Run completed for goal: {goal}. Result: {bounded_text(answer, 180)}",
                memory_type=MemoryType.TASK_SUMMARY,
                importance=0.78,
                confidence=0.86,
                source_kind="final_answer",
                source_event_ids=done_event_ids(events) or source_event_ids[-2:],
                reusable=True,
            )
        )
    elif goal and outcome == "completed":
        seeds.append(
            seed(
                content=f"Run completed for goal: {goal}",
                memory_type=MemoryType.TASK_SUMMARY,
                importance=0.74,
                confidence=0.84,
                source_kind="goal",
                source_event_ids=source_event_ids[:2],
                reusable=True,
            )
        )

    key_output = bounded_text(answer, policy.key_output_max_chars)
    eligible_for_thread = is_run_eligible_for_thread({"outcome": outcome}, policy)
    summary = {
        "summary_type": "run",
        "goal": goal,
        "outcome": outcome,
        "tools_used": list(tools.keys()),
        "tool_details": list(tools.values()),
        "final_answer": key_output,
        "completed_actions": dedupe_records(completed_actions)[:MAX_ITEMS_PER_SECTION],
        "decisions": dedupe_records(decisions)[:MAX_ITEMS_PER_SECTION],
        "artifacts": dedupe_records(artifacts)[:MAX_ITEMS_PER_SECTION],
        "retrieval_sources": dedupe_records(retrieval_sources)[:MAX_ITEMS_PER_SECTION],
        "warnings": dedupe_strings(warnings)[:MAX_ITEMS_PER_SECTION],
        "memory_candidates_seed": dedupe_seeds(seeds, outcome=outcome)[:MAX_SEEDS],
        "key_outputs": [key_output] if key_output else [],
        "errors": errors[:MAX_ITEMS_PER_SECTION],
        "source_event_ids": source_event_ids,
        "eligible_for_thread": eligible_for_thread,
    }
    summary["char_count"] = len(str(summary))
    return summary


def _goal_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "plan_created":
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                goal = str(payload.get("goal", "")).strip()
                if goal:
                    return bounded_text(goal, MAX_TEXT_CHARS)
    return ""


def _outcome_from_events(events: list[dict[str, Any]]) -> str:
    types = [str(event.get("type", "")) for event in events]
    if "cancelled" in types or "cancel_requested" in types:
        return "cancelled"
    if "error" in types:
        return "failed"
    if "done" in types:
        return "completed"
    return "unknown"


def retrieval_sources_from_payload(payload: dict[str, Any], *, event_id: int | None) -> list[dict[str, Any]]:
    return extract_retrieval_sources(payload, event_id=event_id)


