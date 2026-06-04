#!/usr/bin/env python
"""Verify dependency families that commonly drift together."""

from __future__ import annotations

import json
import sys
from importlib import metadata
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_PATH = ROOT / "artifacts/runtime/dependency-compat-summary.json"


REQUIRED_RANGES = {
    "langgraph": ">=0.2.40,<0.3.0",
    "langgraph-prebuilt": ">=0.2.0,<0.3.0",
    "langgraph-checkpoint": ">=2.0.10,<3.0.0",
    "langgraph-checkpoint-sqlite": ">=2.0.0,<3.0.0",
    "langchain-core": ">=0.3.0,<0.4.0",
    "langchain-openai": ">=0.2.0,<0.3.0",
}


def _version_ok(package: str, spec: str) -> tuple[bool, str]:
    try:
        version = metadata.version(package)
    except metadata.PackageNotFoundError:
        return False, "MISSING"
    return Version(version) in SpecifierSet(spec), version


def main() -> int:
    checks: dict[str, bool] = {}
    versions: dict[str, str] = {}
    for package, spec in REQUIRED_RANGES.items():
        ok, version = _version_ok(package, spec)
        checks[f"{package}_version"] = ok
        versions[package] = version

    try:
        from langgraph.checkpoint.memory import MemorySaver  # noqa: F401
        from langgraph.graph import StateGraph  # noqa: F401
        from langgraph.prebuilt import ToolNode  # noqa: F401
    except Exception as exc:
        checks["langgraph_imports"] = False
        import_error = f"{type(exc).__name__}: {exc}"
    else:
        checks["langgraph_imports"] = True
        import_error = ""

    passed = all(checks.values())
    summary = {
        "suite_name": "dependency_compat",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "versions": versions,
        "import_error": import_error,
        "dependency_compat": "PASS" if passed else "FAIL",
    }
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"versions={json.dumps(versions, ensure_ascii=False)}")
    if import_error:
        print(f"import_error={import_error}")
    print(f"summary_json={SUMMARY_PATH}")
    print(f"dependency_compat={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
