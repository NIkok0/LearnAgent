#!/usr/bin/env python
"""Phase 4 Step 2: RAG quality evaluation (RAGAS optional, proxy fallback)."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list):
        return []
    return [c for c in cases if isinstance(c, dict) and str(c.get("category", "")) == "docs"]


def _build_proxy_records(
    cases: list[dict[str, Any]], top_k: int, *, disable_vector: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if disable_vector:
        # Keep CI deterministic and avoid optional vector backend noise.
        os.environ["RAG_USE_VECTOR"] = "false"
    from copilot_agent.rag import build_rag_store  # noqa: WPS433

    store = build_rag_store()
    records: list[dict[str, Any]] = []
    coverage_values: list[float] = []
    full_match_values: list[float] = []
    hit_values: list[float] = []

    for case in cases:
        case_id = str(case.get("id", ""))
        question = str(case.get("question", ""))
        required_sources = [str(x) for x in case.get("required_sources", []) if str(x).strip()]
        parts = store.search(question, top_k=top_k)
        retrieved_sources = sorted({p.source for p in parts})
        required_set = set(required_sources)
        hit_set = required_set & set(retrieved_sources)
        coverage = (len(hit_set) / len(required_set)) if required_set else 1.0
        full_match = 1.0 if (not required_set or required_set.issubset(set(retrieved_sources))) else 0.0
        has_hits = 1.0 if parts else 0.0

        coverage_values.append(coverage)
        full_match_values.append(full_match)
        hit_values.append(has_hits)

        records.append(
            {
                "id": case_id,
                "question": question,
                "required_sources": required_sources,
                "retrieved_sources": retrieved_sources,
                "required_source_coverage": round(coverage, 4),
                "required_source_full_match": bool(full_match),
                "retrieved_chunks": len(parts),
            }
        )

    metrics = {
        "docs_cases": len(cases),
        "vector_enabled": bool(getattr(store, "vector_enabled", False)),
        "avg_required_source_coverage": round(statistics.mean(coverage_values), 4) if coverage_values else 0.0,
        "required_source_full_match_rate": round(statistics.mean(full_match_values), 4) if full_match_values else 0.0,
        "retrieval_hit_rate": round(statistics.mean(hit_values), 4) if hit_values else 0.0,
    }
    return records, metrics


def _maybe_run_ragas(records: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Try real RAGAS scoring. Falls back to proxy when unavailable."""
    errors: list[str] = []
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        errors.append("OPENAI_API_KEY_not_set")
        return {}, errors
    try:
        from datasets import Dataset  # type: ignore
        from langchain_openai import ChatOpenAI  # type: ignore
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
        llm = ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), temperature=0)
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
    from copilot_agent.rag.ingest import DOC_FILENAMES, repo_docs_dir  # noqa: WPS433

    base = repo_docs_dir()
    if base is None:
        return False, {
            "docs_dir": None,
            "required_files": list(DOC_FILENAMES),
            "missing_files": list(DOC_FILENAMES),
        }
    missing = [name for name in DOC_FILENAMES if not (base / name).is_file()]
    return len(missing) == 0, {
        "docs_dir": str(base),
        "required_files": list(DOC_FILENAMES),
        "missing_files": missing,
    }


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
    args = parser.parse_args()

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
            "docs_precondition_failed: set WATERMARK_DOCS_PATH or restore docs/source with required markdown files"
        )

    disable_vector = bool(args.disable_vector and not args.enable_vector)
    records, proxy_metrics = _build_proxy_records(cases, top_k=args.top_k, disable_vector=disable_vector)
    ragas_metrics: dict[str, Any] = {}
    ragas_errors: list[str] = []
    eval_mode = "proxy"

    if args.mode in ("auto", "ragas"):
        ragas_metrics, ragas_errors = _maybe_run_ragas(records)
        if ragas_metrics:
            eval_mode = "ragas"
        elif args.mode == "ragas":
            errors.extend(ragas_errors)

    # Proxy gate for CI stability (works offline, deterministic).
    proxy_pass = (
        proxy_metrics["docs_cases"] >= 3
        and proxy_metrics["retrieval_hit_rate"] >= 0.9
        and proxy_metrics["required_source_full_match_rate"] >= 0.6
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
    print(f"summary_json={summary_path}")
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
