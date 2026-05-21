"""Server composition boundary."""

from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ResolvedConfig
from active_knowledge_server.mcp import ActiveKnowledgeFastMCPApp, create_fastmcp_app


def server_name() -> str:
    """Return the canonical server name."""

    return "active-knowledge-server"


def build_server_app(
    resolved: ResolvedConfig,
    *,
    cwd: Path | None = None,
) -> ActiveKnowledgeFastMCPApp:
    """Build the configured FastMCP application for CLI serve."""

    return create_fastmcp_app(resolved, cwd=cwd)
