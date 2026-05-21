from __future__ import annotations

from pathlib import Path


def resolve_config_path(value: str, *, base: Path) -> Path:
    raw = value.strip()
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()
