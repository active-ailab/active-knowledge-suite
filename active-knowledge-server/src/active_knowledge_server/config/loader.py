"""Configuration loader boundary."""

from __future__ import annotations

from pathlib import Path


def normalize_config_path(path: str | Path) -> Path:
    """Return a normalized config path without reading it."""

    return Path(path).expanduser()
