"""Default configuration constants."""

from __future__ import annotations

from copy import deepcopy
from typing import Final

DEFAULT_WORKDIR: Final = ".active-kb"
DEFAULT_TRANSPORT: Final = "stdio"
DEFAULT_CONFIG_FILE: Final = "active-kb.yaml"
DEFAULT_LOCAL_CONFIG_NAME: Final = "active-kb.local.yaml"
DEFAULT_SCHEMA_VERSION: Final = "0.1"

DEFAULT_CONFIG: Final[dict[str, object]] = {
    "config_schema_version": DEFAULT_SCHEMA_VERSION,
    "deployment_mode": "local_single_user",
    "server": {
        "name": "active-knowledge-server",
        "transport": DEFAULT_TRANSPORT,
        "expose_ops_tools": False,
        "http": {
            "host": "127.0.0.1",
            "port": 8765,
            "mcp_path": "/mcp",
            "require_auth": False,
            "auth_provider": "none",
            "allowed_origins": ["http://127.0.0.1", "http://localhost"],
            "trust_reverse_proxy": False,
        },
    },
    "runtime": {
        "source_root": ".",
        "workdir": DEFAULT_WORKDIR,
        "source_docs_root": "knowledge-sources",
        "log_level": "info",
    },
    "project": {
        "id": "active",
        "display_name": "Active",
        "workspace_root": ".",
        "default_snapshot": "current",
        "default_profile": "auto",
    },
    "indexing": {
        "mode": "local",
        "incremental": True,
        "reuse_baseline": True,
        "write_target": "local_overlay",
        "workers": "auto",
    },
    "query": {
        "default_top_k": 12,
        "max_evidence_items": 20,
        "evidence_required": True,
    },
    "security": {
        "audit": {"enabled": True},
        "secret_scan": {"enabled": True},
    },
}


def default_config() -> dict[str, object]:
    """Return a mutable copy of the built-in default config."""

    return deepcopy(DEFAULT_CONFIG)
