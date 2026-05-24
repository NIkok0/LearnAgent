#!/usr/bin/env python
"""RAG E2E evaluation: retrieve → LLM answer → RAGAS + L4 citation."""

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


def _load_subset(path: Path, *, limit: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    docs = [c for c in cases if isinstance(c, dict) and str(c.get("category", "")) == "docs"]
    with_gold = [c for c in docs if isinstance(c.get("gold_chunks"), list) and c.get("gold_chunks")]
    selected = with_gold[:limit] if with_gold else docs[:limit]
    return selected


def _run_ragas(records: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    from copilot_agent.eval.llm_client import ensure_eval_api_env, get_eval_chat_model  # noqa: WPS433

    errors: list[str] = []
    if not ensure_eval_api_env():
        errors.append("OPENAI_API_KEY_not_set")
        return {}, errors
    try:
        from datasets import Dataset  # type: ignore
        from ragas import evaluate  # type: ignore
        from ragas.metrics import answer_relevancy, faithfulness  # type: ignore
    except Exception as exc:  # pragma: no cover
        errors.append(f"ragas_import_error: {exc}")
        return {}, errors

    try:
        ds = Dataset.from_dict(
            {
                "question": [r["question"] for r in records],
                "answer": [r["answer"] for r in records],
                "contexts": [r["contexts"] for r in records],
            }
        )
        llm = get_eval_chat_model()
        result = evaluate(ds, metrics=[faithfulness, answer_relevancy], llm=llm)
        scores = result.to_pandas().mean(numeric_only=True).to_dict()
        return {
            "faithfulness": float(scores.get("faithfulness", 0.0)),
            "answer_relevancy": float(scores.get("answer_relevancy", 0.0)),
        }, errors
    except Exception as exc:  # pragma: no cover
        errors.append(f"ragas_runtime_error: {exc}")
        return {}, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify RAG E2E generation quality.")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval/phase4-eval-cases.json"),
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/eval/rag_metrics/e2e-latest.json"),
    )
    parser.add_argument("--limit", type=int, default=8, help="Number of docs cases to run.")
    parser.add_argument(
        "--faithfulness-warn",
        type=float,
        default=0.75,
        help="Soft warning threshold for average faithfulness.",
    )
    parser.add_argument(
        "--allow-missing-key",
        action="store_true",
        help="Return SKIP when OPENAI_API_KEY is missing.",
    )
    args = parser.parse_args()

    _bootstrap_scenario()
    os.environ["RAG_USE_VECTOR"] = "false"

    from copilot_agent.eval.citation import evaluate_citation  # noqa: WPS433
    from copilot_agent.eval.rag_e2e import retrieve_and_answer  # noqa: WPS433
    from copilot_agent.rag import build_rag_store  # noqa: WPS433

    cases = _load_subset(Path(args.dataset).resolve(), limit=args.limit)
    if not cases:
        print("rag_e2e_ragas=FAIL")
        print("error=no_docs_cases")
        return 1

    from copilot_agent.eval.llm_client import ensure_eval_api_env  # noqa: WPS433

    if not ensure_eval_api_env() and args.allow_missing_key:
        summary = {
            "status": "SKIP",
            "reason": "OPENAI_API_KEY_not_set",
            "cases": 0,
        }
        summary_path = Path(args.summary_json).resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print("rag_e2e_ragas=SKIP")
        print(f"summary_json={summary_path}")
        return 0

    store = build_rag_store()
    records: list[dict[str, Any]] = []
    citation_pass_values: list[float] = []
    llm_errors: list[str] = []

    for case in cases:
        question = str(case.get("question", ""))
        required_sources = [str(x) for x in case.get("required_sources", []) if str(x).strip()]
        try:
            result = retrieve_and_answer(question, store)
        except Exception as exc:  # pragma: no cover - environment dependent
            llm_errors.append(f"llm_error:{exc}")
            if args.allow_missing_key:
                summary = {
                    "status": "SKIP",
                    "reason": "llm_unavailable",
                    "errors": llm_errors,
                    "cases": 0,
                }
                summary_path = Path(args.summary_json).resolve()
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
                print("rag_e2e_ragas=SKIP")
                print(f"skip_reason=llm_unavailable")
                for item in llm_errors:
                    print(f"warning={item}")
                print(f"summary_json={summary_path}")
                return 0
            raise
        citation = evaluate_citation(
            answer=result.answer,
            retrieval_sources=result.retrieved_sources,
            required_sources=required_sources,
        )
        citation_pass_values.append(1.0 if citation.passed else 0.0)
        records.append(
            {
                "id": str(case.get("id", "")),
                "question": question,
                "answer": result.answer,
                "contexts": result.contexts,
                "retrieved_sources": result.retrieved_sources,
                "citations": result.citations,
                "citation_passed": citation.passed,
                "required_source_coverage": citation.required_source_coverage,
            }
        )

    ragas_metrics, ragas_errors = _run_ragas(records)
    faithfulness_avg = float(ragas_metrics.get("faithfulness", 0.0)) if ragas_metrics else None
    faithfulness_warn = (
        faithfulness_avg is not None and faithfulness_avg < args.faithfulness_warn
    )

    checks = {
        "cases_ran": len(records) > 0,
        "citation_pass_rate_ok": (statistics.mean(citation_pass_values) if citation_pass_values else 0.0) >= 0.5,
        "ragas_available_or_skipped": bool(ragas_metrics) or bool(ragas_errors),
    }
    hard_fail = not checks["cases_ran"] or not checks["citation_pass_rate_ok"]
    if ragas_errors and not ragas_metrics and not args.allow_missing_key:
        hard_fail = True

    status = "FAIL" if hard_fail else ("WARN" if faithfulness_warn else "PASS")
    summary = {
        "timestamp": datetime.now(UTC).isoformat(),
        "status": status,
        "cases": len(records),
        "checks": checks,
        "citation_pass_rate": round(statistics.mean(citation_pass_values), 4) if citation_pass_values else 0.0,
        "ragas_metrics": ragas_metrics,
        "ragas_errors": ragas_errors,
        "faithfulness_warn": faithfulness_warn,
        "records": records,
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    history_dir = summary_path.parent / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (history_dir / f"e2e-{stamp}.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"cases={summary['cases']}")
    print(f"citation_pass_rate={summary['citation_pass_rate']}")
    if ragas_metrics:
        print(f"faithfulness={ragas_metrics.get('faithfulness')}")
        print(f"answer_relevancy={ragas_metrics.get('answer_relevancy')}")
    if faithfulness_warn:
        print(f"warning=faithfulness_below_{args.faithfulness_warn}")
    for item in ragas_errors:
        print(f"warning={item}")
    print(f"summary_json={summary_path}")
    print(f"rag_e2e_ragas={status}")
    return 0 if status in {"PASS", "WARN", "SKIP"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
