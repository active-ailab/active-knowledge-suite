from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.responses import Warning
from active_knowledge_server.models.routing import RouteTraceEntry, RouterDecision, ToolPlan
from active_knowledge_server.query import QueryService
from active_knowledge_server.storage import EntityRecord, LogicalEntity, ProfileRecord, QueryScope, RelationRecord
from active_knowledge_server.query.retrievers import (
    FullTextMatchResult,
    FullTextSearchRequest,
    FullTextSearchResult,
    GraphNodeResult,
    GraphRelationResult,
    GraphSearchRequest,
    GraphSearchResult,
    SymbolCandidate,
    SymbolSearchRequest,
    SymbolSearchResult,
    VectorMatchResult,
    VectorSearchRequest,
    VectorSearchResult,
)


@dataclass
class StubRouter:
    decision: RouterDecision
    requests: list[QueryRequest] | None = None

    def route(self, request: QueryRequest) -> RouterDecision:
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        return self.decision


@dataclass
class StubSymbolRetriever:
    result: SymbolSearchResult
    requests: list[SymbolSearchRequest] | None = None

    def search(self, request: SymbolSearchRequest) -> SymbolSearchResult:
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        return self.result


@dataclass
class StubFullTextRetriever:
    result: FullTextSearchResult
    requests: list[FullTextSearchRequest] | None = None

    def search(self, request: FullTextSearchRequest) -> FullTextSearchResult:
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        return self.result


@dataclass
class StubVectorRetriever:
    result: VectorSearchResult
    requests: list[VectorSearchRequest] | None = None

    def search(self, request: VectorSearchRequest) -> VectorSearchResult:
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        return self.result


@dataclass
class StubGraphRetriever:
    result: GraphSearchResult
    requests: list[GraphSearchRequest] | None = None

    def search(self, request: GraphSearchRequest) -> GraphSearchResult:
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        return self.result


@dataclass
class StubProfileAwareReader:
    profiles: tuple[ProfileRecord, ...]
    entities: tuple[LogicalEntity, ...]
    relations: tuple[RelationRecord, ...]

    def iter_profiles(self, snapshot_id: str | None = None) -> tuple[ProfileRecord, ...]:
        if snapshot_id is None:
            return self.profiles
        return tuple(profile for profile in self.profiles if profile.snapshot_id == snapshot_id)

    def logical_entities(self, scope: QueryScope) -> tuple[LogicalEntity, ...]:
        return tuple(entity for entity in self.entities if entity.record.snapshot_id == scope.snapshot_id)

    def iter_relations(self, scope: QueryScope) -> tuple[RelationRecord, ...]:
        return tuple(relation for relation in self.relations if relation.snapshot_id == scope.snapshot_id)


@dataclass
class StubMetadataAdapter:
    stub_reader: StubProfileAwareReader

    def reader(self) -> StubProfileAwareReader:
        return self.stub_reader


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


def query_warning(
    code: str,
    *,
    level: str = "caution",
    message: str | None = None,
    details: dict[str, object] | None = None,
    suggested_action: str = "Refine the query and retry.",
) -> Warning:
    return Warning(
        level=level,
        code=code,
        message=message or code,
        details={} if details is None else dict(details),
        actionable=True,
        suggested_action=suggested_action,
    )


def test_query_service_fuses_weighted_results_and_emits_retrieval_trace(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        make_decision(
            intent="code_exact",
            weights={"symbol": 0.55, "fts": 0.30, "vector": 0.05, "graph": 0.10},
            selected_view="code",
            selected_granularity="symbol",
            resolved_profile_id="watch",
        )
    )
    symbol = StubSymbolRetriever(
        SymbolSearchResult(
            request=SymbolSearchRequest(query="alpha"),
            candidates=(
                SymbolCandidate(
                    logical_entity_id="entity:alpha",
                    physical_entity_id="entity:alpha",
                    source_index="baseline",
                    entity_type="Function",
                    name="alpha",
                    qualified_name="src/alpha.c::alpha",
                    path="src/alpha.c#alpha",
                    relative_path="src/alpha.c",
                    file_id="file-alpha",
                    profile_id="watch",
                    source_scope="workspace",
                    start_line=10,
                    end_line=18,
                    score=0.96,
                    match_kinds=("exact",),
                    match_reason="symbol exact",
                    disambiguation_key="Function|src/alpha.c",
                ),
            ),
            total_candidates=1,
        )
    )
    fulltext = StubFullTextRetriever(
        FullTextSearchResult(
            request=FullTextSearchRequest(query="alpha"),
            matches=(
                FullTextMatchResult(
                    logical_object_id="entity:beta",
                    physical_object_id="entity:beta",
                    object_type="entity",
                    primary_index="entity_fts",
                    matched_indexes=("entity_fts",),
                    source_index="baseline",
                    score=0.94,
                    match_reason="fts beta",
                    relative_path="src/beta.c",
                    title="beta",
                    snippet="beta symbol",
                    file_id="file-beta",
                    chunk_id=None,
                    entity_id="entity:beta",
                    profile_id="watch",
                    source_scope="workspace",
                    domain=None,
                    doc_type=None,
                    metadata={"start_line": 4, "end_line": 8},
                ),
                FullTextMatchResult(
                    logical_object_id="entity:alpha",
                    physical_object_id="entity:alpha",
                    object_type="entity",
                    primary_index="entity_fts",
                    matched_indexes=("entity_fts",),
                    source_index="baseline",
                    score=0.80,
                    match_reason="fts alpha",
                    relative_path="src/alpha.c",
                    title="alpha",
                    snippet="alpha fallback",
                    file_id="file-alpha",
                    chunk_id=None,
                    entity_id="entity:alpha",
                    profile_id="watch",
                    source_scope="workspace",
                    domain=None,
                    doc_type=None,
                    metadata={"start_line": 10, "end_line": 18},
                ),
            ),
        )
    )
    vector = StubVectorRetriever(
        VectorSearchResult(
            request=VectorSearchRequest(query="alpha"),
            matches=(
                VectorMatchResult(
                    logical_object_id="entity:gamma",
                    physical_object_id="entity:gamma",
                    vector_ref_id="vec-gamma",
                    object_type="entity",
                    source_index="baseline",
                    score=0.88,
                    match_reason="vector gamma",
                    file_id="file-gamma",
                    relative_path="src/gamma.c",
                    title="gamma",
                    snippet="gamma semantic",
                    chunk_id=None,
                    entity_id="entity:gamma",
                    evidence_id=None,
                    profile_id="watch",
                    source_scope="workspace",
                    domain=None,
                    doc_type=None,
                    embedding_model_version="bge-m3",
                    content_hash="hash-gamma",
                ),
            ),
        )
    )
    graph = StubGraphRetriever(
        GraphSearchResult(
            request=GraphSearchRequest(seed_entity_ids=("entity:alpha",)),
            nodes=(
                GraphNodeResult(
                    node_id="entity:alpha",
                    node_type="entity",
                    name="alpha",
                    depth=0,
                    relative_path="src/alpha.c",
                    entity_type="Function",
                    profile_id="watch",
                    metadata={"start_line": 10, "end_line": 18},
                ),
            ),
            relations=(
                GraphRelationResult(
                    relation_id="rel-alpha-beta",
                    relation_type="calls",
                    src_node_id="entity:alpha",
                    dst_node_id="entity:beta",
                    depth=1,
                    source_index="baseline",
                    profile_id="watch",
                ),
            ),
            total_nodes=1,
            total_relations=1,
        )
    )

    service = QueryService(
        config,
        router=router,
        symbol_retriever=symbol,
        fulltext_retriever=fulltext,
        vector_retriever=vector,
        graph_retriever=graph,
    )

    result = service.search(QueryRequest(query="alpha", profile_id="watch"))

    assert result.result_status == "ok"
    assert result.items[0]["candidate_id"] == "entity:alpha"
    assert set(result.items[0]["retrieval_sources"]) == {"symbol", "fts", "graph"}
    assert graph.requests is not None
    assert graph.requests[0].seed_entity_ids == ("entity:alpha",)
    trace = result.diagnostics["retrieval_trace"]
    assert trace["fusion_strategy"]["name"] == "weighted_rrf"
    assert trace["fusion_strategy"]["weights"]["symbol"] == 0.55
    assert result.relations[0]["relation_type"] == "calls"


def test_query_service_dedupes_evidence_and_reranks_authoritative_doc(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        make_decision(
            intent="api_lookup",
            weights={"symbol": 0.10, "fts": 0.50, "vector": 0.30, "graph": 0.10},
            selected_view="evidence",
            selected_granularity="doc_section",
            resolved_profile_id="watch",
        )
    )
    fulltext = StubFullTextRetriever(
        FullTextSearchResult(
            request=FullTextSearchRequest(query="sensor open"),
            matches=(
                FullTextMatchResult(
                    logical_object_id="doc:authoritative",
                    physical_object_id="chunk:authoritative",
                    object_type="chunk",
                    primary_index="doc_fts",
                    matched_indexes=("doc_fts",),
                    source_index="baseline",
                    score=0.84,
                    match_reason="fts authoritative",
                    relative_path="knowledge-sources/api/sensor.md",
                    title="sensor_open",
                    snippet="authoritative sensor API docs",
                    file_id="file-sensor",
                    chunk_id="chunk-authoritative",
                    entity_id=None,
                    profile_id="watch",
                    source_scope="api",
                    domain="engineering",
                    doc_type="api",
                    metadata={
                        "authority_level": "source_doc",
                        "freshness_ts": "2026-05-20T00:00:00Z",
                        "start_line": 12,
                        "end_line": 24,
                    },
                ),
                FullTextMatchResult(
                    logical_object_id="doc:derived",
                    physical_object_id="chunk:derived",
                    object_type="chunk",
                    primary_index="doc_fts",
                    matched_indexes=("doc_fts",),
                    source_index="baseline",
                    score=0.89,
                    match_reason="fts derived",
                    relative_path="knowledge-sources/api/notes.md",
                    title="sensor_open notes",
                    snippet="derived older notes",
                    file_id="file-notes",
                    chunk_id="chunk-derived",
                    entity_id=None,
                    profile_id="all",
                    source_scope="api",
                    domain="engineering",
                    doc_type="api",
                    metadata={
                        "authority_level": "derived",
                        "freshness_ts": "2024-01-01T00:00:00Z",
                        "start_line": 12,
                        "end_line": 24,
                    },
                ),
            ),
        )
    )
    vector = StubVectorRetriever(
        VectorSearchResult(
            request=VectorSearchRequest(query="sensor open"),
            matches=(
                VectorMatchResult(
                    logical_object_id="doc:authoritative",
                    physical_object_id="chunk:authoritative",
                    vector_ref_id="vec-authoritative",
                    object_type="chunk",
                    source_index="baseline",
                    score=0.82,
                    match_reason="vector authoritative",
                    file_id="file-sensor",
                    relative_path="knowledge-sources/api/sensor.md",
                    title="sensor_open",
                    snippet="authoritative semantic hit",
                    chunk_id="chunk-authoritative",
                    entity_id=None,
                    evidence_id=None,
                    profile_id="watch",
                    source_scope="api",
                    domain="engineering",
                    doc_type="api",
                    embedding_model_version="bge-m3",
                    content_hash="hash-authoritative",
                    metadata={
                        "authority_level": "source_doc",
                        "freshness_ts": "2026-05-20T00:00:00Z",
                        "start_line": 12,
                        "end_line": 24,
                    },
                ),
            ),
        )
    )

    service = QueryService(
        config,
        router=router,
        fulltext_retriever=fulltext,
        vector_retriever=vector,
    )

    result = service.search(QueryRequest(query="sensor open", profile_id="watch"))

    assert result.items[0]["candidate_id"] == "doc:authoritative"
    assert len(result.evidence_refs) == 2
    authoritative_trace = [
        item
        for item in result.diagnostics["retrieval_trace"]["evidence_trace"]
        if item["path"] == "knowledge-sources/api/sensor.md"
    ]
    assert authoritative_trace[0]["retrieval_sources"] == ["fts", "vector"]
    assert result.items[0]["authority_level"] == "source_doc"


def test_query_service_returns_zero_result_contract(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        make_decision(
            intent="unknown",
            weights={"symbol": 0.15, "fts": 0.40, "vector": 0.20, "graph": 0.25},
            selected_view="evidence",
            selected_granularity="doc_section",
            resolved_profile_id="all",
            confidence=0.30,
        )
    )
    service = QueryService(config, router=router)

    result = service.search(QueryRequest(query="帮我看看这里。"))

    assert result.result_status == "zero_result"
    assert result.items == ()
    assert result.evidence_refs == ()
    assert any(warning.code == "retrieval.zero_result" for warning in result.warnings)
    assert result.next_queries


def test_query_service_returns_low_confidence_contract_when_router_is_uncertain(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        RouterDecision(
            normalized_query="sensor open guide",
            intent="unknown",
            confidence=0.32,
            selected_view="evidence",
            selected_granularity="doc_section",
            profile_resolution={
                "status": "resolved",
                "resolved_profile_id": "watch",
                "warnings": [],
            },
            warnings=(
                query_warning(
                    "router.low_confidence",
                    message="Router could not classify the query with high confidence.",
                    details={"top_intent": "api_lookup", "top_score": 0.32},
                    suggested_action="Add a module, symbol, doc_type, or profile_id and retry.",
                ),
            ),
            retriever_weights={"symbol": 0.0, "fts": 1.0, "vector": 0.0, "graph": 0.0},
            tool_plan=ToolPlan(route_mode="explore", primary_tool="kb_search"),
            route_trace=(RouteTraceEntry(stage="route", summary="stub", details={}),),
        )
    )
    fulltext = StubFullTextRetriever(
        FullTextSearchResult(
            request=FullTextSearchRequest(query="sensor open guide"),
            matches=(
                FullTextMatchResult(
                    logical_object_id="doc:sensor-open",
                    physical_object_id="chunk:sensor-open",
                    object_type="chunk",
                    primary_index="doc_fts",
                    matched_indexes=("doc_fts",),
                    source_index="baseline",
                    score=0.78,
                    match_reason="matched API guide",
                    relative_path="knowledge-sources/api/sensor.md",
                    title="sensor_open",
                    snippet="sensor_open API quick reference",
                    file_id="file-sensor",
                    chunk_id="chunk-sensor-open",
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
        fulltext_retriever=fulltext,
    )

    result = service.search(QueryRequest(query="sensor open guide", profile_id="watch"))

    assert result.result_status == "low_confidence"
    assert result.confidence < 0.50
    assert result.evidence_refs
    assert any(warning.code == "router.low_confidence" for warning in result.warnings)
    assert "verify the attached evidence" in result.summary


def test_query_service_returns_partial_ready_when_index_is_degraded(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        make_decision(
            intent="api_lookup",
            weights={"symbol": 0.0, "fts": 0.70, "vector": 0.30, "graph": 0.0},
            selected_view="evidence",
            selected_granularity="doc_section",
            resolved_profile_id="watch",
            confidence=0.74,
        )
    )
    fulltext = StubFullTextRetriever(
        FullTextSearchResult(
            request=FullTextSearchRequest(query="sensor open"),
            matches=(
                FullTextMatchResult(
                    logical_object_id="doc:sensor-open",
                    physical_object_id="chunk:sensor-open",
                    object_type="chunk",
                    primary_index="doc_fts",
                    matched_indexes=("doc_fts",),
                    source_index="baseline",
                    score=0.86,
                    match_reason="matched API guide",
                    relative_path="knowledge-sources/api/sensor.md",
                    title="sensor_open",
                    snippet="sensor_open API quick reference",
                    file_id="file-sensor",
                    chunk_id="chunk-sensor-open",
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
    vector = StubVectorRetriever(
        VectorSearchResult(
            request=VectorSearchRequest(query="sensor open"),
            warnings=(
                query_warning(
                    "index.partial_ready",
                    level="degraded",
                    message="Widget and profile indexes are not ready yet.",
                    details={
                        "ready_sources": ["knowledge-sources/api"],
                        "missing_sources": ["knowledge-sources/widgets"],
                        "failed_jobs": ["job-17"],
                        "degradation_chain": ["skip_widgets", "fts_only"],
                    },
                    suggested_action="Repair the failed job and rebuild the missing sources.",
                ),
            ),
        )
    )
    service = QueryService(
        config,
        router=router,
        fulltext_retriever=fulltext,
        vector_retriever=vector,
    )

    result = service.search(QueryRequest(query="sensor open", profile_id="watch"))

    assert result.result_status == "partial_ready"
    assert result.items
    assert any(warning.code == "index.partial_ready" for warning in result.warnings)
    assert result.diagnostics["index_status"]["missing_sources"] == ["knowledge-sources/widgets"]


def test_query_service_returns_profile_candidates_for_unresolved_profile_sensitive_query(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        RouterDecision(
            normalized_query="CONFIG_BT impact",
            intent="profile_diff",
            confidence=0.88,
            selected_view="profile",
            selected_granularity="profile",
            profile_resolution={
                "status": "multiple_candidates",
                "resolved_profile_id": None,
                "candidates": [
                    {
                        "profile_id": "mhs003_watch",
                        "profile_record_id": "profile:watch",
                        "dotconfig_path": "build/.config",
                        "app": "watch",
                        "confidence": 0.93,
                    },
                    {
                        "profile_id": "mhs003_sensorhub",
                        "profile_record_id": "profile:sensorhub",
                        "dotconfig_path": "build/out_hub/.config",
                        "app": "sensorhub",
                        "confidence": 0.91,
                    },
                ],
                "warnings": [
                    {
                        "level": "caution",
                        "code": "profile.multiple_candidates",
                        "message": "Multiple profile candidates were found; no profile was selected automatically.",
                        "details": {"candidate_count": 2},
                    }
                ],
            },
            retriever_weights={"symbol": 0.15, "fts": 0.30, "graph": 0.55},
            tool_plan=ToolPlan(route_mode="chain", primary_tool="config_impact"),
            route_trace=(RouteTraceEntry(stage="route", summary="stub", details={}),),
            warnings=(),
        )
    )
    service = QueryService(config, router=router)

    result = service.search(QueryRequest(query="CONFIG_BT 影响哪些模块？"))

    assert result.result_status == "multi_result"
    assert result.profile_id == "unresolved"
    assert [candidate.profile_id for candidate in result.candidates] == [
        "mhs003_watch",
        "mhs003_sensorhub",
    ]
    assert any(warning.code == "profile.multiple_candidates" for warning in result.warnings)
    assert result.suggested_filters[0].field == "profile_id"


def test_query_service_returns_ambiguous_for_invalid_compare_to_profile(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        RouterDecision(
            normalized_query="CONFIG_HEALTH_BT diff",
            intent="profile_diff",
            confidence=0.83,
            selected_view="profile",
            selected_granularity="profile",
            profile_resolution={
                "status": "resolved",
                "resolved_profile_id": "mhs003_watch",
                "warnings": [],
            },
            retriever_weights={"symbol": 0.15, "fts": 0.30, "graph": 0.55},
            tool_plan=ToolPlan(
                route_mode="chain",
                primary_tool="config_impact",
                primary_args={
                    "macro_or_config": "CONFIG_HEALTH_BT",
                    "profile_id": "mhs003_watch",
                    "compare_to": "missing_profile",
                },
            ),
            route_trace=(RouteTraceEntry(stage="route", summary="stub", details={}),),
            warnings=(),
        )
    )
    reader = StubProfileAwareReader(
        profiles=(
            ProfileRecord(
                profile_record_id="profile:watch",
                snapshot_id="current",
                profile_id="mhs003_watch",
                defconfig_path="configs/mhs003_watch_defconfig",
                dotconfig_path="build/.config",
                metadata={
                    "macro_assignments": {
                        "CONFIG_HEALTH_BT": {
                            "value": "y",
                            "enabled": True,
                            "value_type": "bool",
                        }
                    }
                },
            ),
        ),
        entities=(),
        relations=(),
    )
    service = QueryService(
        config,
        router=router,
        metadata_adapter=StubMetadataAdapter(reader),
    )

    result = service.search(
        QueryRequest(
            query="CONFIG_HEALTH_BT 在 watch 和 missing_profile 的差异是什么？",
            profile_id="mhs003_watch",
            client_context={"compare_to": "missing_profile"},
        )
    )

    assert result.result_status == "ambiguous"
    assert result.diagnostics["required_context"] == ["compare_to"]
    assert any(warning.code == "profile.invalid" for warning in result.warnings)
    assert result.next_queries


def test_query_service_returns_profile_matrix_for_compare_to_queries(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    router = StubRouter(
        RouterDecision(
            normalized_query="CONFIG_HEALTH_BT diff",
            intent="profile_diff",
            confidence=0.89,
            selected_view="profile",
            selected_granularity="profile",
            profile_resolution={
                "status": "resolved",
                "resolved_profile_id": "mhs003_watch",
                "warnings": [],
            },
            retriever_weights={"symbol": 0.15, "fts": 0.30, "graph": 0.55},
            tool_plan=ToolPlan(
                route_mode="chain",
                primary_tool="config_impact",
                primary_args={
                    "macro_or_config": "CONFIG_HEALTH_BT",
                    "profile_id": "mhs003_watch",
                    "compare_to": "mhs003_sensorhub",
                },
            ),
            route_trace=(RouteTraceEntry(stage="route", summary="stub", details={}),),
            warnings=(),
        )
    )
    reader = StubProfileAwareReader(
        profiles=(
            ProfileRecord(
                profile_record_id="profile:watch",
                snapshot_id="current",
                profile_id="mhs003_watch",
                defconfig_path="configs/mhs003_watch_defconfig",
                dotconfig_path="build/.config",
                metadata={
                    "macro_assignments": {
                        "CONFIG_HEALTH_BT": {
                            "value": "y",
                            "enabled": True,
                            "value_type": "bool",
                        }
                    }
                },
            ),
            ProfileRecord(
                profile_record_id="profile:sensorhub",
                snapshot_id="current",
                profile_id="mhs003_sensorhub",
                defconfig_path="configs/mhs003_sensorhub_defconfig",
                dotconfig_path="build/out_hub/.config",
                metadata={
                    "macro_assignments": {
                        "CONFIG_HEALTH_BT": {
                            "value": "n",
                            "enabled": False,
                            "value_type": "bool",
                        }
                    }
                },
            ),
        ),
        entities=(
            LogicalEntity(
                logical_object_id="entity:module:health_core",
                physical_object_id="entity:module:health_core",
                source_index="baseline",
                record=EntityRecord(
                    entity_id="entity:module:health_core",
                    snapshot_id="current",
                    file_id="file:module:health",
                    entity_type="Module",
                    name="health_core",
                    qualified_name="components/health/module.mk::health_core",
                    path="components/health/module.mk::health_core",
                ),
            ),
            LogicalEntity(
                logical_object_id="entity:file:bt.c",
                physical_object_id="entity:file:bt.c",
                source_index="baseline",
                record=EntityRecord(
                    entity_id="entity:file:bt.c",
                    snapshot_id="current",
                    file_id="file:bt",
                    entity_type="File",
                    name="bt.c",
                    qualified_name="components/health/bt.c",
                    path="components/health/bt.c",
                ),
            ),
        ),
        relations=(
            RelationRecord(
                relation_id="rel:watch:module",
                snapshot_id="current",
                relation_type="enabled_by",
                src_entity_id="entity:module:health_core",
                dst_entity_id="entity:macro:CONFIG_HEALTH_BT",
                profile_id="mhs003_watch",
                metadata={
                    "macro_name": "CONFIG_HEALTH_BT",
                    "condition_expr": "CONFIG_HEALTH_BT",
                },
            ),
            RelationRecord(
                relation_id="rel:watch:file",
                snapshot_id="current",
                relation_type="enabled_by",
                src_entity_id="entity:file:bt.c",
                dst_entity_id="entity:macro:CONFIG_HEALTH_BT",
                profile_id="mhs003_watch",
                metadata={
                    "macro_name": "CONFIG_HEALTH_BT",
                    "condition_expr": "CONFIG_HEALTH_BT",
                },
            ),
            RelationRecord(
                relation_id="rel:sensorhub:module",
                snapshot_id="current",
                relation_type="disabled_by",
                src_entity_id="entity:module:health_core",
                dst_entity_id="entity:macro:CONFIG_HEALTH_BT",
                profile_id="mhs003_sensorhub",
                metadata={
                    "macro_name": "CONFIG_HEALTH_BT",
                    "condition_expr": "CONFIG_HEALTH_BT",
                },
            ),
        ),
    )
    service = QueryService(
        config,
        router=router,
        metadata_adapter=StubMetadataAdapter(reader),
    )

    result = service.search(
        QueryRequest(
            query="CONFIG_HEALTH_BT 在 watch 和 sensorhub 的差异是什么？",
            profile_id="mhs003_watch",
            client_context={"compare_to": "mhs003_sensorhub"},
        )
    )

    assert result.result_status == "ok"
    assert result.profile_id == "multi"
    items_by_profile = {item["profile_id"]: item for item in result.items}
    assert items_by_profile["mhs003_watch"]["status"] == "enabled"
    assert items_by_profile["mhs003_sensorhub"]["status"] == "disabled"
    assert items_by_profile["mhs003_watch"]["macro_diff"][0]["compare_to"] == "n"
    assert "health_core" in items_by_profile["mhs003_watch"]["affected_modules"]
    assert result.evidence_refs
    assert result.diagnostics["profile_matrix"]["compare_to"] == "mhs003_sensorhub"


def make_decision(
    *,
    intent: str,
    weights: dict[str, float],
    selected_view: str,
    selected_granularity: str,
    resolved_profile_id: str,
    confidence: float = 0.86,
) -> RouterDecision:
    return RouterDecision(
        normalized_query="normalized",
        intent=intent,
        confidence=confidence,
        selected_view=selected_view,
        selected_granularity=selected_granularity,
        profile_resolution={
            "status": "resolved",
            "resolved_profile_id": resolved_profile_id,
            "warnings": [],
        },
        retriever_weights=weights,
        tool_plan=ToolPlan(route_mode="explore", primary_tool="kb_search"),
        route_trace=(
            RouteTraceEntry(stage="route", summary="stub", details={}),
        ),
    )


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