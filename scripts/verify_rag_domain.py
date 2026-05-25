#!/usr/bin/env python
"""Aggregate lightweight deterministic RAG verifier cases."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.retrieval import RetrievalRequest  # noqa: E402
from copilot_agent.credentials import CredentialManager  # noqa: E402
from copilot_agent.rag.api_parse import parse_api_section  # noqa: E402
from copilot_agent.rag.api_paths import extract_api_paths, merge_path_strings  # noqa: E402
from copilot_agent.rag.bm25 import BM25Index  # noqa: E402
from copilot_agent.rag.fusion import dedup_chunks, rank_from_scores, rrf_fuse  # noqa: E402
from copilot_agent.rag.query_rewrite import configure_rag_rules, rewrite_query  # noqa: E402
from copilot_agent.rag.query_router import route_query  # noqa: E402
from copilot_agent.rag.request_context import merge_retrieval_scopes, retrieval_defaults_from_scenario  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk, dynamic_search_top_k, select_chunks_for_budget  # noqa: E402
from copilot_agent.rag.security import AUTHORITY_BY_DOC_TYPE  # noqa: E402
from copilot_agent.rag.tokenize import tokenize  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from scripts._rag_verify_helpers import apply_verify_scenario, build_keyword_rag_store, load_verify_chunks, write_verify_summary  # noqa: E402


def _chunk(source: str, heading: str, *, authority: int, start_line: int) -> DocChunk:
    return DocChunk(
        source=source,
        start_line=start_line,
        text=f"content for {heading}",
        section_title=heading,
        heading_path=heading,
        authority=authority,
    )


def case_authority_dedup() -> dict[str, Any]:
    ranked = [
        _chunk("policy.md", "Redis retry", authority=60, start_line=10),
        _chunk("policy.md", "Redis retry", authority=90, start_line=20),
        _chunk("policy.md", "Queue sizing", authority=70, start_line=30),
    ]
    deduped = dedup_chunks(ranked)
    by_heading = {c.heading_path: c for c in deduped}
    return {
        "checks": {
            "dedup_count": len(deduped) == 2,
            "redis_retry_keeps_high_authority": by_heading.get("Redis retry") is not None
            and by_heading["Redis retry"].authority == 90
            and by_heading["Redis retry"].start_line == 20,
            "queue_sizing_preserved": by_heading.get("Queue sizing") is not None
            and by_heading["Queue sizing"].authority == 70,
        }
    }


def case_api_path_extraction() -> dict[str, Any]:
    apply_verify_scenario("watermark")
    job_id = str(uuid.uuid4())
    chunks = [
        DocChunk(
            source="API-CONTRACT.md",
            start_line=49,
            text="## Jobs\n\n### GET /api/v1/jobs/{id}\n\nQuery watermark job status.",
            section_title="GET /api/v1/jobs/{id}",
            heading_path="Jobs > GET /api/v1/jobs/{id}",
            doc_type="api",
        ),
        DocChunk(
            source="API-CONTRACT.md",
            start_line=27,
            text="## Health\n\n### GET /actuator/health\n\nLiveness probe.",
            section_title="GET /actuator/health",
            heading_path="Health",
            doc_type="api",
        ),
    ]
    paths = [hint.path for hint in extract_api_paths(chunks, query=f"job status {job_id}")]
    return {
        "checks": {
            "extracts_job_path": f"/api/v1/jobs/{job_id}" in paths,
            "extracts_health_path": "/actuator/health" in paths,
            "merge_paths": merge_path_strings(("/actuator/health",), (f"/api/v1/jobs/{job_id}", "/actuator/health"))
            == ("/actuator/health", f"/api/v1/jobs/{job_id}"),
        }
    }


def case_api_ingest() -> dict[str, Any]:
    chunks = load_verify_chunks(sources=("API-CONTRACT.md",))
    login = next((chunk for chunk in chunks if chunk.api_endpoint and chunk.api_endpoint.path == "/api/v1/auth/login"), None)
    health = next((chunk for chunk in chunks if chunk.api_endpoint and chunk.api_endpoint.path == "/actuator/health"), None)
    error_chunk = next((chunk for chunk in chunks if any(code.code == "UNAUTHORIZED" for code in chunk.error_codes)), None)
    login_fields = {field.name for field in login.request_fields} if login else set()
    login_response = {field.name for field in login.response_fields} if login else set()
    health_response = {field.name for field in health.response_fields} if health else set()
    parse_login = parse_api_section(
        section_title="POST /api/v1/auth/login",
        text=(login.text if login else ""),
        heading_path="Authentication > POST /api/v1/auth/login",
    )
    return {
        "checks": {
            "chunks_loaded": len(chunks) > 0,
            "login_endpoint": login is not None and login.api_endpoint.method == "POST",
            "login_fields": login_fields == {"username", "password"},
            "login_response_fields": login_response == {"success", "userId"},
            "health_response_fields": health_response == {"status"},
            "health_endpoint": health is not None and health.api_endpoint.method == "GET",
            "error_codes": error_chunk is not None and any(code.code == "UNAUTHORIZED" for code in error_chunk.error_codes),
            "chunk_index_present": all(chunk.chunk_index >= 0 for chunk in chunks),
            "updated_at_present": all(bool(chunk.updated_at) for chunk in chunks),
            "parser_unit_login_fields": {field.name for field in parse_login.request_fields} == {"username", "password"},
        },
        "api_contract_chunks": len(chunks),
        "login_response_fields": sorted(login_response),
        "health_response_fields": sorted(health_response),
    }


def case_doc_security_ingest() -> dict[str, Any]:
    chunks = load_verify_chunks()
    by_source: dict[str, list[Any]] = {}
    for chunk in chunks:
        by_source.setdefault(chunk.source, []).append(chunk)
    api_chunks = by_source.get("API-CONTRACT.md") or []
    security_chunks = by_source.get("SECURITY-BASELINE.md") or []
    deploy_chunks = by_source.get("DEPLOY-SERVER.md") or []
    return {
        "checks": {
            "chunks_loaded": len(chunks) > 0,
            "api_contract_authority": bool(api_chunks) and all(c.authority == 95 for c in api_chunks),
            "api_contract_tenant": bool(api_chunks) and all(c.tenant_id == "default" for c in api_chunks),
            "security_baseline_acl": bool(security_chunks)
            and all(set(c.acl) == {"group:ops", "group:security"} for c in security_chunks),
            "security_baseline_classification": bool(security_chunks)
            and all(c.classification == "confidential" for c in security_chunks),
            "deploy_default_authority": bool(deploy_chunks)
            and all(c.authority == AUTHORITY_BY_DOC_TYPE["deploy"] for c in deploy_chunks),
            "all_chunks_have_tenant": all(c.tenant_id == "default" for c in chunks),
        }
    }


def case_retrieval_scopes() -> dict[str, Any]:
    scenario = load_scenario("watermark")
    apply_verify_scenario("watermark")
    creds = CredentialManager.from_scenario_resources(scenario.resources, ttl_seconds=3600)
    defaults = retrieval_defaults_from_scenario(scenario, credential_manager=creds, user_id="alice")
    merged = merge_retrieval_scopes(credential_manager=creds, scenario=scenario, user_id="alice")
    request = RetrievalRequest(
        tenant_id=str(defaults["tenant_id"]),
        user_id="alice",
        query="HTTPS cookie baseline",
        max_classification="confidential",
        allowed_scopes=list(defaults["allowed_scopes"]),
        purpose="verify_scopes",
    )
    detailed, _policy = RagStore(
        [
            DocChunk(
                source="SECURITY-BASELINE.md",
                start_line=1,
                text="HTTPS whitelist cookie baseline policy",
                tenant_id="default",
                classification="confidential",
                acl=["group:ops", "group:security"],
                authority=85,
            )
        ]
    ).policy_aware_search(request, top_k=4)
    return {
        "checks": {
            "http_scopes_present": "http:read" in merged and "http:write" in merged,
            "rag_group_scopes_present": "group:ops" in merged and "group:security" in merged,
            "user_scope_present": "user:alice" in merged,
            "security_baseline_allowed": any(chunk.source == "SECURITY-BASELINE.md" for chunk in detailed.chunks),
            "embedding_model_from_scenario": settings.rag_embedding_model == "BAAI/bge-small-zh-v1.5",
        },
        "allowed_scopes": list(request.allowed_scopes),
    }


def case_retrieval_quality() -> dict[str, Any]:
    apply_verify_scenario("watermark")
    production_deploy = "\u751f\u4ea7\u90e8\u7f72"
    stuck_task_question = "\u6c34\u5370\u4efb\u52a1\u4e00\u76f4\u5361\u4f4f\u600e\u4e48\u6392\u67e5\uff1f"
    api_field_question = "POST /api/v1/auth/login \u9700\u8981\u54ea\u4e9b\u8bf7\u6c42\u5b57\u6bb5\uff1f"
    deploy_steps_question = "\u751f\u4ea7\u90e8\u7f72 Java API \u7684\u5927\u81f4\u6b65\u9aa4\u662f\u4ec0\u4e48\uff1f"
    queue_json_question = "\u961f\u5217\u91cc\u7684\u6c34\u5370\u4efb\u52a1 JSON \u5b57\u6bb5\u6709\u54ea\u4e9b\uff1f"
    checklist_risk_question = "\u9700\u6c42\u68c0\u67e5\u8868\u91cc\u6709\u54ea\u4e9b\u5df2\u77e5\u504f\u5dee\u6216\u98ce\u9669\u70b9\uff1f"
    rewritten = rewrite_query(stuck_task_question)
    checks: dict[str, bool] = {
        "cjk_tokenize": production_deploy in tokenize(f"{production_deploy} Java API"),
        "ascii_tokenize": "verify-config" in tokenize("verify-config self check"),
        "query_rewrite_expands": "QUEUED" in rewritten or "Redis" in rewritten,
    }
    chunks = [
        DocChunk(source="a.md", start_line=1, text="Redis Stream wm:jobs:stream", heading_path="Queue > Redis", doc_type="tech_selection"),
        DocChunk(source="a.md", start_line=10, text="Redis Stream wm:jobs:stream duplicate", heading_path="Queue > Redis", doc_type="tech_selection"),
        DocChunk(source="b.md", start_line=1, text="verify-config environment", doc_type="deploy"),
    ]
    checks["bm25_scores"] = ("a.md", 1) in BM25Index(chunks).scores("Redis Stream")
    fused = rrf_fuse(
        [
            rank_from_scores({("a.md", 1): 1.0, ("b.md", 1): 0.5}),
            rank_from_scores({("b.md", 1): 1.0, ("a.md", 1): 0.2}),
        ],
        k=60,
    )
    checks["rrf_fuse"] = ("a.md", 1) in fused and ("b.md", 1) in fused
    checks["dedup_same_heading_path"] = len(dedup_chunks(chunks[:2])) == 1
    huge = [DocChunk(source=f"big-{i}.md", start_line=1, text="x" * 5000, doc_type="doc") for i in range(6)]
    packed = select_chunks_for_budget(huge, max_chars=8000)
    checks["budget_packing_limits_chunks"] = 1 <= len(packed) < len(huge)
    checks["dynamic_top_k_respects_budget"] = dynamic_search_top_k(budget_chars=4200, ceiling=8) <= 3
    sparse = route_query(api_field_question, vector_available=True)
    dense = route_query(deploy_steps_question, vector_available=True)
    checks["route_sparse_for_api_path"] = sparse.mode in {"sparse", "hybrid"} and sparse.bm25_weight >= sparse.vector_weight
    checks["route_dense_for_open_chinese"] = dense.mode in {"dense", "hybrid"} and dense.vector_weight > 0
    store = build_keyword_rag_store()
    detailed = store.search_detailed(queue_json_question, top_k=6)
    checks["store_search_chinese_queue_question"] = "watermark-java-backend-tech-selection.md" in {h.source for h in detailed.chunks}
    checks["store_search_returns_route"] = detailed.route.mode in {"sparse", "dense", "hybrid"}
    old_rewrite = settings.rag_query_rewrite_enabled
    try:
        configure_rag_rules(None)
        settings.rag_query_rewrite_enabled = False
        p4_005_sources = {h.source for h in build_keyword_rag_store().search(checklist_risk_question, top_k=6)}
        checks["p4_005_chinese_question_without_rewrite"] = "REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md" in p4_005_sources
    finally:
        apply_verify_scenario("watermark")
        settings.rag_query_rewrite_enabled = old_rewrite
    return {"checks": checks}


CASES: dict[str, Callable[[], dict[str, Any]]] = {
    "authority_dedup": case_authority_dedup,
    "api_path_extraction": case_api_path_extraction,
    "api_ingest": case_api_ingest,
    "doc_security_ingest": case_doc_security_ingest,
    "retrieval_scopes": case_retrieval_scopes,
    "retrieval_quality": case_retrieval_quality,
}

def run_case(name: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        payload = CASES[name]()
        checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
        status = "PASS" if all(bool(value) for value in checks.values()) else "FAIL"
        error = ""
    except Exception as exc:
        payload = {"checks": {}}
        status = "FAIL"
        error = str(exc)
    return {
        "case": name,
        "status": status,
        "checks": payload.get("checks", {}),
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "details": {key: value for key, value in payload.items() if key != "checks"},
        "error": error,
    }


def print_case_result(case_name: str, result: dict[str, Any]) -> None:
    checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"rag_domain_{case_name}={result['status']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify lightweight deterministic RAG domain cases.")
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/phase4/rag-domain-summary.json"))
    args = parser.parse_args()

    selected = list(CASES) if args.case == "all" else [args.case]
    results = [run_case(name) for name in selected]
    passed = all(result["status"] == "PASS" for result in results)
    summary = {
        "suite_name": "rag_domain",
        "case": args.case,
        "status": "PASS" if passed else "FAIL",
        "cases_total": len(results),
        "cases_passed": sum(1 for result in results if result["status"] == "PASS"),
        "results": results,
        "checks": {f"{result['case']}_passed": result["status"] == "PASS" for result in results},
    }
    summary_path = write_verify_summary(args.summary_json, summary)
    if args.case == "all":
        for result in results:
            print(f"{result['case']}={result['status']}")
    else:
        print_case_result(args.case, results[0])
    print(f"summary_json={summary_path}")
    print(f"rag_domain={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
