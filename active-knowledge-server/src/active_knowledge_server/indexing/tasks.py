"""Deterministic task lists for resumable incremental indexing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from active_knowledge_server.indexing.code_indexer import (
    CODE_INDEXER_SCHEMA_VERSION,
    count_indexable_workspace_files,
)
from active_knowledge_server.indexing.doc_indexer import DOC_INDEXER_SCHEMA_VERSION
from active_knowledge_server.indexing.embeddings import EMBEDDING_PREPARATION_SCHEMA_VERSION
from active_knowledge_server.indexing.relation_extractor import (
    PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
)
from active_knowledge_server.indexing.workspace_map import WORKSPACE_MAP_SCHEMA_VERSION

INDEX_TASK_SCHEMA_VERSION: Final = "index_task.v1"
INDEX_TASK_LIST_SCHEMA_VERSION: Final = "index_task_list.v1"

IndexTaskOperation = Literal["apply", "delete", "doc", "relations", "map"]


@dataclass(frozen=True)
class IndexTask:
    """One idempotent unit in an incremental index apply plan."""

    task_key: str
    phase: str
    source_kind: str
    operation: IndexTaskOperation
    relative_path: str | None = None
    input_hash: str = ""
    schema_version: str = INDEX_TASK_SCHEMA_VERSION
    embedding_model: str | None = None
    required: bool = True
    collect_dependencies: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-serializable task payload."""

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "task_key": self.task_key,
            "phase": self.phase,
            "source_kind": self.source_kind,
            "operation": self.operation,
            "relative_path": self.relative_path,
            "input_hash": self.input_hash,
            "embedding_model": self.embedding_model,
            "required": self.required,
            "collect_dependencies": list(self.collect_dependencies),
        }
        return payload


def make_index_task_list(plan: object) -> tuple[IndexTask, ...]:
    """Derive the stable resumable task list from an incremental index plan."""

    tasks: list[IndexTask] = []
    source = str(getattr(plan, "source", "all"))
    current_state = plan.current_state
    previous_state = getattr(plan, "previous_state", None)

    if source in {"all", "code"} and _code_work_required(plan):
        code_collect_paths = _incremental_code_paths_to_collect(plan)
        code_collect_dependencies = tuple(f"code:collect:{path}" for path in code_collect_paths)
        for path in _sorted_paths(getattr(plan, "changed_code_paths", ())):
            tasks.append(
                IndexTask(
                    task_key=f"code:apply:{path}",
                    phase="code_apply",
                    source_kind="code",
                    operation="apply",
                    relative_path=path,
                    input_hash=_path_hash(current_state.code_files, path),
                    schema_version=CODE_INDEXER_SCHEMA_VERSION,
                    collect_dependencies=code_collect_dependencies,
                )
            )
        for path in _sorted_paths(getattr(plan, "deleted_code_paths", ())):
            tasks.append(
                IndexTask(
                    task_key=f"code:delete:{path}",
                    phase="code_apply",
                    source_kind="code",
                    operation="delete",
                    relative_path=path,
                    input_hash=_path_hash(
                        getattr(previous_state, "code_files", {}) if previous_state else {},
                        path,
                    ),
                    schema_version=CODE_INDEXER_SCHEMA_VERSION,
                )
            )

    if source in {"all", "docs"} and _doc_work_required(plan):
        doc_collect_paths = _incremental_doc_paths_to_collect(plan)
        doc_collect_dependencies = {path: (f"doc:collect:{path}",) for path in doc_collect_paths}
        doc_apply_paths = tuple(
            path
            for path in doc_collect_paths
            if getattr(plan, "reindex_all_docs", False)
            or path in set(getattr(plan, "changed_doc_paths", ()))
        )
        vector_paths = tuple(
            path
            for path in doc_collect_paths
            if getattr(plan, "rebuild_vectors", False)
            or getattr(plan, "reindex_all_docs", False)
            or path in set(getattr(plan, "changed_doc_paths", ()))
        )
        for path in doc_apply_paths:
            tasks.append(
                IndexTask(
                    task_key=f"doc:apply:{path}",
                    phase="doc_apply",
                    source_kind="doc",
                    operation="apply",
                    relative_path=path,
                    input_hash=_path_hash(current_state.doc_files, path),
                    schema_version=DOC_INDEXER_SCHEMA_VERSION,
                    collect_dependencies=doc_collect_dependencies.get(path, ()),
                )
            )
        for path in _sorted_paths(getattr(plan, "deleted_doc_paths", ())):
            tasks.append(
                IndexTask(
                    task_key=f"doc:delete:{path}",
                    phase="doc_apply",
                    source_kind="doc",
                    operation="delete",
                    relative_path=path,
                    input_hash=_path_hash(
                        getattr(previous_state, "doc_files", {}) if previous_state else {},
                        path,
                    ),
                    schema_version=DOC_INDEXER_SCHEMA_VERSION,
                )
            )
        if getattr(current_state, "embeddings_enabled", True):
            for path in vector_paths:
                tasks.append(
                    IndexTask(
                        task_key=f"vector:doc:{path}",
                        phase="vectors_apply",
                        source_kind="vector",
                        operation="doc",
                        relative_path=path,
                        input_hash=_stable_digest(
                            {
                                "path": path,
                                "doc_hash": _path_hash(current_state.doc_files, path),
                                "embedding_model": getattr(
                                    current_state,
                                    "embedding_model_version",
                                    "",
                                ),
                            }
                        ),
                        schema_version=EMBEDDING_PREPARATION_SCHEMA_VERSION,
                        embedding_model=getattr(
                            current_state,
                            "embedding_model_version",
                            None,
                        ),
                        collect_dependencies=doc_collect_dependencies.get(path, ()),
                    )
                )

    if source in {"all", "code"} and getattr(
        plan,
        "rebuild_profile_conditioned_relations",
        False,
    ):
        tasks.append(
            IndexTask(
                task_key="profile:relations",
                phase="profile_relations",
                source_kind="profile",
                operation="relations",
                input_hash=_stable_digest(
                    {
                        "changed_profile_ids": _sorted_paths(
                            getattr(plan, "changed_profile_ids", ())
                        ),
                        "removed_profile_ids": _sorted_paths(
                            getattr(plan, "removed_profile_ids", ())
                        ),
                        "profile_hashes": dict(
                            sorted(getattr(current_state, "profile_config_hashes", {}).items())
                        ),
                    }
                ),
                schema_version=PROFILE_CONDITIONED_RELATION_SCHEMA_VERSION,
            )
        )

    if _workspace_map_refresh_required(plan):
        tasks.append(
            IndexTask(
                task_key="workspace:map",
                phase="workspace_map",
                source_kind="workspace",
                operation="map",
                input_hash=_stable_digest(
                    {
                        "workspace_inventory_hash": getattr(
                            current_state,
                            "workspace_inventory_hash",
                            "",
                        ),
                        "profile_hashes": dict(
                            sorted(getattr(current_state, "profile_config_hashes", {}).items())
                        ),
                    }
                ),
                schema_version=WORKSPACE_MAP_SCHEMA_VERSION,
            )
        )

    return tuple(sorted(tasks, key=lambda task: task.task_key))


def index_task_list_to_dict(tasks: Sequence[IndexTask]) -> dict[str, object]:
    """Return a JSON-serializable task list payload."""

    return {
        "schema_version": INDEX_TASK_LIST_SCHEMA_VERSION,
        "tasks": [task.to_dict() for task in tasks],
    }


def estimate_progress_total_from_tasks(tasks: Sequence[IndexTask]) -> int:
    """Estimate the incremental global progress total represented by a task list."""

    collect_dependencies = {
        dependency for task in tasks for dependency in task.collect_dependencies
    }
    return 1 + len(collect_dependencies) + len(tasks)


def estimate_progress_totals_from_plan(
    plan: object,
    *,
    source: str | None = None,
) -> dict[str, int]:
    """Mirror the current incremental pipeline progress totals for task planning."""

    task_source = source or str(getattr(plan, "source", "all"))
    doc_paths_to_collect = _incremental_doc_paths_to_collect(plan)
    tasks = make_index_task_list(plan)
    code_collect = 0
    if task_source in {"all", "code"} and _code_work_required(plan):
        code_include_paths = (
            None
            if getattr(plan, "reindex_all_code", False)
            else _incremental_code_paths_to_collect(plan)
        )
        code_collect = count_indexable_workspace_files(
            plan.workspace_inventory,
            include_paths=code_include_paths,
        )
    code_apply = sum(1 for task in tasks if task.phase == "code_apply")
    doc_collect = (
        len(doc_paths_to_collect)
        if task_source in {"all", "docs"} and _doc_work_required(plan)
        else 0
    )
    doc_apply = sum(1 for task in tasks if task.phase == "doc_apply")
    vectors_apply = sum(1 for task in tasks if task.phase == "vectors_apply")
    profile_relations = sum(1 for task in tasks if task.phase == "profile_relations")
    workspace_map = sum(1 for task in tasks if task.phase == "workspace_map")
    global_total = (
        1
        + code_collect
        + code_apply
        + doc_collect
        + doc_apply
        + vectors_apply
        + profile_relations
        + workspace_map
    )
    return {
        "code_collect": code_collect,
        "code_apply": code_apply,
        "doc_collect": doc_collect,
        "doc_apply": doc_apply,
        "vectors_apply": vectors_apply,
        "profile_relations": profile_relations,
        "workspace_map": workspace_map,
        "global_total": global_total,
    }


def _incremental_doc_paths_to_collect(plan: object) -> tuple[str, ...]:
    doc_paths_to_collect = set(getattr(plan, "changed_doc_paths", ()))
    if getattr(plan, "reindex_all_docs", False) or (
        getattr(plan, "rebuild_vectors", False) and not doc_paths_to_collect
    ):
        doc_paths_to_collect.update(getattr(plan.current_state, "doc_files", {}))
    return tuple(sorted(doc_paths_to_collect))


def _incremental_code_paths_to_collect(plan: object) -> tuple[str, ...]:
    if getattr(plan, "reindex_all_code", False):
        return tuple(sorted(getattr(plan.current_state, "code_files", {})))
    if not getattr(plan, "changed_code_paths", ()):
        return ()
    paths = set(getattr(plan, "changed_code_paths", ()))
    paths.update(
        path
        for path in getattr(plan.current_state, "code_files", {})
        if Path(path).name == "Makefile" or Path(path).suffix == ".mk"
    )
    return tuple(sorted(paths))


def _workspace_map_refresh_required(plan: object) -> bool:
    return bool(
        getattr(plan, "source", "all") in {"all", "code"}
        and (
            getattr(plan, "reindex_all_code", False)
            or getattr(plan, "changed_code_paths", ())
            or getattr(plan, "deleted_code_paths", ())
            or getattr(plan, "rebuild_profile_conditioned_relations", False)
            or getattr(plan, "changed_profile_ids", ())
            or getattr(plan, "removed_profile_ids", ())
        )
    )


def _code_work_required(plan: object) -> bool:
    return bool(
        getattr(plan, "changed_code_paths", ())
        or getattr(plan, "deleted_code_paths", ())
        or getattr(plan, "reindex_all_code", False)
    )


def _doc_work_required(plan: object) -> bool:
    return bool(
        getattr(plan, "changed_doc_paths", ())
        or getattr(plan, "deleted_doc_paths", ())
        or getattr(plan, "reindex_all_docs", False)
        or getattr(plan, "rebuild_vectors", False)
    )


def _path_hash(values: Mapping[str, str], path: str) -> str:
    return values.get(path, "")


def _sorted_paths(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(str(path) for path in paths))


def _stable_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
