from __future__ import annotations

import re
from dataclasses import dataclass

from copilot_agent.scenario.router.schema import (
    MatchExpr,
    RouterRule,
    RouterRulesConfig,
    SuggestedPathsConfig,
)
from copilot_agent.scenario.router.types import ToolRoute, UUID_RE


@dataclass(frozen=True)
class _EvalContext:
    text: str
    uuid: str | None
    confirm_dangerous: bool
    allow_job_post: bool
    dangerous_job_path: str


class RouterEngine:
    """Kernel router engine: evaluates declarative Scenario rules (first match wins)."""

    def __init__(self, rules: RouterRulesConfig) -> None:
        self._rules = rules

    def route(
        self,
        query: str,
        *,
        confirm_dangerous: bool = False,
        allow_job_post: bool = False,
    ) -> ToolRoute:
        text = query.strip()
        ctx = _EvalContext(
            text=text,
            uuid=UUID_RE.search(text).group(0) if UUID_RE.search(text) else None,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow_job_post,
            dangerous_job_path=self._rules.dangerous_job_path,
        )

        if not text:
            empty = self._rules.empty_query or self._rules.defaults
            return ToolRoute(
                kind=empty.kind,
                recommended_tools=tuple(empty.recommended_tools),
                forbidden_tools=tuple(empty.forbidden_tools),
                suggested_paths=tuple(empty.suggested_paths),
                rationale=empty.rationale or "Empty query; default to documentation search if needed.",
            )

        for rule in self._rules.rules:
            if rule.when is not None and not _eval_expr(rule.when, ctx, self._rules.predicates):
                continue
            return self._materialize(rule, ctx)

        fallback = self._rules.defaults
        return ToolRoute(
            kind=fallback.kind,
            recommended_tools=tuple(fallback.recommended_tools),
            forbidden_tools=tuple(fallback.forbidden_tools),
            suggested_paths=tuple(fallback.suggested_paths),
            rationale=fallback.rationale or "Static platform documentation question; use search_docs only.",
        )

    def _materialize(self, rule: RouterRule, ctx: _EvalContext) -> ToolRoute:
        recommended = _resolve_recommended(rule, ctx)
        paths = _resolve_paths(rule, ctx)
        return ToolRoute(
            kind=rule.kind,
            recommended_tools=tuple(recommended),
            forbidden_tools=tuple(rule.forbidden_tools),
            suggested_paths=tuple(paths),
            rationale=rule.rationale,
        )


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def _eval_expr(expr: MatchExpr, ctx: _EvalContext, predicates: dict[str, MatchExpr]) -> bool:
    if expr.confirm_dangerous is not None:
        return ctx.confirm_dangerous is expr.confirm_dangerous
    if expr.allow_job_post is not None:
        return ctx.allow_job_post is expr.allow_job_post
    if expr.has_uuid is not None:
        return (ctx.uuid is not None) is expr.has_uuid
    if expr.predicate:
        nested = predicates.get(expr.predicate)
        if nested is None:
            return False
        return _eval_expr(nested, ctx, predicates)
    if expr.match:
        return bool(re.search(expr.match, ctx.text, flags=re.IGNORECASE))
    if expr.not_ is not None:
        return not _eval_expr(expr.not_, ctx, predicates)
    if expr.all:
        return all(_eval_expr(item, ctx, predicates) for item in expr.all)
    if expr.any:
        return any(_eval_expr(item, ctx, predicates) for item in expr.any)
    return True


def _resolve_recommended(rule: RouterRule, ctx: _EvalContext) -> list[str]:
    if rule.recommended is not None:
        for override in rule.recommended.overrides:
            if _contains_any(ctx.text, tuple(override.if_any)):
                return list(override.tools)
        return list(rule.recommended.default)
    return list(rule.recommended_tools)


def _expand_path(template: str, ctx: _EvalContext) -> str:
    out = template.replace("{dangerous_job_path}", ctx.dangerous_job_path)
    if ctx.uuid:
        out = out.replace("{uuid}", ctx.uuid)
    return out


def _resolve_paths(rule: RouterRule, ctx: _EvalContext) -> list[str]:
    if rule.paths is not None:
        return _resolve_paths_config(rule.paths, ctx)
    return [_expand_path(item, ctx) for item in rule.suggested_paths]


def _resolve_paths_config(config: SuggestedPathsConfig, ctx: _EvalContext) -> list[str]:
    paths: list[str] = []
    for dynamic in config.prepend:
        if _contains_any(ctx.text, tuple(dynamic.if_any)):
            for item in dynamic.paths:
                paths.append(_expand_path(item, ctx))
    if ctx.uuid:
        for template in config.prepend_if_uuid:
            paths.append(_expand_path(template, ctx))
    for item in config.static:
        paths.append(_expand_path(item, ctx))
    for dynamic in config.dynamic:
        if _contains_any(ctx.text, tuple(dynamic.if_any)):
            for item in dynamic.paths:
                paths.append(_expand_path(item, ctx))
    return list(dict.fromkeys(paths))
