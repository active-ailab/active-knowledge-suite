from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing import CURRENT_SNAPSHOT_ID
from active_knowledge_server.query import GraphRetriever, GraphSearchRequest
from active_knowledge_server.storage import (
    ALL_SCOPE,
    EntityRecord,
    FileRecord,
    QueryScope,
    RelationRecord,
    StorageWriteRequest,
    TombstoneRecord,
)
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
)


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


def build_adapter(config: ActiveKnowledgeConfig) -> SQLiteStorageAdapter:
    baseline_path = Path(config.storage.metadata.path)
    overlay_path = Path(config.storage.overlay.path)
    jobs_path = Path(config.storage.jobs.path)
    migrate_sqlite_store(baseline_path, target="baseline_metadata")
    migrate_sqlite_store(overlay_path, target="overlay_metadata")
    migrate_sqlite_store(jobs_path, target="jobs")
    return SQLiteStorageAdapter(
        baseline_metadata_path=baseline_path,
        overlay_metadata_path=overlay_path,
        jobs_path=jobs_path,
    )


def test_graph_retriever_expands_live_edges_and_workspace_context(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    seed_workspace_file(config.project.workspace_root, "packages/apps/Payments/src/payment_view.py")
    seed_graph_fixture(adapter)

    retriever = GraphRetriever.from_config(config, metadata_adapter=adapter)
    shallow = retriever.search(
        GraphSearchRequest(
            seed_entity_ids=("entity:function:render_payment",),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
            relation_types=("contains", "defines", "belongs_to_layer", "implements_feature"),
            max_depth=1,
        )
    )

    assert shallow.warnings == ()
    assert {node.node_id for node in shallow.nodes} == {
        "entity:file:payment_view",
        "entity:function:render_payment",
        "entity:module:payments_ui",
        "feature:payments",
        "layer:app",
    }
    assert {edge.relation_type for edge in shallow.relations} == {
        "contains",
        "defines",
        "belongs_to_layer",
        "implements_feature",
    }
    assert all(
        edge.synthetic
        for edge in shallow.relations
        if edge.relation_type in {"belongs_to_layer", "implements_feature"}
    )
    assert "entity:directory:payments" not in {node.node_id for node in shallow.nodes}

    deep = retriever.search(
        GraphSearchRequest(
            seed_entity_ids=("entity:function:render_payment",),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
            relation_types=("contains", "defines", "belongs_to_layer", "implements_feature"),
            max_depth=2,
        )
    )
    assert "entity:directory:payments" in {node.node_id for node in deep.nodes}


def test_graph_retriever_respects_profile_scope_for_live_relations(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    seed_workspace_file(config.project.workspace_root, "profiles/profile_gate.c")
    seed_profile_fixture(adapter)

    retriever = GraphRetriever.from_config(config, metadata_adapter=adapter)
    watch = retriever.search(
        GraphSearchRequest(
            seed_entity_ids=("entity:function:profile_gate",),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="watch"),
            relation_types=("guarded_by_macro",),
            max_depth=1,
        )
    )
    sensorhub = retriever.search(
        GraphSearchRequest(
            seed_entity_ids=("entity:function:profile_gate",),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID, profile_id="sensorhub"),
            relation_types=("guarded_by_macro",),
            max_depth=1,
        )
    )

    assert {node.node_id for node in watch.nodes} == {
        "entity:function:profile_gate",
        "entity:macro:watch_gate",
    }
    assert [(edge.relation_type, edge.profile_id) for edge in watch.relations] == [
        ("guarded_by_macro", "watch")
    ]
    assert {node.node_id for node in sensorhub.nodes} == {
        "entity:function:profile_gate",
        "entity:macro:sensorhub_gate",
    }
    assert [(edge.relation_type, edge.profile_id) for edge in sensorhub.relations] == [
        ("guarded_by_macro", "sensorhub")
    ]


def test_graph_retriever_omits_tombstoned_neighbors(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    seed_workspace_file(config.project.workspace_root, "profiles/calls.c")
    seed_tombstone_fixture(adapter)

    retriever = GraphRetriever.from_config(config, metadata_adapter=adapter)
    before = retriever.search(
        GraphSearchRequest(
            seed_entity_ids=("entity:function:caller",),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
            relation_types=("calls",),
            max_depth=1,
        )
    )
    assert {node.node_id for node in before.nodes} == {
        "entity:function:caller",
        "entity:function:callee",
    }
    assert [edge.relation_type for edge in before.relations] == ["calls"]

    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    writer.upsert_tombstone(
        TombstoneRecord(
            tombstone_id="ts-callee",
            object_type="entity",
            object_id="entity:function:callee",
            reason="removed",
            created_by_job="job-delete-callee",
            snapshot_id=CURRENT_SNAPSHOT_ID,
        )
    )
    writer.flush()

    after = retriever.search(
        GraphSearchRequest(
            seed_entity_ids=("entity:function:caller",),
            scope=QueryScope(snapshot_id=CURRENT_SNAPSHOT_ID),
            relation_types=("calls",),
            max_depth=1,
        )
    )
    assert {node.node_id for node in after.nodes} == {"entity:function:caller"}
    assert after.relations == ()


def seed_graph_fixture(adapter: SQLiteStorageAdapter) -> None:
    writer = adapter.writer(StorageWriteRequest(target="baseline"))
    records = (
        FileRecord(
            file_id="file:payment_view",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source_id="workspace",
            relative_path="packages/apps/Payments/src/payment_view.py",
            content_hash="hash:payment-view",
            source_scope="workspace",
            language="python",
        ),
        EntityRecord(
            entity_id="entity:directory:payments",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:payment_view",
            entity_type="Directory",
            name="Payments",
            qualified_name="packages/apps/Payments",
            path="packages/apps/Payments",
            source_scope="workspace",
        ),
        EntityRecord(
            entity_id="entity:file:payment_view",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:payment_view",
            entity_type="File",
            name="payment_view.py",
            qualified_name="packages/apps/Payments/src/payment_view.py",
            path="packages/apps/Payments/src/payment_view.py",
            source_scope="workspace",
        ),
        EntityRecord(
            entity_id="entity:module:payments_ui",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:payment_view",
            entity_type="Module",
            name="payments.ui",
            qualified_name="payments.ui",
            path="packages/apps/Payments",
            source_scope="workspace",
            metadata={"module_path": "packages/apps/Payments"},
        ),
        EntityRecord(
            entity_id="entity:function:render_payment",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:payment_view",
            entity_type="Function",
            name="render_payment",
            qualified_name="packages/apps/Payments/src/payment_view.py::render_payment",
            path="packages/apps/Payments/src/payment_view.py#render_payment",
            source_scope="workspace",
            start_line=1,
            end_line=12,
        ),
        RelationRecord(
            relation_id="rel:directory_contains_file",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            relation_type="contains",
            src_entity_id="entity:directory:payments",
            dst_entity_id="entity:file:payment_view",
            source_scope="workspace",
        ),
        RelationRecord(
            relation_id="rel:file_contains_function",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            relation_type="contains",
            src_entity_id="entity:file:payment_view",
            dst_entity_id="entity:function:render_payment",
            source_scope="workspace",
        ),
        RelationRecord(
            relation_id="rel:module_defines_function",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            relation_type="defines",
            src_entity_id="entity:module:payments_ui",
            dst_entity_id="entity:function:render_payment",
            source_scope="workspace",
        ),
    )
    for record in records:
        if isinstance(record, FileRecord):
            writer.upsert_file(record)
        elif isinstance(record, EntityRecord):
            writer.upsert_entity(record)
        else:
            writer.upsert_relation(record)
    writer.flush()


def seed_profile_fixture(adapter: SQLiteStorageAdapter) -> None:
    baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
    overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))
    baseline_writer.upsert_file(
        FileRecord(
            file_id="file:profile_gate",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source_id="workspace",
            relative_path="profiles/profile_gate.c",
            content_hash="hash:profile-gate",
            source_scope="workspace",
            language="c",
        )
    )
    baseline_writer.upsert_entity(
        EntityRecord(
            entity_id="entity:function:profile_gate",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:profile_gate",
            entity_type="Function",
            name="profile_gate",
            qualified_name="profiles/profile_gate.c::profile_gate",
            path="profiles/profile_gate.c#profile_gate",
            source_scope="workspace",
            profile_id=ALL_SCOPE,
        )
    )
    baseline_writer.flush()

    overlay_records = (
        FileRecord(
            file_id="file:watch_gate",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source_id="workspace",
            relative_path="profiles/profile_watch.h",
            content_hash="hash:watch-gate",
            source_scope="workspace",
            profile_id="watch",
            language="c-header",
        ),
        FileRecord(
            file_id="file:sensorhub_gate",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source_id="workspace",
            relative_path="profiles/profile_sensorhub.h",
            content_hash="hash:sensorhub-gate",
            source_scope="workspace",
            profile_id="sensorhub",
            language="c-header",
        ),
        EntityRecord(
            entity_id="entity:macro:watch_gate",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:watch_gate",
            entity_type="Macro",
            name="WATCH_GATE",
            qualified_name="profiles/profile_watch.h::WATCH_GATE",
            path="profiles/profile_watch.h#WATCH_GATE",
            source_scope="workspace",
            profile_id="watch",
        ),
        EntityRecord(
            entity_id="entity:macro:sensorhub_gate",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:sensorhub_gate",
            entity_type="Macro",
            name="SENSORHUB_GATE",
            qualified_name="profiles/profile_sensorhub.h::SENSORHUB_GATE",
            path="profiles/profile_sensorhub.h#SENSORHUB_GATE",
            source_scope="workspace",
            profile_id="sensorhub",
        ),
        RelationRecord(
            relation_id="rel:watch_guard",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            relation_type="guarded_by_macro",
            src_entity_id="entity:function:profile_gate",
            dst_entity_id="entity:macro:watch_gate",
            source_scope="workspace",
            profile_id="watch",
        ),
        RelationRecord(
            relation_id="rel:sensorhub_guard",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            relation_type="guarded_by_macro",
            src_entity_id="entity:function:profile_gate",
            dst_entity_id="entity:macro:sensorhub_gate",
            source_scope="workspace",
            profile_id="sensorhub",
        ),
    )
    for record in overlay_records:
        if isinstance(record, FileRecord):
            overlay_writer.upsert_file(record)
        elif isinstance(record, EntityRecord):
            overlay_writer.upsert_entity(record)
        else:
            overlay_writer.upsert_relation(record)
    overlay_writer.flush()


def seed_tombstone_fixture(adapter: SQLiteStorageAdapter) -> None:
    writer = adapter.writer(StorageWriteRequest(target="baseline"))
    records = (
        FileRecord(
            file_id="file:calls",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            source_id="workspace",
            relative_path="profiles/calls.c",
            content_hash="hash:calls",
            source_scope="workspace",
            language="c",
        ),
        EntityRecord(
            entity_id="entity:function:caller",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:calls",
            entity_type="Function",
            name="caller",
            qualified_name="profiles/calls.c::caller",
            path="profiles/calls.c#caller",
            source_scope="workspace",
        ),
        EntityRecord(
            entity_id="entity:function:callee",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            file_id="file:calls",
            entity_type="Function",
            name="callee",
            qualified_name="profiles/calls.c::callee",
            path="profiles/calls.c#callee",
            source_scope="workspace",
        ),
        RelationRecord(
            relation_id="rel:caller_calls_callee",
            snapshot_id=CURRENT_SNAPSHOT_ID,
            relation_type="calls",
            src_entity_id="entity:function:caller",
            dst_entity_id="entity:function:callee",
            source_scope="workspace",
        ),
    )
    for record in records:
        if isinstance(record, FileRecord):
            writer.upsert_file(record)
        elif isinstance(record, EntityRecord):
            writer.upsert_entity(record)
        else:
            writer.upsert_relation(record)
    writer.flush()


def seed_workspace_file(workspace_root: str, relative_path: str) -> None:
    target = Path(workspace_root) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("placeholder\n", encoding="utf-8")


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
            continue
        merged[key] = value
    return merged