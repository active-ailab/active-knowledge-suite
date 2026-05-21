from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.routing import RouteTraceEntry, RouterDecision, ToolPlan
from active_knowledge_server.query import EvidencePackager, QueryService
from active_knowledge_server.query.rerank import FusionCandidate, RetrievalSignal
from active_knowledge_server.query.retrievers import (
    FullTextMatchResult,
    FullTextSearchRequest,
    FullTextSearchResult,
)
from active_knowledge_server.security.secret_scan import REDACTED_SECRET_MARKER
from active_knowledge_server.storage import (
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    LogicalEvidence,
    QueryScope,
)


@dataclass
class StubEvidenceReader:
    evidences: tuple[LogicalEvidence, ...]
    chunks: dict[str, ChunkRecord]
    entities: dict[str, EntityRecord]
    files: dict[str, FileRecord]

    def logical_evidence(self, scope: QueryScope) -> tuple[LogicalEvidence, ...]:
        return self.evidences

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        return self.chunks.get(chunk_id)

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        return self.entities.get(entity_id)

    def get_file(self, file_id: str) -> FileRecord | None:
        return self.files.get(file_id)


@dataclass
class StubMetadataAdapter:
    stub_reader: StubEvidenceReader

    def reader(self) -> StubEvidenceReader:
        return self.stub_reader


@dataclass
class StubRouter:
    decision: RouterDecision

    def route(self, request: QueryRequest) -> RouterDecision:
        return self.decision


@dataclass
class StubFullTextRetriever:
    result: FullTextSearchResult

    def search(self, request: FullTextSearchRequest) -> FullTextSearchResult:
        return self.result


def resolve_model(tmp_path: Path, overrides: ConfigDict | None = None) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    docs.mkdir()
    merged: ConfigDict = {
        "runtime": {
            "workdir": str(tmp_path / ".active-kb"),
            "baseline_dir": str(tmp_path / ".active-kb" / "baseline"),
            "local_dir": str(tmp_path / ".active-kb" / "local"),
            "source_docs_root": str(docs),
        },
        "project": {
            "workspace_root": str(workspace),
            "default_profile": "auto",
        },
        "storage": {
            "baseline": {
                "manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")
            },
            "metadata": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "db" / "metadata.db"),
                "mode": "readwrite",
            },
            "overlay": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "overlay.db"),
                "mode": "readwrite",
            },
            "jobs": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "jobs.db"),
                "mode": "readwrite",
            },
            "vector": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "vectors"),
                "mode": "readwrite",
            },
            "vector_delta": {
                "path": str(tmp_path / ".active-kb" / "local" / "vectors"),
                "mode": "readwrite",
            },
            "cache_root": str(tmp_path / ".active-kb" / "local" / "cache"),
        },
    }
    if overrides:
        merged = deep_merge(merged, overrides)
    return resolve_config(cli_overrides=merged, env={}, cwd=tmp_path).model


def make_candidate(
    *,
    candidate_id: str,
    object_type: str,
    path: str,
    snippet: str,
    evidence_keys: tuple[str, ...],
    authority_level: str = "workspace_code",
) -> FusionCandidate:
    return FusionCandidate(
        candidate_id=candidate_id,
        object_type=object_type,
        title=candidate_id,
        snippet=snippet,
        relative_path=path,
        profile_id="watch",
        raw_score=0.82,
        source_index="baseline",
        source_scope="workspace",
        authority_level=authority_level,
        evidence_keys=evidence_keys,
        retrieval_signals=(
            RetrievalSignal(
                retriever="fts",
                rank=1,
                weight=1.0,
                raw_score=0.82,
                rrf_score=0.82,
                match_reason="matched test candidate",
            ),
        ),
        rerank_score=0.91,
        metadata={"path": path, "start_line": 12, "end_line": 24},
    )


def test_evidence_packager_prefers_logical_evidence_excerpt_and_sanitizes_it(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    file_record = FileRecord(
        file_id="file:sensor",
        snapshot_id="current",
        source_id="source:api",
        relative_path="knowledge-sources/api/sensor.md",
        content_hash="sha256:file-sensor",
        source_scope="api",
    )
    chunk_record = ChunkRecord(
        chunk_id="chunk:sensor",
        snapshot_id="current",
        file_id=file_record.file_id,
        content_hash="sha256:chunk-sensor",
        chunk_type="doc.api",
        ordinal=1,
        text="sensor_open API quick reference",
        source_scope="api",
        start_line=12,
        end_line=24,
    )
    long_secret_excerpt = " ".join(["token = supersecret123456"] * 24)
    reader = StubEvidenceReader(
        evidences=(
            LogicalEvidence(
                logical_object_id="evidence:sensor",
                physical_object_id="evidence:sensor",
                source_index="baseline",
                record=EvidenceRecord(
                    evidence_id="evidence:sensor",
                    snapshot_id="current",
                    object_type="chunk",
                    object_id=chunk_record.chunk_id,
                    file_id=file_record.file_id,
                    source_scope="api",
                    chunk_id=chunk_record.chunk_id,
                    excerpt=long_secret_excerpt,
                    start_line=12,
                    end_line=24,
                    metadata={
                        "path": file_record.relative_path,
                        "authority_level": "source_doc",
                    },
                ),
            ),
        ),
        chunks={chunk_record.chunk_id: chunk_record},
        entities={},
        files={file_record.file_id: file_record},
    )
    packager = EvidencePackager.from_config(
        config,
        metadata_adapter=StubMetadataAdapter(reader),
    )

    refs, trace = packager.bundle_for_query(
        scope=QueryScope(snapshot_id="current", profile_id="watch"),
        candidates=(
            make_candidate(
                candidate_id="doc:sensor",
                object_type="chunk",
                path=file_record.relative_path,
                snippet="fallback candidate snippet should not win",
                evidence_keys=("evidence:sensor",),
                authority_level="source_doc",
            ),
        ),
    )

    assert len(refs) == 1
    assert refs[0].path == "knowledge-sources/api/sensor.md"
    assert refs[0].content_hash == "sha256:chunk-sensor"
    assert refs[0].excerpt is not None
    assert REDACTED_SECRET_MARKER in refs[0].excerpt
    assert "supersecret123456" not in refs[0].excerpt
    assert len(refs[0].excerpt) <= 220
    assert trace[0]["candidate_ids"] == ["doc:sensor"]


def test_evidence_packager_falls_back_to_candidate_excerpt_with_limit(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    packager = EvidencePackager(
        secret_scanner=EvidencePackager.from_config(config)._secret_scanner,
        max_evidence_items=20,
        max_excerpt_chars=80,
    )

    refs, _ = packager.bundle_for_query(
        scope=QueryScope(snapshot_id="current", profile_id="watch"),
        candidates=(
            make_candidate(
                candidate_id="entity:alpha",
                object_type="entity",
                path="src/alpha.c",
                snippet=" ".join(["password = supersecret123456"] * 10),
                evidence_keys=(),
            ),
        ),
    )

    assert len(refs) == 1
    assert refs[0].path == "src/alpha.c"
    assert refs[0].excerpt is not None
    assert REDACTED_SECRET_MARKER in refs[0].excerpt
    assert "supersecret123456" not in refs[0].excerpt
    assert len(refs[0].excerpt) <= 80
    assert refs[0].content_hash is not None


def test_evidence_packager_supports_entity_bundle(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    file_record = FileRecord(
        file_id="file:alpha",
        snapshot_id="current",
        source_id="source:workspace",
        relative_path="src/alpha.c",
        content_hash="sha256:file-alpha",
    )
    entity_record = EntityRecord(
        entity_id="entity:alpha",
        snapshot_id="current",
        file_id=file_record.file_id,
        entity_type="Function",
        name="alpha",
        qualified_name="src/alpha.c::alpha",
        path="src/alpha.c#alpha",
        start_line=10,
        end_line=18,
        metadata={"summary": "alpha initializes the runtime pipeline"},
    )
    reader = StubEvidenceReader(
        evidences=(
            LogicalEvidence(
                logical_object_id="evidence:alpha",
                physical_object_id="evidence:alpha",
                source_index="baseline",
                record=EvidenceRecord(
                    evidence_id="evidence:alpha",
                    snapshot_id="current",
                    object_type="entity",
                    object_id=entity_record.entity_id,
                    file_id=file_record.file_id,
                    excerpt="alpha initializes the runtime pipeline",
                    start_line=10,
                    end_line=18,
                    metadata={"path": file_record.relative_path},
                ),
            ),
        ),
        chunks={},
        entities={entity_record.entity_id: entity_record},
        files={file_record.file_id: file_record},
    )
    packager = EvidencePackager.from_config(
        config,
        metadata_adapter=StubMetadataAdapter(reader),
    )

    refs = packager.bundle_for_entity(
        scope=QueryScope(snapshot_id="current", profile_id="watch"),
        entity_id="entity:alpha",
    )

    assert len(refs) == 1
    assert refs[0].path == "src/alpha.c"
    assert refs[0].start_line == 10
    assert refs[0].end_line == 18
    assert refs[0].excerpt == "alpha initializes the runtime pipeline"


def test_query_service_uses_storage_backed_evidence_packager(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    file_record = FileRecord(
        file_id="file:sensor",
        snapshot_id="current",
        source_id="source:api",
        relative_path="knowledge-sources/api/sensor.md",
        content_hash="sha256:file-sensor",
        source_scope="api",
    )
    chunk_record = ChunkRecord(
        chunk_id="chunk:sensor",
        snapshot_id="current",
        file_id=file_record.file_id,
        content_hash="sha256:chunk-sensor",
        chunk_type="doc.api",
        ordinal=1,
        text="sensor_open API quick reference",
        source_scope="api",
        start_line=12,
        end_line=24,
    )
    reader = StubEvidenceReader(
        evidences=(
            LogicalEvidence(
                logical_object_id="evidence:sensor",
                physical_object_id="evidence:sensor",
                source_index="baseline",
                record=EvidenceRecord(
                    evidence_id="evidence:sensor",
                    snapshot_id="current",
                    object_type="chunk",
                    object_id=chunk_record.chunk_id,
                    file_id=file_record.file_id,
                    source_scope="api",
                    chunk_id=chunk_record.chunk_id,
                    excerpt="stored excerpt from evidence catalog",
                    start_line=12,
                    end_line=24,
                    metadata={
                        "path": file_record.relative_path,
                        "authority_level": "source_doc",
                    },
                ),
            ),
        ),
        chunks={chunk_record.chunk_id: chunk_record},
        entities={},
        files={file_record.file_id: file_record},
    )
    router = StubRouter(
        RouterDecision(
            normalized_query="sensor open",
            intent="api_lookup",
            confidence=0.86,
            selected_view="evidence",
            selected_granularity="doc_section",
            profile_resolution={
                "status": "resolved",
                "resolved_profile_id": "watch",
                "warnings": [],
            },
            retriever_weights={"symbol": 0.0, "fts": 1.0, "vector": 0.0, "graph": 0.0},
            tool_plan=ToolPlan(route_mode="explore", primary_tool="docs_search"),
            route_trace=(RouteTraceEntry(stage="route", summary="stub", details={}),),
            warnings=(),
        )
    )
    fulltext = StubFullTextRetriever(
        FullTextSearchResult(
            request=FullTextSearchRequest(query="sensor open"),
            matches=(
                FullTextMatchResult(
                    logical_object_id="doc:sensor",
                    physical_object_id="chunk:sensor",
                    object_type="chunk",
                    primary_index="doc_fts",
                    matched_indexes=("doc_fts",),
                    source_index="baseline",
                    score=0.85,
                    match_reason="matched sensor api",
                    relative_path=file_record.relative_path,
                    title="sensor_open",
                    snippet="fallback retriever snippet",
                    file_id=file_record.file_id,
                    chunk_id=chunk_record.chunk_id,
                    entity_id=None,
                    profile_id="watch",
                    source_scope="api",
                    domain="engineering",
                    doc_type="api",
                    metadata={"start_line": 12, "end_line": 24},
                ),
            ),
        )
    )
    service = QueryService(
        config,
        router=router,
        metadata_adapter=StubMetadataAdapter(reader),
        fulltext_retriever=fulltext,
    )

    result = service.search(QueryRequest(query="sensor open", profile_id="watch"))
    entity_refs = service.bundle_evidence_for_entity(
        "chunk:sensor",
        snapshot_id="current",
        profile_id="watch",
    )

    assert result.evidence_refs[0].excerpt == "stored excerpt from evidence catalog"
    assert result.evidence_refs[0].path == "knowledge-sources/api/sensor.md"
    assert entity_refs[0].excerpt == "stored excerpt from evidence catalog"


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
        else:
            merged[key] = value
    return merged