from __future__ import annotations

from typing import Any

from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.memory.schema import EpisodicInjectBundle

EPISODIC_MEMORY_PREFIX = "[EpisodicMemory]"
MEMORY_CONTEXT_PREFIX = "[MemoryContext]"


def build_episodic_inject_bundle(
    *,
    thread_summary: dict[str, Any] | None,
    recalled_runs: list[dict[str, Any]],
    dropped_conflicts: list[dict[str, Any]],
    dropped_long_term: list[dict[str, Any]] | None = None,
    recalled_long_term: list[dict[str, Any]] | None = None,
    long_term_body: str = "",
    config: MemoryPolicyConfig,
) -> EpisodicInjectBundle:
    if not config.enabled:
        return EpisodicInjectBundle(
            thread_summary=None,
            recalled_runs=[],
            dropped_conflicts=dropped_conflicts,
            dropped_long_term=list(dropped_long_term or []),
            recalled_long_term=[],
            inject_preview="",
            budget_applied={"max_chars": config.thread_summary_max_chars, "used_chars": 0, "truncated": False},
            sources={"run_ids": [], "event_ids": [], "memory_item_ids": []},
        )

    body, budget_drops, section_usage = _render_memory_context(
        thread_summary=thread_summary,
        recalled_runs=recalled_runs,
        recalled_long_term=list(recalled_long_term or []),
        config=config,
    )
    all_dropped_long_term = [*list(dropped_long_term or []), *budget_drops]

    footer = (
        "Rules:\n"
        "- Use memory only when relevant.\n"
        "- Prefer current user message and retrieved docs on conflict.\n"
        f"Sources: source_run_ids={_source_run_ids(thread_summary, recalled_runs)}; "
        f"memory_item_ids={_source_memory_item_ids(recalled_long_term or [])}"
    )
    inject_preview = f"{body}\n\n{footer}".strip() if body else footer

    max_chars = config.thread_summary_max_chars
    truncated = len(inject_preview) > max_chars
    if truncated:
        inject_preview = _truncate_text(inject_preview, max_chars)

    return EpisodicInjectBundle(
        thread_summary=thread_summary,
        recalled_runs=recalled_runs,
        dropped_conflicts=dropped_conflicts,
        dropped_long_term=all_dropped_long_term,
        recalled_long_term=list(recalled_long_term or []),
        inject_preview=inject_preview,
        budget_applied={
            "max_chars": max_chars,
            "used_chars": len(inject_preview),
            "truncated": truncated,
            "sections": section_usage,
            "dropped_long_term_count": len(all_dropped_long_term),
        },
        sources={
            "run_ids": _source_run_ids(thread_summary, recalled_runs),
            "event_ids": _source_event_ids(thread_summary, recalled_runs),
            "memory_item_ids": _source_memory_item_ids(recalled_long_term or []),
        },
    )


def render_episodic_system_message(bundle: EpisodicInjectBundle) -> str | None:
    preview = (bundle.inject_preview or "").strip()
    return preview or None


def _render_memory_context(
    *,
    thread_summary: dict[str, Any] | None,
    recalled_runs: list[dict[str, Any]],
    recalled_long_term: list[dict[str, Any]],
    config: MemoryPolicyConfig,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    dropped: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    sections: list[str] = [MEMORY_CONTEXT_PREFIX]

    summary_text, summary_usage, summary_drops = _budget_section(
        "Thread summary",
        _thread_summary_lines(thread_summary),
        max_chars=config.thread_summary_budget_chars,
    )
    if summary_text:
        sections.append(summary_text)
    usage["thread_summary"] = summary_usage
    dropped.extend(summary_drops)

    facts = [item for item in recalled_long_term if item.get("memory_type") == "fact"]
    task_summaries = [item for item in recalled_long_term if item.get("memory_type") == "task_summary"]
    preferences = [
        item
        for item in recalled_long_term
        if item.get("memory_type") in {"preference", "behavior"}
    ]

    fact_text, fact_usage, fact_drops = _budget_section(
        "Relevant facts",
        [_long_term_line(item) for item in facts[: config.long_term_max_per_type]],
        max_chars=config.long_term_budget_chars // 2,
        dropped_items=facts[config.long_term_max_per_type :],
    )
    if fact_text:
        sections.append(fact_text)
    usage["relevant_facts"] = fact_usage
    dropped.extend(fact_drops)

    lessons = [_episodic_lesson_line(item) for item in recalled_runs]
    lessons.extend(_long_term_line(item) for item in task_summaries[: config.long_term_max_per_type])
    lesson_dropped = task_summaries[config.long_term_max_per_type :]
    lesson_text, lesson_usage, lesson_drops = _budget_section(
        "Past run lessons",
        lessons,
        max_chars=config.episodic_budget_chars,
        dropped_items=lesson_dropped,
    )
    if lesson_text:
        sections.append(lesson_text)
    usage["past_run_lessons"] = lesson_usage
    dropped.extend(lesson_drops)

    pref_text, pref_usage, pref_drops = _budget_section(
        "User preferences",
        [_long_term_line(item) for item in preferences[: config.long_term_max_per_type]],
        max_chars=max(120, config.long_term_budget_chars // 3),
        dropped_items=preferences[config.long_term_max_per_type :],
    )
    if pref_text:
        sections.append(pref_text)
    usage["user_preferences"] = pref_usage
    dropped.extend(pref_drops)

    return "\n\n".join(sections).strip(), dropped, usage


def _thread_summary_lines(thread_summary: dict[str, Any] | None) -> list[str]:
    lines: list[str] = []
    if isinstance(thread_summary, dict) and thread_summary:
        goals = thread_summary.get("recent_goals") or []
        outcomes = thread_summary.get("recent_outcomes") or []
        tools = thread_summary.get("tools_used") or []
        lines.append(f"- goals: {', '.join(str(x) for x in goals) or 'none'}")
        lines.append(f"- outcomes: {', '.join(str(x) for x in outcomes) or 'none'}")
        lines.append(f"- tools: {', '.join(str(x) for x in tools) or 'none'}")
    return lines


def _episodic_lesson_line(item: dict[str, Any]) -> str:
    return (
        f"- run_id={item.get('run_id')} goal={item.get('goal')} "
        f"outcome={item.get('outcome')} snippet={item.get('snippet')}"
    )


def _long_term_line(item: dict[str, Any]) -> str:
    return (
        f"- [{item.get('memory_type')}|{item.get('scope')}] "
        f"{_truncate_text(str(item.get('content', '')), 180)} "
        f"(score={float(item.get('score') or 0):.2f})"
    )


def _budget_section(
    title: str,
    lines: list[str],
    *,
    max_chars: int,
    dropped_items: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    dropped: list[dict[str, Any]] = [
        {**item, "reason": "budget_exceeded"} for item in list(dropped_items or [])
    ]
    kept: list[str] = []
    used = len(title) + 1
    for line in lines:
        candidate_chars = len(line) + 1
        if max_chars > 0 and used + candidate_chars > max_chars:
            dropped.append({"content": line, "reason": "budget_exceeded", "section": title})
            continue
        kept.append(line)
        used += candidate_chars
    if not kept:
        return "", {"used_chars": 0, "max_chars": max_chars, "items": 0, "dropped": len(dropped)}, dropped
    text = f"{title}:\n" + "\n".join(kept)
    return text, {"used_chars": len(text), "max_chars": max_chars, "items": len(kept), "dropped": len(dropped)}, dropped


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _source_run_ids(thread_summary: dict[str, Any] | None, recalled_runs: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    if isinstance(thread_summary, dict):
        ids.extend(str(x) for x in thread_summary.get("source_run_ids", []) if str(x))
    ids.extend(str(x.get("run_id", "")) for x in recalled_runs if str(x.get("run_id", "")))
    return list(dict.fromkeys(ids))


def _source_memory_item_ids(recalled_long_term: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in recalled_long_term:
        raw = item.get("id")
        if raw:
            ids.append(str(raw))
    return list(dict.fromkeys(ids))


def _source_event_ids(thread_summary: dict[str, Any] | None, recalled_runs: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    if isinstance(thread_summary, dict):
        for raw in thread_summary.get("source_event_ids", []):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    for item in recalled_runs:
        raw = item.get("source_event_id")
        if raw is not None:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    return ids


__all__ = [
    "EPISODIC_MEMORY_PREFIX",
    "MEMORY_CONTEXT_PREFIX",
    "build_episodic_inject_bundle",
    "render_episodic_system_message",
]
