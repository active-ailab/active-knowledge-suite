"""Application bootstrap helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BootstrapInfo:
    """Minimal bootstrap metadata exposed before config loading is implemented."""

    app_name: str = "active-knowledge-server"


def get_bootstrap_info() -> BootstrapInfo:
    """Return static bootstrap metadata."""

    return BootstrapInfo()
