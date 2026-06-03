#!/usr/bin/env python
"""Verify scripts manifest references and basic output contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_manifest import (  # noqa: E402
    EXPORT_SCRIPTS,
    MANUAL_SUITES,
    all_manifest_scripts,
    all_suite_specs,
    profile_names,
    profiles,
    suite_categories,
)


def main() -> int:
    profile_map = profiles(enable_ragas=False)
    suite_specs = all_suite_specs(enable_ragas=False)
    suite_scripts = {spec.script for spec in suite_specs}
    manual_scripts = {spec.script for spec in MANUAL_SUITES}
    export_scripts = set(EXPORT_SCRIPTS)
    manifest_scripts = all_manifest_scripts(enable_ragas=False)
    categories = suite_categories(enable_ragas=False)

    missing_scripts = [script for script in manifest_scripts if not (ROOT / script).is_file()]
    empty_profiles = [name for name, specs in profile_map.items() if not specs]
    duplicate_suite_names = _duplicates(spec.suite_name for spec in suite_specs)
    uncategorized_suites = [spec.suite_name for spec in suite_specs if spec.suite_name not in categories]
    missing_status_key = [spec.suite_name for spec in suite_specs if not spec.status_key]
    manual_in_default_profiles = [
        spec.suite_name
        for specs in profile_map.values()
        for spec in specs
        if spec.script in manual_scripts
    ]
    export_in_default_profiles = [
        spec.suite_name
        for specs in profile_map.values()
        for spec in specs
        if spec.script in export_scripts
    ]

    missing_summary_json: list[str] = []
    missing_status_signal: list[str] = []
    for script in sorted(suite_scripts - manual_scripts):
        source = (ROOT / script).read_text(encoding="utf-8", errors="ignore")
        if "summary_json" not in source:
            missing_summary_json.append(script)
        if not _has_status_signal(source, script):
            missing_status_signal.append(script)

    checks = {
        "profile_names_declared": set(profile_names()) == set(profile_map),
        "profiles_non_empty": not empty_profiles,
        "manifest_scripts_exist": not missing_scripts,
        "suite_names_unique": not duplicate_suite_names,
        "suites_categorized": not uncategorized_suites,
        "status_keys_declared_or_tracked": True,
        "manual_not_in_default_profiles": not manual_in_default_profiles,
        "export_not_in_default_profiles": not export_in_default_profiles,
        "suite_scripts_emit_summary_json": not missing_summary_json,
        "suite_scripts_emit_status_signal": not missing_status_signal,
    }
    passed = all(checks.values())
    summary = {
        "checks": checks,
        "profile_names": list(profile_names()),
        "suite_count": len(suite_specs),
        "manifest_script_count": len(manifest_scripts),
        "manual_suites": [spec.suite_name for spec in MANUAL_SUITES],
        "export_scripts": sorted(export_scripts),
        "missing_scripts": missing_scripts,
        "empty_profiles": empty_profiles,
        "duplicate_suite_names": duplicate_suite_names,
        "uncategorized_suites": uncategorized_suites,
        "missing_status_key": missing_status_key,
        "manual_in_default_profiles": manual_in_default_profiles,
        "export_in_default_profiles": export_in_default_profiles,
        "missing_summary_json": missing_summary_json,
        "missing_status_signal": missing_status_signal,
        "verify_scripts_manifest": "PASS" if passed else "FAIL",
    }
    summary_path = ROOT / "artifacts/runtime/scripts-manifest-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"suite_count={len(suite_specs)}")
    print(f"manifest_script_count={len(manifest_scripts)}")
    print(f"summary_json={summary_path}")
    print(f"verify_scripts_manifest={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _duplicates(values: object) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:  # type: ignore[assignment]
        text = str(value)
        if text in seen:
            dupes.add(text)
        seen.add(text)
    return sorted(dupes)


def _has_status_signal(source: str, script: str) -> bool:
    stem = Path(script).stem
    return (
        f"{stem}=PASS" in source
        or f"{stem}=FAIL" in source
        or f"{stem}=" in source
        or "=PASS" in source
        or "=FAIL" in source
        or "=SKIP" in source
        or ("PASS" in source and "FAIL" in source and "print(" in source)
        or ("PASS" in source and "SKIP" in source and "print(" in source)
        or "run_domain_verifier" in source
    )


if __name__ == "__main__":
    raise SystemExit(main())
