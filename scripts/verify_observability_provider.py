#!/usr/bin/env python
"""Verify observability provider selection and no-op fallback."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.observability import (  # noqa: E402
    end_chat_trace,
    flush_observability,
    get_observability_provider,
    reset_observability_provider,
    resolve_observability_trace_id,
    start_chat_trace,
)
from copilot_agent.settings import settings  # noqa: E402


def _provider_case(name: str) -> dict[str, object]:
    old_provider = settings.observability_provider
    old_tracing = os.environ.pop("LANGSMITH_TRACING", None)
    old_key = os.environ.pop("LANGSMITH_API_KEY", None)
    try:
        settings.observability_provider = name
        reset_observability_provider()
        provider = get_observability_provider()
        trace = start_chat_trace(
            conversation_id=f"provider-{name}",
            run_id=f"run-{name}",
            messages=[{"role": "user", "content": "hello"}],
            confirm_dangerous=False,
            model="test-model",
        )
        trace_id = resolve_observability_trace_id(trace, thread_id=f"provider-{name}", run_id=f"run-{name}")
        end_chat_trace(trace, output_preview="ok")
        flush_observability()
        return {
            "requested": name,
            "provider_name": getattr(provider, "name", ""),
            "trace_provider": getattr(trace, "provider", ""),
            "trace_id": trace_id,
            "has_external_trace_url": bool(getattr(trace, "external_trace_url", None)),
        }
    finally:
        settings.observability_provider = old_provider
        if old_tracing is not None:
            os.environ["LANGSMITH_TRACING"] = old_tracing
        if old_key is not None:
            os.environ["LANGSMITH_API_KEY"] = old_key
        reset_observability_provider()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify observability provider facade.")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/observability-provider-summary.json"),
    )
    args = parser.parse_args()

    cases = [_provider_case("none"), _provider_case("langfuse"), _provider_case("langsmith"), _provider_case("bad")]
    checks = {
        "none_selected": cases[0]["provider_name"] == "none",
        "langfuse_selected": cases[1]["provider_name"] == "langfuse",
        "langsmith_selected": cases[2]["provider_name"] == "langsmith",
        "bad_falls_back": cases[3]["provider_name"] == "none",
        "trace_ids_present": all(str(case.get("trace_id") or "").startswith(("local-", "provider-")) for case in cases),
    }
    passed = all(checks.values())
    summary = {
        "cases": cases,
        "checks": checks,
        "status": "PASS" if passed else "FAIL",
    }
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"observability_provider={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
