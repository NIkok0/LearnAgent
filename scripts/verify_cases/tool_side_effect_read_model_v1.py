#!/usr/bin/env python
"""Verify run-level side-effect read model and API."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent import server  # noqa: E402
from copilot_agent.runtime.side_effects import build_side_effect_read_model  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.audit import audit_payload_has_secret  # noqa: E402
from scripts.export_run_debug_bundle import build_debug_bundle  # noqa: E402
from scripts.verify_cases.tool_side_effect_ledger_v1 import verify as seed_side_effect_ledger  # noqa: E402


async def verify(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    seeded = seed_side_effect_ledger(event_store_path, thread_id)
    run_id = str(seeded["run_id"])

    server.event_store = server.EventStore(str(event_store_path))
    run = server.event_store.get_run(run_id) or {}
    events = server.event_store.list_run_events(run_id)
    read_model = build_side_effect_read_model(run, events)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        api_res = await client.get(f"/v1/runs/{run_id}/side-effects")
        missing_res = await client.get(f"/v1/runs/{uuid.uuid4()}/side-effects")

    api_payload = api_res.json() if api_res.headers.get("content-type", "").startswith("application/json") else {}
    bundle = build_debug_bundle(
        event_store_path=event_store_path,
        checkpoint_path=checkpoint_path,
        run_id=run_id,
    )
    encoded_api = json.dumps(api_payload, ensure_ascii=False)
    encoded_bundle_side_effects = json.dumps(
        {
            "side_effects": bundle.get("side_effects"),
            "side_effect_summary": bundle.get("side_effect_summary"),
            "side_effect_warnings": bundle.get("side_effect_warnings"),
        },
        ensure_ascii=False,
    )

    return {
        "event_store_path": str(event_store_path),
        "checkpoint_path": str(checkpoint_path),
        "thread_id": thread_id,
        "run_id": run_id,
        "helper": read_model,
        "api": {
            "status_code": api_res.status_code,
            "payload": api_payload,
        },
        "missing": {
            "status_code": missing_res.status_code,
            "detail": _detail(missing_res),
        },
        "debug_bundle": {
            "side_effect_summary": bundle.get("side_effect_summary"),
            "side_effect_count": len(bundle.get("side_effects") or []),
            "warning_codes": [
                str(warning.get("code"))
                for warning in (bundle.get("side_effect_warnings") or [])
                if isinstance(warning, dict)
            ],
        },
        "audit_safety": {
            "api_has_secret": audit_payload_has_secret(api_payload),
            "api_mentions_json_body": "json_body" in encoded_api,
            "api_mentions_cookie": "WMSESSIONID=" in encoded_api or "cookie_header" in encoded_api,
            "api_mentions_token_query": "secret-token" in encoded_api or "?token=" in encoded_api,
            "api_mentions_raw_body": "raw response" in encoded_api.lower(),
            "debug_side_effects_have_secret": audit_payload_has_secret(
                {
                    "side_effects": bundle.get("side_effects"),
                    "side_effect_summary": bundle.get("side_effect_summary"),
                    "side_effect_warnings": bundle.get("side_effect_warnings"),
                }
            ),
            "debug_mentions_json_body": "json_body" in encoded_bundle_side_effects,
            "debug_mentions_cookie": "WMSESSIONID=" in encoded_bundle_side_effects
            or "cookie_header" in encoded_bundle_side_effects,
            "debug_mentions_token_query": "secret-token" in encoded_bundle_side_effects
            or "?token=" in encoded_bundle_side_effects,
        },
    }


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text
    return str(data.get("detail", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent tool side-effect read model v1.")
    parser.add_argument(
        "--event-store-path",
        default=str(ROOT / "storage/verify-tool-side-effect-read-model-events.sqlite"),
    )
    parser.add_argument(
        "--checkpoint-path",
        default=str(ROOT / "storage/verify-tool-side-effect-read-model-checkpoints.sqlite"),
    )
    parser.add_argument("--thread-id", default=f"side-effect-read-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/tool-side-effect-read-model-v1-summary.json"),
    )
    args = parser.parse_args(argv)
    import asyncio

    event_store_path = Path(args.event_store_path).resolve()
    checkpoint_path = Path(args.checkpoint_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, checkpoint_path, args.thread_id))

    helper_summary = summary["helper"]["summary"]
    api_payload = summary["api"]["payload"]
    api_summary = api_payload.get("summary") if isinstance(api_payload.get("summary"), dict) else {}
    api_side_effects = api_payload.get("side_effects") if isinstance(api_payload.get("side_effects"), list) else []
    api_warnings = api_payload.get("warnings") if isinstance(api_payload.get("warnings"), list) else []
    api_statuses = {str(item.get("call_id")): item.get("side_effect_status") for item in api_side_effects}
    audit = summary["audit_safety"]
    debug_bundle = summary["debug_bundle"]
    checks = {
        "helper_summary_counts": helper_summary == {
            "total": 5,
            "confirmed": 1,
            "reused": 1,
            "none": 1,
            "unknown": 1,
            "blocked": 1,
            "has_unknown": True,
        },
        "api_status_code": summary["api"]["status_code"] == 200,
        "api_summary_matches_helper": api_summary == helper_summary,
        "api_side_effect_statuses": api_statuses == {
            "post-confirmed": "confirmed",
            "post-reused": "reused",
            "post-none": "none",
            "post-unknown": "unknown",
            "post-blocked": "blocked",
        },
        "api_unknown_warning": any(
            isinstance(item, dict) and item.get("code") == "side_effect_unknown"
            for item in api_warnings
        ),
        "api_sanitized": not audit["api_has_secret"]
        and not audit["api_mentions_json_body"]
        and not audit["api_mentions_cookie"]
        and not audit["api_mentions_token_query"]
        and not audit["api_mentions_raw_body"],
        "missing_run_404": summary["missing"]["status_code"] == 404
        and summary["missing"]["detail"] == "run not found",
        "debug_bundle_side_effects": debug_bundle["side_effect_count"] == 5
        and debug_bundle["side_effect_summary"] == helper_summary
        and "side_effect_unknown" in debug_bundle["warning_codes"],
        "debug_bundle_sanitized": not audit["debug_side_effects_have_secret"]
        and not audit["debug_mentions_json_body"]
        and not audit["debug_mentions_cookie"]
        and not audit["debug_mentions_token_query"],
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["tool_side_effect_read_model_v1"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"tool_side_effect_read_model_v1={summary['tool_side_effect_read_model_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
