"""Deterministic reproducibility benchmark used by the E7-06 gate."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import asdict, is_dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.source_docs import SourceDocsConnector
from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.eval.benchmark import (
    _resolve_benchmark_config,
    _seed_docs,
    _seed_workspace,
)
from active_knowledge_server.eval.cases import EvalCaseSuite
from active_knowledge_server.indexing import (
    CodeIndexer,
    DocumentIndexer,
    ProfileCollector,
    ProfileConditionedRelationExtractor,
    SnapshotCollector,
)
from active_knowledge_server.storage import (
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    ProfileRecord,
    RelationRecord,
    VectorRefRecord,
)


class ReproducibilityBenchmark:
    """Collect the same synthetic corpus twice and compare stable IDs and hashes."""

    def __init__(self) -> None:
        self._tmpdir = TemporaryDirectory(prefix="active-kb-reproducibility-")
        self._root = Path(self._tmpdir.name)
        self._resolved = _resolve_benchmark_config(
            self._root,
            overrides={"project": {"default_profile": "mhs003_watch"}},
        )
        self._config = self._resolved.model
        self._workspace_root = Path(self._config.project.workspace_root)
        self._docs_root = Path(self._config.runtime.source_docs_root)
        _seed_workspace(self._workspace_root)
        _seed_docs(self._docs_root)
        _seed_profile_fixture(self._workspace_root)

    def measure_suite(self, suite: EvalCaseSuite) -> dict[str, object]:
        del suite
        first = self._collect_once()
        second = self._collect_once()
        return {
            "first_snapshot_id": first["snapshot_id"],
            "second_snapshot_id": second["snapshot_id"],
            "first_profile_ids": first["profile_ids"],
            "second_profile_ids": second["profile_ids"],
            "first_profile_record_ids": first["profile_record_ids"],
            "second_profile_record_ids": second["profile_record_ids"],
            "first_entity_ids": first["entity_ids"],
            "second_entity_ids": second["entity_ids"],
            "first_chunk_ids": first["chunk_ids"],
            "second_chunk_ids": second["chunk_ids"],
            "first_evidence_ids": first["evidence_ids"],
            "second_evidence_ids": second["evidence_ids"],
            "first_vector_ref_ids": first["vector_ref_ids"],
            "second_vector_ref_ids": second["vector_ref_ids"],
            "first_core_report_hash": first["core_report_hash"],
            "second_core_report_hash": second["core_report_hash"],
            "first_report_counts": first["report_counts"],
            "second_report_counts": second["report_counts"],
        }

    def environment(self) -> dict[str, object]:
        return {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count() or 1,
        }

    def dataset_scale(self) -> dict[str, object]:
        return {
            "workspace_files": _count_files(self._workspace_root),
            "workspace_bytes": _total_size(self._workspace_root),
            "source_docs_files": _count_files(self._docs_root),
            "source_docs_bytes": _total_size(self._docs_root),
        }

    def close(self) -> None:
        self._tmpdir.cleanup()

    def _collect_once(self) -> dict[str, object]:
        workspace_inventory = WorkspaceConnector.from_config(
            self._config,
            cwd=self._root,
        ).scan()
        source_docs_manifest = SourceDocsConnector.from_config(
            self._config,
            cwd=self._root,
        ).scan()
        snapshot = SnapshotCollector.from_config(self._config, cwd=self._root).collect(
            workspace_inventory,
            created_at="2026-05-26T00:00:00Z",
        )
        profiles = ProfileCollector.from_config(self._config, cwd=self._root).collect(
            snapshot.snapshot_id,
            requested_profile_id="mhs003_watch",
        )
        code = CodeIndexer.from_config(self._config, cwd=self._root).collect(
            snapshot_id=snapshot.snapshot_id,
            workspace_inventory=workspace_inventory,
        )
        docs = DocumentIndexer.from_config(self._config, cwd=self._root).collect(
            snapshot_id=snapshot.snapshot_id,
            source_docs_manifest=source_docs_manifest,
        )
        profile_relations = ProfileConditionedRelationExtractor().collect(
            snapshot_id=snapshot.snapshot_id,
            profiles=profiles.profile_records,
            entities=code.entity_records,
            relations=code.relation_records,
        )
        records = _CollectedIndexRecords(
            config=self._config,
            snapshot_id=snapshot.snapshot_id,
            workspace_inventory_hash=workspace_inventory.inventory_hash,
            source_manifest_hash=source_docs_manifest.manifest_hash,
            profiles=profiles.profile_records,
            code_files=code.file_records,
            doc_files=docs.file_records,
            code_chunks=code.chunk_records,
            doc_chunks=docs.chunk_records,
            code_entities=code.entity_records,
            doc_entities=docs.entity_records,
            code_evidence=code.evidence_records,
            doc_evidence=docs.evidence_records,
            code_relations=code.relation_records,
            profile_relations=profile_relations.relation_records,
            vector_refs=docs.vector_refs,
        )
        core_report = records.core_report()
        return {
            "snapshot_id": records.snapshot_id,
            "profile_ids": records.profile_ids(),
            "profile_record_ids": records.profile_record_ids(),
            "entity_ids": records.entity_ids(),
            "chunk_ids": records.chunk_ids(),
            "evidence_ids": records.evidence_ids(),
            "vector_ref_ids": records.vector_ref_ids(),
            "report_counts": records.report_counts(),
            "core_report_hash": _stable_hash(core_report),
        }


class _CollectedIndexRecords:
    def __init__(
        self,
        *,
        config: ActiveKnowledgeConfig,
        snapshot_id: str,
        workspace_inventory_hash: str,
        source_manifest_hash: str,
        profiles: tuple[ProfileRecord, ...],
        code_files: tuple[FileRecord, ...],
        doc_files: tuple[FileRecord, ...],
        code_chunks: tuple[ChunkRecord, ...],
        doc_chunks: tuple[ChunkRecord, ...],
        code_entities: tuple[EntityRecord, ...],
        doc_entities: tuple[EntityRecord, ...],
        code_evidence: tuple[EvidenceRecord, ...],
        doc_evidence: tuple[EvidenceRecord, ...],
        code_relations: tuple[RelationRecord, ...],
        profile_relations: tuple[RelationRecord, ...],
        vector_refs: tuple[VectorRefRecord, ...],
    ) -> None:
        self.config = config
        self.snapshot_id = snapshot_id
        self.workspace_inventory_hash = workspace_inventory_hash
        self.source_manifest_hash = source_manifest_hash
        self.profiles = profiles
        self.files = (*code_files, *doc_files)
        self.chunks = (*code_chunks, *doc_chunks)
        self.entities = (*code_entities, *doc_entities)
        self.evidence = (*code_evidence, *doc_evidence)
        self.relations = (*code_relations, *profile_relations)
        self.vector_refs = vector_refs

    def profile_ids(self) -> list[str]:
        return sorted(record.profile_id for record in self.profiles)

    def profile_record_ids(self) -> list[str]:
        return sorted(record.profile_record_id for record in self.profiles)

    def entity_ids(self) -> list[str]:
        return sorted(record.entity_id for record in self.entities)

    def chunk_ids(self) -> list[str]:
        return sorted(record.chunk_id for record in self.chunks)

    def evidence_ids(self) -> list[str]:
        return sorted(record.evidence_id for record in self.evidence)

    def vector_ref_ids(self) -> list[str]:
        return sorted(record.vector_ref_id for record in self.vector_refs)

    def core_report(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "workspace_inventory_hash": self.workspace_inventory_hash,
            "source_manifest_hash": self.source_manifest_hash,
            "default_profile": self.config.project.default_profile,
            "profile_ids": self.profile_ids(),
            "profile_record_ids": self.profile_record_ids(),
            "files": _canonical_records(self.files, id_attr="file_id"),
            "chunks": _canonical_records(self.chunks, id_attr="chunk_id"),
            "entities": _canonical_records(self.entities, id_attr="entity_id"),
            "evidence": _canonical_records(self.evidence, id_attr="evidence_id"),
            "relations": _canonical_records(self.relations, id_attr="relation_id"),
            "vector_refs": _canonical_records(self.vector_refs, id_attr="vector_ref_id"),
        }

    def report_counts(self) -> dict[str, int]:
        return {
            "profiles": len(self.profiles),
            "files": len(self.files),
            "chunks": len(self.chunks),
            "entities": len(self.entities),
            "evidence": len(self.evidence),
            "relations": len(self.relations),
            "vector_refs": len(self.vector_refs),
        }


def _canonical_records(records: tuple[object, ...], *, id_attr: str) -> list[dict[str, object]]:
    return sorted(
        (cast(dict[str, object], _normalize_value(_record_payload(record))) for record in records),
        key=lambda item: str(item.get(id_attr, "")),
    )


def _record_payload(record: object) -> dict[str, object]:
    if is_dataclass(record):
        payload = asdict(record)  # type: ignore[arg-type]
    elif hasattr(record, "to_dict"):
        raw_payload = record.to_dict()
        payload = raw_payload if isinstance(raw_payload, dict) else {"value": raw_payload}
    else:
        payload = dict(vars(record))
    return {key: value for key, value in payload.items() if key not in {"created_at", "updated_at"}}


def _normalize_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _normalize_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if key not in {"created_at", "updated_at", "freshness_ts"}
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _seed_profile_fixture(workspace_root: Path) -> None:
    defconfig_path = workspace_root / "configs" / "mhs003_watch_defconfig"
    dotconfig_path = workspace_root / "build" / ".config"
    defconfig_path.parent.mkdir(parents=True, exist_ok=True)
    dotconfig_path.parent.mkdir(parents=True, exist_ok=True)
    defconfig_path.write_text(
        'CONFIG_APP="watch"\nCONFIG_BOARD="mhs003"\nCONFIG_FEATURE_WATCH=y\n',
        encoding="utf-8",
    )
    dotconfig_path.write_text(
        'CONFIG_APP="watch"\nCONFIG_BOARD="mhs003"\nCONFIG_RUNTIME_READY=y\n',
        encoding="utf-8",
    )


def _count_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file())


def _total_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
