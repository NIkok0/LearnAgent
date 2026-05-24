#!/usr/bin/env python
"""Phase 4 Step 2: RAG quality evaluation (RAGAS optional, proxy fallback)."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SCENARIO", "watermark")


def _bootstrap_scenario() -> None:
    from copilot_agent.scenario import load_scenario  # noqa: WPS433
    from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: WPS433

    apply_scenario_environment(load_scenario("watermark"))


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list):
        return []
    return [c for c in cases if isinstance(c, dict) and str(c.get("category", "")) == "docs"]


def _gold_chunks(case: dict[str, Any]) -> list[tuple[str, int]]:
    raw = case.get("gold_chunks")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        if not source:
            continue
        try:
            start_line = int(item.get("start_line", 0))
        except (TypeError, ValueError):
            continue
        out.append((source, start_line))
    return out


def _chunk_recall_at_k(
    retrieved_keys: list[tuple[str, int]],
    gold_keys: list[tuple[str, int]],
    *,
    top_k: int,
) -> float | None:
    if not gold_keys:
        return None
    top_keys = retrieved_keys[:top_k]
    hits = sum(1 for key in gold_keys if key in top_keys)
    return hits / len(gold_keys)


def _chunk_mrr(retrieved_keys: list[tuple[str, int]], gold_keys: list[tuple[str, int]]) -> float | None:
    if not gold_keys:
        return None
    gold_set = set(gold_keys)
    for rank, key in enumerate(retrieved_keys, start=1):
        if key in gold_set:
            return 1.0 / rank
    return 0.0


def _build_proxy_records(
    cases: list[dict[str, Any]], top_k: int, *, disable_vector: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if disable_vector:
        os.environ["RAG_USE_VECTOR"] = "false"
    else:
        os.environ["RAG_USE_VECTOR"] = "true"

    from copilot_agent.rag import build_rag_store  # noqa: WPS433
    from copilot_agent.settings import settings  # noqa: WPS433

    settings.rag_use_vector = os.environ.get("RAG_USE_VECTOR", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    settings.rag_rerank_enabled = os.environ.get("RAG_RERANK_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    embedding_model = os.environ.get("RAG_EMBEDDING_MODEL", "").strip()
    if embedding_model:
        settings.rag_embedding_model = embedding_model

    store = build_rag_store()
    records: list[dict[str, Any]] = []
    coverage_values: list[float] = []
    full_match_values: list[float] = []
    hit_values: list[float] = []
    gold_recall_values: list[float] = []
    gold_mrr_values: list[float] = []
    must_not_violation_values: list[float] = []

    for case in cases:
        case_id = str(case.get("id", ""))
        question = str(case.get("question", ""))
        required_sources = [str(x) for x in case.get("required_sources", []) if str(x).strip()]
        must_not_sources = [str(x) for x in case.get("must_not_sources", []) if str(x).strip()]
        gold_keys = _gold_chunks(case)

        parts = store.search(question, top_k=top_k)
        retrieved_keys = [(p.source, p.start_line) for p in parts]
        retrieved_sources = sorted({p.source for p in parts})
        required_set = set(required_sources)
        hit_set = required_set & set(retrieved_sources)
        coverage = (len(hit_set) / len(required_set)) if required_set else 1.0
        full_match = 1.0 if (not required_set or required_set.issubset(set(retrieved_sources))) else 0.0
        has_hits = 1.0 if parts else 0.0

        recall_at_k = _chunk_recall_at_k(retrieved_keys, gold_keys, top_k=top_k)
        mrr = _chunk_mrr(retrieved_keys, gold_keys)
        violations = sorted(set(must_not_sources) & set(retrieved_sources))
        must_not_violation = 1.0 if violations else 0.0

        coverage_values.append(coverage)
        full_match_values.append(full_match)
        hit_values.append(has_hits)
        if recall_at_k is not None:
            gold_recall_values.append(recall_at_k)
        if mrr is not None:
            gold_mrr_values.append(mrr)
        if must_not_sources:
            must_not_violation_values.append(must_not_violation)

        record: dict[str, Any] = {
            "id": case_id,
            "question": question,
            "question_type": str(case.get("question_type", "")),
            "required_sources": required_sources,
            "must_not_sources": must_not_sources,
            "retrieved_sources": retrieved_sources,
            "retrieved_chunk_keys": [{"source": s, "start_line": ln} for s, ln in retrieved_keys],
            "required_source_coverage": round(coverage, 4),
            "required_source_full_match": bool(full_match),
            "retrieved_chunks": len(parts),
            "must_not_violations": violations,
        }
        if recall_at_k is not None:
            record["gold_chunk_recall_at_k"] = round(recall_at_k, 4)
        if mrr is not None:
            record["gold_chunk_mrr"] = round(mrr, 4)
        records.append(record)

    metrics: dict[str, Any] = {
        "docs_cases": len(cases),
        "vector_enabled": bool(getattr(store, "vector_enabled", False)),
        "rerank_enabled": bool(settings.rag_rerank_enabled),
        "embedding_model": settings.rag_embedding_model if settings.rag_use_vector else "n/a",
        "avg_required_source_coverage": round(statistics.mean(coverage_values), 4) if coverage_values else 0.0,
        "required_source_full_match_rate": round(statistics.mean(full_match_values), 4) if full_match_values else 0.0,
        "retrieval_hit_rate": round(statistics.mean(hit_values), 4) if hit_values else 0.0,
    }
    if gold_recall_values:
        metrics["gold_chunk_recall_at_k_avg"] = round(statistics.mean(gold_recall_values), 4)
        metrics["gold_chunk_cases"] = len(gold_recall_values)
    if gold_mrr_values:
        metrics["gold_chunk_mrr_avg"] = round(statistics.mean(gold_mrr_values), 4)
    if must_not_violation_values:
        metrics["must_not_violation_rate"] = round(statistics.mean(must_not_violation_values), 4)
        metrics["must_not_cases"] = len(must_not_violation_values)

    from copilot_agent.eval.context_quality import (  # noqa: WPS433
        authority_spread,
        context_overlap_rate,
        truncation_rate,
    )
    from copilot_agent.rag.context_guard import build_guarded_context  # noqa: WPS433
    from copilot_agent.settings import settings as app_settings  # noqa: WPS433

    overlap_values: list[float] = []
    truncation_flags: list[bool] = []
    authority_values: list[dict[str, Any]] = []
    for case in cases:
        question = str(case.get("question", ""))
        parts = store.search(question, top_k=top_k)
        guarded = build_guarded_context(
            parts,
            max_chars=app_settings.rag_context_budget_chars,
            require_citations=app_settings.private_rag_require_citations,
        )
        overlap_values.append(context_overlap_rate(guarded.chunks))
        truncation_flags.append(bool(guarded.truncated))
        authority_values.append(authority_spread(guarded.chunks))

    if overlap_values:
        metrics["context_overlap_rate_avg"] = round(statistics.mean(overlap_values), 4)
    metrics["context_truncation_rate"] = round(truncation_rate(truncation_flags), 4)
    if authority_values:
        metrics["authority_spread_avg"] = round(
            statistics.mean(float(item.get("avg", 0.0)) for item in authority_values),
            2,
        )

    return records, metrics


def _maybe_run_ragas(records: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Try real RAGAS scoring. Falls back to proxy when unavailable."""
    from copilot_agent.eval.llm_client import ensure_eval_api_env, get_eval_chat_model  # noqa: WPS433

    errors: list[str] = []
    if not ensure_eval_api_env():
        errors.append("OPENAI_API_KEY_not_set")
        return {}, errors
    try:
        from datasets import Dataset  # type: ignore
        from ragas import evaluate  # type: ignore
        from ragas.metrics import answer_relevancy, faithfulness  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        errors.append(f"ragas_import_error: {exc}")
        return {}, errors

    try:
        ds = Dataset.from_dict(
            {
                "question": [r["question"] for r in records],
                "answer": [f"Sources: {', '.join(r['retrieved_sources'])}" for r in records],
                "contexts": [[f"source={s}" for s in r["retrieved_sources"]] for r in records],
            }
        )
        llm = get_eval_chat_model()
        result = evaluate(ds, metrics=[faithfulness, answer_relevancy], llm=llm)
        scores = result.to_pandas().mean(numeric_only=True).to_dict()
        return {
            "faithfulness": float(scores.get("faithfulness", 0.0)),
            "answer_relevancy": float(scores.get("answer_relevancy", 0.0)),
        }, errors
    except Exception as exc:  # pragma: no cover - environment dependent
        errors.append(f"ragas_runtime_error: {exc}")
        return {}, errors


def _docs_precondition() -> tuple[bool, dict[str, Any]]:
    from copilot_agent.rag.docs_manifest import load_docs_manifest
    from copilot_agent.rag.ingest import repo_docs_dir  # noqa: WPS433

    base = repo_docs_dir()
    if base is None:
        return False, {
            "docs_dir": None,
            "required_files": [],
            "missing_files": [],
        }
    required = list(load_docs_manifest(base).filenames(docs_dir=base))
    missing = [name for name in required if not (base / name).is_file()]
    return len(missing) == 0, {
        "docs_dir": str(base),
        "required_files": required,
        "missing_files": missing,
    }


def _proxy_pass(proxy_metrics: dict[str, Any], *, docs_cases: int) -> bool:
    if docs_cases < 3:
        return False
    if proxy_metrics.get("retrieval_hit_rate", 0.0) < 0.9:
        return False
    if proxy_metrics.get("required_source_full_match_rate", 0.0) < 0.6:
        return False
    if "gold_chunk_recall_at_k_avg" in proxy_metrics:
        if proxy_metrics["gold_chunk_recall_at_k_avg"] < 0.8:
            return False
    if "must_not_violation_rate" in proxy_metrics:
        if proxy_metrics["must_not_violation_rate"] > 0.0:
            return False
    return True


def _write_rag_metrics(path: Path, *, proxy_metrics: dict[str, Any], profile: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "profile": profile,
        "embedding_model": proxy_metrics.get("embedding_model", "n/a"),
        "vector_enabled": proxy_metrics.get("vector_enabled", False),
        "rerank_enabled": proxy_metrics.get("rerank_enabled", False),
        "proxy_metrics": proxy_metrics,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    history_dir = path.parent / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    history_path = history_dir / f"{profile}-{stamp}.json"
    history_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 4 RAG quality (RAGAS optional).")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval/phase4-eval-cases.json"),
        help="Path to structured eval dataset.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase4/phase4-ragas-summary.json"),
        help="Path to write evaluation summary JSON.",
    )
    parser.add_argument("--top-k", type=int, default=6, help="Top-k chunks per docs case.")
    parser.add_argument(
        "--disable-vector",
        action="store_true",
        default=False,
        help="Disable vector retrieval for deterministic proxy evaluation.",
    )
    parser.add_argument(
        "--enable-vector",
        action="store_true",
        help="Enable vector retrieval in this run (overrides --disable-vector).",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "proxy", "ragas"],
        default="auto",
        help="auto: try RAGAS then fallback; proxy: deterministic retrieval metrics; ragas: strict RAGAS.",
    )
    parser.add_argument(
        "--allow-missing-docs",
        action="store_true",
        help="Return SKIP instead of FAIL when required docs are missing.",
    )
    parser.add_argument(
        "--write-rag-metrics",
        default="",
        help="Optional path to write rag_metrics trend JSON (nightly profile).",
    )
    parser.add_argument(
        "--metrics-profile",
        default="pr",
        help="Profile label stored in rag_metrics JSON (e.g. pr, nightly).",
    )
    parser.add_argument(
        "--allow-vector-skip",
        action="store_true",
        help="Return SKIP when --enable-vector is set but vector backend is unavailable.",
    )
    args = parser.parse_args()

    _bootstrap_scenario()

    dataset_path = Path(args.dataset).resolve()
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    try:
        cases = _load_dataset(dataset_path)
    except Exception as exc:
        cases = []
        errors.append(f"dataset_read_error: {exc}")

    docs_ready, precondition = _docs_precondition()
    if not docs_ready and args.allow_missing_docs:
        summary = {
            "dataset_path": str(dataset_path),
            "eval_mode": "proxy",
            "proxy_metrics": {
                "docs_cases": 0,
                "vector_enabled": False,
                "avg_required_source_coverage": 0.0,
                "required_source_full_match_rate": 0.0,
                "retrieval_hit_rate": 0.0,
            },
            "ragas_metrics": {},
            "ragas_warnings": [],
            "errors": [],
            "records": [],
            "preconditions": precondition,
            "phase4_ragas": "SKIP",
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"dataset_path={summary['dataset_path']}")
        print(f"eval_mode={summary['eval_mode']}")
        print("docs_cases=0")
        print("retrieval_hit_rate=0.0")
        print("required_source_full_match_rate=0.0")
        print("avg_required_source_coverage=0.0")
        print(f"summary_json={summary_path}")
        print("phase4_ragas=SKIP")
        return 0
    if not docs_ready:
        errors.append(
            "docs_precondition_failed: set COPILOT_DOCS_PATH or run with SCENARIO=watermark and scenario docs available"
        )

    disable_vector = bool(args.disable_vector and not args.enable_vector)
    records, proxy_metrics = _build_proxy_records(cases, top_k=args.top_k, disable_vector=disable_vector)

    if args.enable_vector and not proxy_metrics.get("vector_enabled") and args.allow_vector_skip:
        summary = {
            "dataset_path": str(dataset_path),
            "eval_mode": "proxy",
            "proxy_metrics": proxy_metrics,
            "ragas_metrics": {},
            "ragas_warnings": [],
            "errors": [],
            "records": records,
            "preconditions": precondition,
            "phase4_ragas": "SKIP",
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"dataset_path={summary['dataset_path']}")
        print("phase4_ragas=SKIP")
        print("skip_reason=vector_backend_unavailable")
        print(f"summary_json={summary_path}")
        return 0

    ragas_metrics: dict[str, Any] = {}
    ragas_errors: list[str] = []
    eval_mode = "proxy"

    if args.mode in ("auto", "ragas"):
        ragas_metrics, ragas_errors = _maybe_run_ragas(records)
        if ragas_metrics:
            eval_mode = "ragas"
        elif args.mode == "ragas":
            errors.extend(ragas_errors)

    proxy_pass = _proxy_pass(proxy_metrics, docs_cases=proxy_metrics.get("docs_cases", 0))

    if args.write_rag_metrics.strip():
        _write_rag_metrics(
            Path(args.write_rag_metrics).resolve(),
            proxy_metrics=proxy_metrics,
            profile=args.metrics_profile,
        )

    summary = {
        "dataset_path": str(dataset_path),
        "eval_mode": eval_mode,
        "proxy_metrics": proxy_metrics,
        "ragas_metrics": ragas_metrics,
        "ragas_warnings": ragas_errors if args.mode == "auto" else [],
        "errors": errors,
        "records": records,
        "preconditions": precondition,
        "phase4_ragas": "PASS" if (proxy_pass and not errors) else "FAIL",
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"dataset_path={summary['dataset_path']}")
    print(f"eval_mode={summary['eval_mode']}")
    print(f"docs_cases={proxy_metrics['docs_cases']}")
    print(f"retrieval_hit_rate={proxy_metrics['retrieval_hit_rate']}")
    print(f"required_source_full_match_rate={proxy_metrics['required_source_full_match_rate']}")
    print(f"avg_required_source_coverage={proxy_metrics['avg_required_source_coverage']}")
    if "gold_chunk_recall_at_k_avg" in proxy_metrics:
        print(f"gold_chunk_recall_at_k_avg={proxy_metrics['gold_chunk_recall_at_k_avg']}")
    if "gold_chunk_mrr_avg" in proxy_metrics:
        print(f"gold_chunk_mrr_avg={proxy_metrics['gold_chunk_mrr_avg']}")
    if "must_not_violation_rate" in proxy_metrics:
        print(f"must_not_violation_rate={proxy_metrics['must_not_violation_rate']}")
    print(f"summary_json={summary_path}")
    if args.write_rag_metrics.strip():
        print(f"rag_metrics_json={Path(args.write_rag_metrics).resolve()}")
    if errors:
        for item in errors:
            print(f"error={item}")
    if ragas_errors and args.mode == "auto":
        for item in ragas_errors:
            print(f"warning={item}")
    if summary["phase4_ragas"] == "PASS":
        print("phase4_ragas=PASS")
        return 0
    print("phase4_ragas=FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
