from __future__ import annotations

from active_knowledge_server.connectors.workspace import (
    FileInventoryEntry,
    WorkspaceInventory,
)
from active_knowledge_server.indexing.pipeline import (
    INCREMENTAL_INDEX_STATE_SCHEMA_VERSION,
    IncrementalIndexPlan,
    IncrementalIndexState,
    _incremental_doc_paths_to_collect,
    _incremental_progress_totals,
)
from active_knowledge_server.indexing.tasks import (
    estimate_progress_total_from_tasks,
    estimate_progress_totals_from_plan,
    index_task_list_to_dict,
    make_index_task_list,
)


def test_task_list_is_stable_and_covers_small_code_doc_change_and_delete() -> None:
    plan = _plan(
        changed_code_paths=("src/app.c",),
        deleted_code_paths=("src/old.c",),
        changed_doc_paths=("guide.md",),
        deleted_doc_paths=("old.md",),
    )

    first = make_index_task_list(plan)
    second = make_index_task_list(plan)

    assert first == second
    assert [task.task_key for task in first] == [
        "code:apply:src/app.c",
        "code:delete:src/old.c",
        "doc:apply:guide.md",
        "doc:delete:old.md",
        "vector:doc:guide.md",
        "workspace:map",
    ]
    assert first[0].collect_dependencies == ("code:collect:src/app.c",)
    assert first[2].collect_dependencies == ("doc:collect:guide.md",)
    assert index_task_list_to_dict(first)["schema_version"] == "index_task_list.v1"
    assert estimate_progress_total_from_tasks(first) == 9
    assert estimate_progress_totals_from_plan(plan) == _pipeline_totals(plan)


def test_full_rebuild_task_list_uses_current_paths_and_matches_progress_total() -> None:
    plan = _plan(
        reindex_all_code=True,
        reindex_all_docs=True,
        current_code_files={
            "Makefile": "make-hash",
            "src/app.c": "app-hash",
            "src/lib.c": "lib-hash",
        },
        current_doc_files={"api/a.md": "api-hash", "guide.md": "guide-hash"},
    )

    tasks = make_index_task_list(plan)

    assert [task.task_key for task in tasks] == [
        "doc:apply:api/a.md",
        "doc:apply:guide.md",
        "vector:doc:api/a.md",
        "vector:doc:guide.md",
        "workspace:map",
    ]
    assert estimate_progress_totals_from_plan(plan) == _pipeline_totals(plan)
    assert estimate_progress_totals_from_plan(plan)["global_total"] == 11


def test_makefile_collect_dependency_is_included_for_small_code_change() -> None:
    plan = _plan(
        changed_code_paths=("src/app.c",),
        current_code_files={"Makefile": "make-hash", "src/app.c": "app-hash"},
    )

    totals = estimate_progress_totals_from_plan(plan)
    tasks = make_index_task_list(plan)

    assert totals == _pipeline_totals(plan)
    assert totals["code_collect"] == 2
    assert tasks[0].collect_dependencies == (
        "code:collect:Makefile",
        "code:collect:src/app.c",
    )
    assert estimate_progress_total_from_tasks(tasks) == totals["global_total"]


def test_vector_rebuild_tasks_cover_all_current_docs() -> None:
    plan = _plan(
        source="docs",
        rebuild_vectors=True,
        current_doc_files={"api/a.md": "api-hash", "guide.md": "guide-hash"},
    )

    tasks = make_index_task_list(plan)

    assert [task.task_key for task in tasks] == [
        "vector:doc:api/a.md",
        "vector:doc:guide.md",
    ]
    assert all(task.schema_version == "embedding_preparation.v1" for task in tasks)
    assert estimate_progress_totals_from_plan(plan) == _pipeline_totals(plan)


def test_profile_change_creates_profile_and_workspace_tasks() -> None:
    plan = _plan(
        changed_profile_ids=("debug",),
        rebuild_profile_conditioned_relations=True,
    )

    tasks = make_index_task_list(plan)

    assert [task.task_key for task in tasks] == ["profile:relations", "workspace:map"]
    assert tasks[0].schema_version == "profile_conditioned_relations.v1"
    assert estimate_progress_totals_from_plan(plan) == _pipeline_totals(plan)


def _pipeline_totals(plan: IncrementalIndexPlan) -> dict[str, int]:
    return _incremental_progress_totals(
        plan,
        plan.source,
        _incremental_doc_paths_to_collect(plan),
    )


def _plan(
    *,
    source: str = "all",
    previous_code_files: dict[str, str] | None = None,
    current_code_files: dict[str, str] | None = None,
    previous_doc_files: dict[str, str] | None = None,
    current_doc_files: dict[str, str] | None = None,
    reindex_all_code: bool = False,
    reindex_all_docs: bool = False,
    rebuild_vectors: bool = False,
    rebuild_profile_conditioned_relations: bool = False,
    changed_code_paths: tuple[str, ...] = (),
    deleted_code_paths: tuple[str, ...] = (),
    changed_doc_paths: tuple[str, ...] = (),
    deleted_doc_paths: tuple[str, ...] = (),
    changed_profile_ids: tuple[str, ...] = (),
    removed_profile_ids: tuple[str, ...] = (),
) -> IncrementalIndexPlan:
    previous_code_files = previous_code_files or {
        "src/app.c": "old-app-hash",
        "src/old.c": "old-hash",
    }
    current_code_files = current_code_files or {"src/app.c": "app-hash"}
    previous_doc_files = previous_doc_files or {
        "guide.md": "old-guide-hash",
        "old.md": "old-doc-hash",
    }
    current_doc_files = current_doc_files or {"guide.md": "guide-hash"}
    previous = _state(
        code_files=previous_code_files,
        doc_files=previous_doc_files,
        workspace_inventory_hash="old-workspace-hash",
        source_docs_manifest_hash="old-docs-hash",
    )
    current = _state(
        code_files=current_code_files,
        doc_files=current_doc_files,
        workspace_inventory_hash="workspace-hash",
        source_docs_manifest_hash="docs-hash",
    )
    return IncrementalIndexPlan(
        snapshot_id="current",
        source=source,  # type: ignore[arg-type]
        previous_state=previous,
        current_state=current,
        workspace_inventory=_workspace_inventory(current_code_files),
        source_docs_manifest=object(),  # type: ignore[arg-type]
        collected_profiles=object(),  # type: ignore[arg-type]
        reindex_all_code=reindex_all_code,
        reindex_all_docs=reindex_all_docs,
        rebuild_vectors=rebuild_vectors,
        rebuild_profile_conditioned_relations=rebuild_profile_conditioned_relations,
        changed_code_paths=changed_code_paths,
        deleted_code_paths=deleted_code_paths,
        changed_doc_paths=changed_doc_paths,
        deleted_doc_paths=deleted_doc_paths,
        changed_profile_ids=changed_profile_ids,
        removed_profile_ids=removed_profile_ids,
    )


def _state(
    *,
    code_files: dict[str, str],
    doc_files: dict[str, str],
    workspace_inventory_hash: str,
    source_docs_manifest_hash: str,
) -> IncrementalIndexState:
    return IncrementalIndexState(
        schema_version=INCREMENTAL_INDEX_STATE_SCHEMA_VERSION,
        snapshot_id="current",
        code_indexer_schema_version="code_indexer.v1",
        doc_indexer_schema_version="doc_indexer.v1",
        profile_collector_schema_version="profile_collector.v1",
        profile_conditioned_relation_schema_version="profile_conditioned_relations.v1",
        embedding_model_version="bge-m3",
        embeddings_enabled=True,
        workspace_inventory_hash=workspace_inventory_hash,
        source_docs_manifest_hash=source_docs_manifest_hash,
        code_files=code_files,
        doc_files=doc_files,
        profile_config_hashes={"default": "profile-hash"},
    )


def _workspace_inventory(files: dict[str, str]) -> WorkspaceInventory:
    return WorkspaceInventory(
        schema_version="workspace_inventory.v1",
        workspace_root="/workspace",
        workspace_display_path="/workspace",
        include=(),
        exclude=(),
        areas=(),
        repositories=(),
        files=tuple(
            FileInventoryEntry(
                relative_path=path,
                display_path=path,
                size_bytes=1,
                content_hash=content_hash,
                repo_relative_path=path,
                area=None,
                language="makefile" if path == "Makefile" or path.endswith(".mk") else "c",
            )
            for path, content_hash in sorted(files.items())
        ),
        inventory_hash="workspace-hash",
    )
