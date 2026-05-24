from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_proxy_metrics(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    proxy = payload.get("proxy_metrics")
    return proxy if isinstance(proxy, dict) else None


def detect_gold_recall_regression(
    *,
    current: dict[str, Any],
    history_dir: Path,
    profile_prefix: str = "nightly",
    drop_threshold: float = 0.05,
) -> dict[str, Any]:
    """Compare current gold_chunk_recall_at_k_avg against previous nightly snapshot."""
    current_value = current.get("gold_chunk_recall_at_k_avg")
    if current_value is None:
        return {"regression": False, "reason": "metric_missing"}

    if not history_dir.is_dir():
        return {"regression": False, "reason": "no_history"}

    snapshots: list[tuple[str, float]] = []
    for path in sorted(history_dir.glob(f"{profile_prefix}-*.json")):
        proxy = _load_proxy_metrics(path)
        if not proxy:
            continue
        value = proxy.get("gold_chunk_recall_at_k_avg")
        if value is None:
            continue
        snapshots.append((path.name, float(value)))

    if len(snapshots) < 2:
        return {"regression": False, "reason": "insufficient_history", "current": current_value}

    previous_value = snapshots[-2][1]
    delta = float(current_value) - previous_value
    return {
        "regression": delta < -drop_threshold,
        "current": float(current_value),
        "previous": previous_value,
        "delta": round(delta, 4),
        "drop_threshold": drop_threshold,
    }
