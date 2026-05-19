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
        "baseline_dir": "${runtime.workdir}/baseline",
        "local_dir": "${runtime.workdir}/local",
        "source_docs_root": "knowledge-sources",
        "log_level": "info",
        "logging": {
            "rotation": {
                "enabled": True,
                "max_bytes": 10_485_760,
                "backup_count": 5,
            },
        },
    },
    "project": {
        "id": "active",
        "display_name": "Active",
        "workspace_root": ".",
        "default_snapshot": "current",
        "default_profile": "auto",
    },
    "paths": {
        "include": [],
        "exclude": [".git", "**/.cache/**", "**/__pycache__/**"],
    },
    "profiles": {
        "discovery": {
            "defconfig_roots": ["configs"],
            "dotconfig_candidates": ["build/.config", "build/out_hub/.config"],
        },
        "known": [],
    },
    "storage": {
        "baseline": {
            "manifest": "${runtime.baseline_dir}/manifest.json",
        },
        "metadata": {
            "backend": "sqlite",
            "path": "${runtime.baseline_dir}/db/metadata.db",
            "mode": "readonly",
        },
        "overlay": {
            "backend": "sqlite",
            "path": "${runtime.local_dir}/db/overlay.db",
        },
        "jobs": {
            "backend": "sqlite",
            "path": "${runtime.local_dir}/db/jobs.db",
        },
        "vector": {
            "backend": "lancedb",
            "path": "${runtime.baseline_dir}/vectors/lancedb",
            "mode": "readonly",
        },
        "vector_delta": {
            "backend": "lancedb",
            "path": "${runtime.local_dir}/vectors/lancedb-delta",
        },
        "artifacts_root": "${runtime.baseline_dir}/artifacts",
        "local_artifacts_root": "${runtime.local_dir}/artifacts",
        "cache_root": "${runtime.local_dir}/cache",
    },
    "indexing": {
        "mode": "local",
        "incremental": True,
        "reuse_baseline": True,
        "write_target": "local_overlay",
        "workers": "auto",
        "code": {
            "enable_full_code_scan": True,
            "enable_ctags": True,
            "enable_tree_sitter": True,
            "enable_clang_index": False,
            "compile_db_candidates": [
                "build/compile_commands.json",
                "build/out_hub/compile_commands.json",
            ],
        },
        "docs": {
            "enable_markdown": True,
            "enable_html": True,
            "enable_pdf": False,
        },
        "embeddings": {
            "enabled": True,
            "provider": "local",
            "model": "bge-m3",
            "batch_size": 32,
        },
        "learned_cards": {
            "enabled": False,
            "require_review": True,
        },
    },
    "query": {
        "default_top_k": 12,
        "max_evidence_items": 20,
        "hybrid": {
            "enable_fts": True,
            "enable_vector": True,
            "enable_symbol": True,
            "enable_graph_expand": True,
            "rerank": "lightweight",
        },
        "evidence_required": True,
    },
    "security": {
        "path_allowlist": [
            "${project.workspace_root}",
            "${runtime.source_docs_root}",
            "${runtime.workdir}",
        ],
        "secret_scan": {"enabled": True},
        "audit": {"enabled": True},
    },
}


def default_config() -> dict[str, object]:
    """Return a mutable copy of the built-in default config."""

    return deepcopy(DEFAULT_CONFIG)
