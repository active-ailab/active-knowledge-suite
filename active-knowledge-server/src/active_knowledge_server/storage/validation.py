"""Machine-readable storage consistency validation."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage.base import (
    FTSQuery,
    QueryScope,
    StorageFTSTable,
    StorageMetadata,
    StorageWarningLevel,
)
from active_knowledge_server.storage.lancedb_store import (
    LATEST_VECTOR_SCHEMA_VERSION,
    load_store_rows,
    manifest_path,
)
from active_knowledge_server.storage.sqlite_store import (
    LATEST_SQLITE_SCHEMA_VERSION,
    SQLiteStorageAdapter,
    SQLiteTarget,
    configured_sqlite_paths,
    read_schema_version,
    sqlite_connection,
    table_exists,
    utc_now,
)

ValidateStatus = Literal["ok", "degraded", "blocked"]
CheckSeverity = Literal["info", "caution", "degraded", "blocked"]
ValidationMode = Literal["quick", "full"]


@dataclass(frozen=True)
class StorageValidationCheck:
    """One storage consistency validation finding."""

    check_code: str
    severity: CheckSeverity
    message: str
    affected_objects: tuple[str, ...] = ()
    suggested_action: str | None = None
    details: StorageMetadata = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "check_code": self.check_code,
            "severity": self.severity,
            "message": self.message,
            "affected_objects": list(self.affected_objects),
            "suggested_action": self.suggested_action,
            "details": self.details,
        }


@dataclass(frozen=True)
class StorageValidationReport:
    """Machine-readable storage validate report."""

    schema_version: str
    status: ValidateStatus
    checked_at: str
    baseline_id: str | None
    overlay_id: str | None
    checks: tuple[StorageValidationCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "checked_at": self.checked_at,
            "baseline_id": self.baseline_id,
            "overlay_id": self.overlay_id,
            "checks": [check.to_dict() for check in self.checks],
        }


def validate_storage_consistency(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    scope: QueryScope | None = None,
    mode: ValidationMode = "full",
    emit_progress: bool = False,
) -> StorageValidationReport:
    """Validate baseline/overlay metadata, FTS, vectors, evidence, and relations."""

    query_scope = scope or QueryScope(snapshot_id=config.project.default_snapshot)
    sqlite_paths = configured_sqlite_paths(config, cwd=cwd)
    baseline_vector_path = resolve_runtime_path(config.storage.vector.path, cwd)
    delta_vector_path = resolve_runtime_path(config.storage.vector_delta.path, cwd)
    adapter = SQLiteStorageAdapter(
        baseline_metadata_path=sqlite_paths["baseline_metadata"],
        overlay_metadata_path=sqlite_paths["overlay_metadata"],
        jobs_path=sqlite_paths["jobs"],
    )
    reader = adapter.reader()

    checks: list[StorageValidationCheck] = []
    progress = _validation_progress_printer(enabled=emit_progress)
    progress(f"storage validation start (mode={mode})")

    progress("check sqlite schema versions")
    checks.extend(check_sqlite_schema_versions(sqlite_paths))
    progress("check vector manifests")
    checks.extend(
        check_vector_manifests(
            baseline_vector_path=baseline_vector_path,
            delta_vector_path=delta_vector_path,
            expected_embedding_model=config.indexing.embeddings.model,
        )
    )
    if mode == "full":
        progress("check fts metadata consistency")
        checks.extend(check_fts_metadata_consistency(reader, sqlite_paths, query_scope))
        progress("check vector refs")
        checks.extend(
            check_vector_refs(reader, baseline_vector_path, delta_vector_path, query_scope)
        )
        progress("check evidence targets")
        checks.extend(check_evidence_targets(reader, query_scope))
        progress("check relation targets")
        checks.extend(check_relation_targets(reader, query_scope))
        progress("check tombstone leaks")
        checks.extend(check_tombstone_leaks(reader, sqlite_paths["overlay_metadata"], query_scope))
    progress("check replacement loops")
    checks.extend(check_replacement_loops(sqlite_paths["overlay_metadata"]))
    progress("check job locks")
    checks.extend(check_job_locks(sqlite_paths["jobs"]))
    progress("storage validation done")

    return StorageValidationReport(
        schema_version="validate_report.v1",
        status=report_status(checks),
        checked_at=utc_now(),
        baseline_id=read_baseline_id(config, cwd=cwd),
        overlay_id=f"local:{sqlite_paths['overlay_metadata']}",
        checks=tuple(checks),
    )


def check_sqlite_schema_versions(
    sqlite_paths: Mapping[SQLiteTarget, Path],
) -> tuple[StorageValidationCheck, ...]:
    checks: list[StorageValidationCheck] = []
    targets: dict[SQLiteTarget, SQLiteTarget] = {
        "baseline_metadata": "baseline_metadata",
        "overlay_metadata": "overlay_metadata",
        "jobs": "jobs",
    }
    for name, target in targets.items():
        path = sqlite_paths[name]
        if not path.exists():
            checks.append(
                StorageValidationCheck(
                    check_code="storage.schema_missing",
                    severity="degraded",
                    message=f"{target} SQLite store is missing.",
                    affected_objects=(str(path),),
                    suggested_action="Run active-kb init or migrate before indexing.",
                )
            )
            continue
        version = read_schema_version(path, target=target)
        if version != LATEST_SQLITE_SCHEMA_VERSION:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.schema_mismatch",
                    severity="blocked",
                    message=(
                        f"{target} schema version is {version!r}; "
                        f"expected {LATEST_SQLITE_SCHEMA_VERSION!r}."
                    ),
                    affected_objects=(str(path),),
                    suggested_action="Run active-kb migrate before querying this store.",
                    details={"actual": version, "expected": LATEST_SQLITE_SCHEMA_VERSION},
                )
            )
    return tuple(checks)


def check_vector_manifests(
    *,
    baseline_vector_path: Path,
    delta_vector_path: Path,
    expected_embedding_model: str,
) -> tuple[StorageValidationCheck, ...]:
    checks: list[StorageValidationCheck] = []
    for source_name, root in (("baseline", baseline_vector_path), ("overlay", delta_vector_path)):
        manifest = manifest_path(root)
        if not manifest.exists():
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
        if schema_version != LATEST_VECTOR_SCHEMA_VERSION:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.vector_schema_mismatch",
                    severity="blocked",
                    message=(
                        f"{source_name} vector manifest schema is {schema_version!r}; "
                        f"expected {LATEST_VECTOR_SCHEMA_VERSION!r}."
                    ),
                    affected_objects=(str(manifest),),
                    suggested_action="Rebuild the vector store for this source.",
                )
            )
        collections = payload.get("collections", {}) if isinstance(payload, dict) else {}
        if isinstance(collections, dict):
            for collection_name, collection in collections.items():
                if not isinstance(collection, dict):
                    continue
                versions = collection.get("embedding_model_versions", [])
                if isinstance(versions, list) and expected_embedding_model not in versions:
                    checks.append(
                        StorageValidationCheck(
                            check_code="storage.embedding_version_mismatch",
                            severity="degraded",
                            message=(
                                f"{source_name}/{collection_name} vectors use {versions}; "
                                f"expected {expected_embedding_model}."
                            ),
                            affected_objects=(f"{source_name}:{collection_name}",),
                            suggested_action="Rebuild vectors with the configured embedding model.",
                        )
                    )
    return tuple(checks)


def check_fts_metadata_consistency(
    reader: Any,
    sqlite_paths: Mapping[SQLiteTarget, Path],
    scope: QueryScope,
) -> tuple[StorageValidationCheck, ...]:
    checks: list[StorageValidationCheck] = []
    for chunk in reader.logical_chunks(scope):
        if chunk.source_index == "merged":
            continue
        path = sqlite_paths[
            "overlay_metadata" if chunk.source_index == "overlay" else "baseline_metadata"
        ]
        if not fts_object_exists(path, "chunk_fts", chunk.record.chunk_id):
            checks.append(
                StorageValidationCheck(
                    check_code="storage.fts_metadata_mismatch",
                    severity="degraded",
                    message="Live chunk metadata has no chunk_fts row.",
                    affected_objects=(f"chunk:{chunk.record.chunk_id}",),
                    suggested_action="Rebuild FTS for the affected metadata store.",
                )
            )
    for entity in reader.logical_entities(scope):
        if entity.source_index == "merged":
            continue
        path = sqlite_paths[
            "overlay_metadata" if entity.source_index == "overlay" else "baseline_metadata"
        ]
        if not fts_object_exists(path, "entity_fts", entity.record.entity_id):
            checks.append(
                StorageValidationCheck(
                    check_code="storage.fts_metadata_mismatch",
                    severity="degraded",
                    message="Live entity metadata has no entity_fts row.",
                    affected_objects=(f"entity:{entity.record.entity_id}",),
                    suggested_action="Rebuild FTS for the affected metadata store.",
                )
            )
    return tuple(checks)


def check_vector_refs(
    reader: Any,
    baseline_vector_path: Path,
    delta_vector_path: Path,
    scope: QueryScope,
) -> tuple[StorageValidationCheck, ...]:
    payload_rows = {
        row.vector_ref_id: row
        for row in (
            *load_store_rows(baseline_vector_path, ("chunk", "entity", "evidence")),
            *load_store_rows(delta_vector_path, ("chunk", "entity", "evidence")),
        )
    }
    metadata_refs = {
        vector_ref.vector_ref_id: vector_ref for vector_ref in reader.iter_vector_refs(scope)
    }
    checks: list[StorageValidationCheck] = []
    for vector_ref in metadata_refs.values():
        payload = payload_rows.get(vector_ref.vector_ref_id)
        if payload is None:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.vector_ref_missing",
                    severity="degraded",
                    message="Metadata vector_ref has no matching vector payload.",
                    affected_objects=(f"vector_ref:{vector_ref.vector_ref_id}",),
                    suggested_action="Rebuild vectors for the affected object.",
                )
            )
            continue
        mismatched_fields = tuple(
            field
            for field, metadata_value, payload_value in (
                ("object_type", vector_ref.object_type, payload.object_type),
                ("object_id", vector_ref.object_id, payload.object_id),
                ("chunk_id", vector_ref.chunk_id, payload.chunk_id),
                (
                    "embedding_model_version",
                    vector_ref.embedding_model_version,
                    payload.embedding_model_version,
                ),
                ("content_hash", vector_ref.content_hash, payload.content_hash),
                ("source_scope", vector_ref.source_scope, payload.source_scope),
                ("profile_id", vector_ref.profile_id, payload.profile_id),
            )
            if metadata_value != payload_value
        )
        if mismatched_fields:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.vector_ref_payload_mismatch",
                    severity="degraded",
                    message="Metadata vector_ref and vector payload disagree.",
                    affected_objects=(f"vector_ref:{vector_ref.vector_ref_id}",),
                    suggested_action="Rebuild vectors for the affected object.",
                    details={"fields": list(mismatched_fields)},
                )
            )
    for vector_ref_id in sorted(set(payload_rows) - set(metadata_refs)):
        checks.append(
            StorageValidationCheck(
                check_code="storage.vector_payload_orphan",
                severity="degraded",
                message="Vector payload has no matching metadata vector_ref.",
                affected_objects=(f"vector_ref:{vector_ref_id}",),
                suggested_action="Delete stale vector payloads or rebuild vectors.",
            )
        )
    return tuple(checks)


def check_evidence_targets(reader: Any, scope: QueryScope) -> tuple[StorageValidationCheck, ...]:
    checks: list[StorageValidationCheck] = []
    live_chunks = {chunk.logical_object_id for chunk in reader.logical_chunks(scope)}
    live_entities = {entity.logical_object_id for entity in reader.logical_entities(scope)}
    for evidence in reader.logical_evidence(scope):
        record = evidence.record
        if reader.get_file(record.file_id) is None:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.dangling_evidence",
                    severity="degraded",
                    message="Evidence points to a missing file.",
                    affected_objects=(f"evidence:{record.evidence_id}", f"file:{record.file_id}"),
                    suggested_action="Re-index or tombstone the stale evidence.",
                )
            )
        if record.chunk_id is not None and record.chunk_id not in live_chunks:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.dangling_evidence",
                    severity="degraded",
                    message="Evidence points to a missing chunk.",
                    affected_objects=(f"evidence:{record.evidence_id}", f"chunk:{record.chunk_id}"),
                    suggested_action="Re-index or tombstone the stale evidence.",
                )
            )
        if record.object_type == "entity" and record.object_id not in live_entities:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.dangling_evidence",
                    severity="degraded",
                    message="Evidence points to a missing entity.",
                    affected_objects=(
                        f"evidence:{record.evidence_id}",
                        f"entity:{record.object_id}",
                    ),
                    suggested_action="Re-index or tombstone the stale evidence.",
                )
            )
    return tuple(checks)


def check_relation_targets(reader: Any, scope: QueryScope) -> tuple[StorageValidationCheck, ...]:
    return tuple(
        StorageValidationCheck(
            check_code=issue.issue_code,
            severity=warning_level_to_check_severity(issue.level),
            message=issue.message,
            affected_objects=(f"relation:{issue.relation_id}",),
            suggested_action="Re-index the source relation or add a replacement mapping.",
            details=issue.metadata,
        )
        for issue in reader.validate_relations(scope)
    )


def check_tombstone_leaks(
    reader: Any,
    overlay_path: Path,
    scope: QueryScope,
) -> tuple[StorageValidationCheck, ...]:
    if not overlay_path.exists() or not table_exists_at(overlay_path, "tombstone"):
        return ()
    logical_ids = {
        "chunk": {item.logical_object_id for item in reader.logical_chunks(scope)},
        "entity": {item.logical_object_id for item in reader.logical_entities(scope)},
        "relation": {item.logical_object_id for item in reader.logical_relations(scope)},
        "evidence": {item.logical_object_id for item in reader.logical_evidence(scope)},
        "vector_ref": {item.vector_ref_id for item in reader.iter_vector_refs(scope)},
    }
    checks: list[StorageValidationCheck] = []
    with sqlite_connection(overlay_path) as connection:
        rows = connection.execute(
            """
            SELECT object_type, object_id, snapshot_id, profile_id, source_scope
            FROM tombstone
            WHERE active = 1 AND snapshot_id = ?
            """,
            (scope.snapshot_id,),
        ).fetchall()
    for row in rows:
        object_type = str(row["object_type"])
        object_id = str(row["object_id"])
        if object_type in logical_ids and object_id in logical_ids[object_type]:
            checks.append(
                StorageValidationCheck(
                    check_code="storage.tombstone_leak",
                    severity="blocked",
                    message="A tombstoned object is still visible in a logical view.",
                    affected_objects=(f"{object_type}:{object_id}",),
                    suggested_action="Fix logical filtering, then rebuild affected indexes.",
                )
            )
        if object_type in {"chunk", "entity"}:
            fts_table: StorageFTSTable = "chunk_fts" if object_type == "chunk" else "entity_fts"
            matches = tuple(
                reader.search_fts(
                    FTSQuery(index_name=fts_table, query=object_id, scope=scope, top_k=20)
                )
            )
            if any(match.logical_object_id == object_id for match in matches):
                checks.append(
                    StorageValidationCheck(
                        check_code="storage.tombstone_leak",
                        severity="blocked",
                        message="A tombstoned object is still returned by FTS.",
                        affected_objects=(f"{object_type}:{object_id}",),
                        suggested_action="Rebuild FTS after fixing tombstone filtering.",
                    )
                )
    return tuple(checks)


def check_replacement_loops(overlay_path: Path) -> tuple[StorageValidationCheck, ...]:
    if not overlay_path.exists() or not table_exists_at(overlay_path, "replacement"):
        return ()
    with sqlite_connection(overlay_path) as connection:
        rows = connection.execute(
            """
            SELECT object_type, old_object_id, new_object_id
            FROM replacement
            WHERE active = 1
            """
        ).fetchall()
    edges = {
        (str(row["object_type"]), str(row["old_object_id"])): str(row["new_object_id"])
        for row in rows
    }
    checks: list[StorageValidationCheck] = []
    for start in sorted(edges):
        seen: set[tuple[str, str]] = set()
        current = start
        while current in edges:
            if current in seen:
                object_type, object_id = current
                checks.append(
                    StorageValidationCheck(
                        check_code="storage.replacement_loop",
                        severity="blocked",
                        message="Active replacement mappings contain a loop.",
                        affected_objects=(f"{object_type}:{object_id}",),
                        suggested_action="Deactivate one replacement row in the loop.",
                    )
                )
                break
            seen.add(current)
            current = (current[0], edges[current])
    return tuple(checks)


def check_job_locks(jobs_path: Path) -> tuple[StorageValidationCheck, ...]:
    if not jobs_path.exists() or not table_exists_at(jobs_path, "job_lock"):
        return ()
    now = datetime.now(UTC)
    checks: list[StorageValidationCheck] = []
    with sqlite_connection(jobs_path) as connection:
        rows = connection.execute("SELECT * FROM job_lock ORDER BY lock_id ASC").fetchall()
    for row in rows:
        expires_at = optional_text(row["expires_at"])
        if expires_at is not None and parse_timestamp(expires_at) <= now:
            lock_id = str(row["lock_id"])
            checks.append(
                StorageValidationCheck(
                    check_code="storage.overlay_lock_stale",
                    severity="caution",
                    message="A job lock lease has expired but is still present.",
                    affected_objects=(f"job_lock:{lock_id}",),
                    suggested_action="Release stale locks before starting a new index job.",
                )
            )
    return tuple(checks)


def read_baseline_id(config: ActiveKnowledgeConfig, *, cwd: Path) -> str | None:
    manifest = resolve_runtime_path(config.storage.baseline.manifest, cwd)
    if not manifest.exists():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        value = payload.get("baseline_id")
        return str(value) if value is not None else None
    return None


def report_status(checks: Iterable[StorageValidationCheck]) -> ValidateStatus:
    severities = {check.severity for check in checks}
    if "blocked" in severities:
        return "blocked"
    if "degraded" in severities:
        return "degraded"
    return "ok"


def fts_object_exists(path: Path, table: str, object_id: str) -> bool:
    if not path.exists() or not table_exists_at(path, table):
        return False
    with sqlite_connection(path) as connection:
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE object_id = ? LIMIT 1",
            (object_id,),
        ).fetchone()
    return row is not None


def table_exists_at(path: Path, table: str) -> bool:
    if not path.exists():
        return False
    with sqlite_connection(path) as connection:
        return table_exists(connection, table)


def warning_level_to_check_severity(level: StorageWarningLevel) -> CheckSeverity:
    if level == "blocked":
        return "blocked"
    if level == "degraded":
        return "degraded"
    if level == "caution":
        return "caution"
    return "info"


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validation_progress_printer(*, enabled: bool):
    def emit(message: str) -> None:
        if not enabled:
            return
        print(f"active-kb: {message}", file=sys.stderr, flush=True)

    return emit
