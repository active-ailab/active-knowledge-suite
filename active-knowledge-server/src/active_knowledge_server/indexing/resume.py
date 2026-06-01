"""Deterministic resume contracts for incremental indexing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from active_knowledge_server.config.defaults import DEFAULT_SCHEMA_VERSION
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import SOURCE_DOCS_MANIFEST_SCHEMA_VERSION
from active_knowledge_server.connectors.workspace import WORKSPACE_INVENTORY_SCHEMA_VERSION
from active_knowledge_server.indexing.code_indexer import CODE_INDEXER_SCHEMA_VERSION
from active_knowledge_server.indexing.doc_indexer import DOC_INDEXER_SCHEMA_VERSION
from active_knowledge_server.indexing.embeddings import EMBEDDING_PREPARATION_SCHEMA_VERSION
from active_knowledge_server.indexing.profile import PROFILE_COLLECTOR_SCHEMA_VERSION
from active_knowledge_server.indexing.relation_extractor import (
    PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
)
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID
from active_knowledge_server.indexing.workspace_map import WORKSPACE_MAP_SCHEMA_VERSION
from active_knowledge_server.parsers import (
    C_FAMILY_PARSER_SCHEMA_VERSION,
    DOC_PARSER_SCHEMA_VERSION,
    KCONFIG_PARSER_SCHEMA_VERSION,
    MAKEFILE_PARSER_SCHEMA_VERSION,
)
from active_knowledge_server.security.secret_scan import SECRET_SCAN_SCHEMA_VERSION
from active_knowledge_server.storage.lancedb_store import LATEST_VECTOR_SCHEMA_VERSION
from active_knowledge_server.storage.sqlite_store import LATEST_SQLITE_SCHEMA_VERSION

INDEX_PLAN_SIGNATURE_SCHEMA_VERSION: Final = "index_plan_signature.v1"


@dataclass(frozen=True)
class IndexPlanSignature:
    """One deterministic identity for a resumable incremental index plan."""

    schema_version: str
    digest: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable signature payload."""

        return {
            "schema_version": self.schema_version,
            "digest": self.digest,
            "payload": dict(self.payload),
        }


def make_index_plan_signature(
    plan: object,
    *,
    config: ActiveKnowledgeConfig,
    mode: str | None = None,
    target: str | None = None,
    parser_schema_versions: Mapping[str, str] | None = None,
    storage_schema_version: str = LATEST_SQLITE_SCHEMA_VERSION,
    vector_schema_version: str = LATEST_VECTOR_SCHEMA_VERSION,
) -> IndexPlanSignature:
    """Build the stable plan signature used to decide whether resume is valid."""

    current_state = plan.current_state
    impacting_config = _indexing_impacting_config(config)
    parser_schemas = parser_schema_versions or {
        "c_family": C_FAMILY_PARSER_SCHEMA_VERSION,
        "doc": DOC_PARSER_SCHEMA_VERSION,
        "kconfig": KCONFIG_PARSER_SCHEMA_VERSION,
        "makefile": MAKEFILE_PARSER_SCHEMA_VERSION,
    }
    payload: dict[str, object] = {
        "schema_version": INDEX_PLAN_SIGNATURE_SCHEMA_VERSION,
        "mode": mode or config.indexing.mode,
        "target": target or config.indexing.write_target,
        "source": str(getattr(plan, "source", "all")),
        "snapshot_id": str(
            getattr(plan, "snapshot_id", None)
            or getattr(current_state, "snapshot_id", None)
            or CURRENT_SNAPSHOT_ID
        ),
        "workspace_inventory_hash": str(getattr(current_state, "workspace_inventory_hash", "")),
        "source_docs_manifest_hash": str(getattr(current_state, "source_docs_manifest_hash", "")),
        "profile_manifest_hash": _stable_digest(
            dict(sorted(getattr(current_state, "profile_config_hashes", {}).items()))
        ),
        "schemas": {
            "config": DEFAULT_SCHEMA_VERSION,
            "workspace_inventory": WORKSPACE_INVENTORY_SCHEMA_VERSION,
            "source_docs_manifest": SOURCE_DOCS_MANIFEST_SCHEMA_VERSION,
            "parser": dict(sorted(parser_schemas.items())),
            "code_indexer": CODE_INDEXER_SCHEMA_VERSION,
            "doc_indexer": DOC_INDEXER_SCHEMA_VERSION,
            "embedding_preparation": EMBEDDING_PREPARATION_SCHEMA_VERSION,
            "profile_collector": PROFILE_COLLECTOR_SCHEMA_VERSION,
            "profile_conditioned_relations": PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
            "workspace_map": WORKSPACE_MAP_SCHEMA_VERSION,
            "secret_scan": SECRET_SCAN_SCHEMA_VERSION,
            "storage": storage_schema_version,
            "vector_storage": vector_schema_version,
        },
        "embeddings": {
            "enabled": bool(config.indexing.embeddings.enabled),
            "provider": config.indexing.embeddings.provider,
            "model": config.indexing.embeddings.model,
        },
        "impacting_config_hash": _stable_digest(impacting_config),
        "impacting_config_summary": impacting_config,
    }
    return IndexPlanSignature(
        schema_version=INDEX_PLAN_SIGNATURE_SCHEMA_VERSION,
        digest=_stable_digest(payload),
        payload=payload,
    )


def diff_plan_signature_payloads(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> tuple[str, ...]:
    """Return dotted payload fields that differ between two plan signatures."""

    previous_payload = _signature_payload(previous)
    current_payload = _signature_payload(current)
    previous_flat = _flatten_mapping(previous_payload)
    current_flat = _flatten_mapping(current_payload)
    return tuple(
        sorted(
            key
            for key in set(previous_flat) | set(current_flat)
            if previous_flat.get(key) != current_flat.get(key)
        )
    )


def format_plan_signature_mismatch_reason(
    previous: Mapping[str, object],
    current: Mapping[str, object],
    *,
    max_fields: int = 8,
) -> str:
    """Return a short human-readable explanation for a resume signature mismatch."""

    changed = diff_plan_signature_payloads(previous, current)
    if not changed:
        return "plan signature payloads match"
    displayed = changed[: max(1, max_fields)]
    suffix = "" if len(changed) <= len(displayed) else f" (+{len(changed) - len(displayed)} more)"
    return "plan signature mismatch: " + ", ".join(displayed) + suffix


def _indexing_impacting_config(config: ActiveKnowledgeConfig) -> dict[str, object]:
    return {
        "paths": {
            "include": list(config.paths.include),
            "exclude": list(config.paths.exclude),
        },
        "project": {
            "default_profile": config.project.default_profile,
        },
        "profiles": config.profiles.model_dump(mode="json"),
        "indexing": {
            "code": config.indexing.code.model_dump(mode="json"),
            "docs": config.indexing.docs.model_dump(mode="json"),
            "embeddings": {
                "enabled": config.indexing.embeddings.enabled,
                "provider": config.indexing.embeddings.provider,
                "model": config.indexing.embeddings.model,
            },
            "learned_cards": config.indexing.learned_cards.model_dump(mode="json"),
        },
        "security": {
            "secret_scan": config.security.secret_scan.model_dump(mode="json"),
        },
    }


def _signature_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    payload = value.get("payload")
    if isinstance(payload, Mapping):
        return payload
    return value


def _flatten_mapping(
    value: object,
    *,
    prefix: str = "",
) -> dict[str, object]:
    if isinstance(value, Mapping):
        flattened: dict[str, object] = {}
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_mapping(item, prefix=child_prefix))
        return flattened
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {f"{prefix}[{index}]": item for index, item in enumerate(value)}
    return {prefix: value}


def _stable_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
