"""Server composition boundary.

FastMCP wiring is implemented in M6-01. This module exists now so later tasks
have a stable import target for application assembly.
"""

from __future__ import annotations


def server_name() -> str:
    """Return the canonical server name."""

    return "active-knowledge-server"
