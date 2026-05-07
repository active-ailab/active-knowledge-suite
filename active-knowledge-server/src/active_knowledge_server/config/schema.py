"""Configuration schema boundary.

Pydantic models are introduced by C1-03. This module gives downstream imports a
stable location without freezing the schema too early.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigSchemaInfo:
    """Current config schema metadata."""

    version: str = "0.1"
