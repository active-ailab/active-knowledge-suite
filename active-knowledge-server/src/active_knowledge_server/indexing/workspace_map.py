"""Workspace map and view projection artifacts built from indexed workspace facts."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.workspace import RepositoryInfo, WorkspaceInventory
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID
from active_knowledge_server.storage import (
    ALL_SCOPE,
    EntityRecord,
    ProfileRecord,
    QueryScope,
    RelationRecord,
    StorageReader,
)

WORKSPACE_MAP_SCHEMA_VERSION: Final = "workspace_map.v1"
_ARTIFACT_DIRNAME: Final = "workspace-maps"
_MAX_SAMPLES: Final = 5
_MAX_PROFILE_SAMPLES: Final = 8
_PROFILE_STATUS_BY_RELATION: Final[dict[str, str]] = {
    "enabled_by": "enabled",
    "disabled_by": "disabled",
    "unknown_by": "unknown",
}
_TOKEN_RE: Final = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|\b)|[A-Z]?[a-z]+|[0-9]+")


@dataclass(frozen=True)
class WorkspaceTreeNode:
    """One summarized directory node in the workspace tree artifact."""

    node_id: str
    name: str
    path: str
    role: str
    layer: str | None
    domain: str | None
    feature: str | None
    summary: str
    direct_file_count: int
    total_file_count: int
    module_count: int
    key_files: tuple[str, ...] = ()
    key_modules: tuple[str, ...] = ()
    children: tuple["WorkspaceTreeNode", ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable tree node."""

        return {
            "node_id": self.node_id,
            "name": self.name,
            "path": self.path,
            "role": self.role,
            "layer": self.layer,
            "domain": self.domain,
            "feature": self.feature,
            "summary": self.summary,
            "direct_file_count": self.direct_file_count,
            "total_file_count": self.total_file_count,
            "module_count": self.module_count,
            "key_files": list(self.key_files),
            "key_modules": list(self.key_modules),
            "children": [child.to_dict() for child in self.children],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkspaceViewItem:
    """One summarized view item for workspace/layer/domain/feature/profile projections."""

    item_id: str
    kind: str
    name: str
    summary: str
    source_paths: tuple[str, ...] = ()
    module_names: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    related_items: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable projection item."""

        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "name": self.name,
            "summary": self.summary,
            "source_paths": list(self.source_paths),
            "module_names": list(self.module_names),
            "entity_ids": list(self.entity_ids),
            "related_items": list(self.related_items),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkspaceProjectionView:
    """One named projection view."""

    view_name: str
    summary: str
    items: tuple[WorkspaceViewItem, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable view payload."""

        return {
            "view_name": self.view_name,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkspaceMapArtifact:
    """Stable workspace map artifact written under artifacts/workspace-maps."""

    schema_version: str
    snapshot_id: str
    workspace_root: str
    inventory_hash: str
    generated_at: str
    summary: Mapping[str, object]
    workspace_tree: WorkspaceTreeNode
    views: Mapping[str, WorkspaceProjectionView]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable workspace map artifact."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "workspace_root": self.workspace_root,
            "inventory_hash": self.inventory_hash,
            "generated_at": self.generated_at,
            "summary": dict(self.summary),
            "workspace_tree": self.workspace_tree.to_dict(),
            "views": {
                name: view.to_dict()
                for name, view in sorted(self.views.items(), key=lambda item: item[0])
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WorkspaceMapWriteResult:
    """Result of collecting and writing one workspace map artifact."""

    artifact: WorkspaceMapArtifact
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class PathClassification:
    """One Active-aware path classification for projection grouping."""

    area: str | None
    layer: str | None
    role: str
    anchor_path: str
    domain: str | None = None
    feature: str | None = None
    display_domain: str | None = None
    display_feature: str | None = None


@dataclass
class _DirectoryAccumulator:
    path: str
    children: set[str] = field(default_factory=set)
    direct_files: list[str] = field(default_factory=list)
    module_names: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _RoleProjection:
    path: str
    layer: str | None
    role: str
    area: str | None
    domain: str | None
    feature: str | None
    display_domain: str | None
    display_feature: str | None
    file_count: int
    module_names: tuple[str, ...]
    entity_ids: tuple[str, ...]


class WorkspaceMapBuilder:
    """Build and write workspace tree and projection artifacts from indexed facts."""

    def __init__(self, config: ActiveKnowledgeConfig, *, cwd: Path | None = None) -> None:
        self._config = config
        self._cwd = (cwd or Path.cwd()).expanduser()
        self._default_output_root = resolve_runtime_path(
            config.storage.local_artifacts_root,
            self._cwd,
        )

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
    ) -> WorkspaceMapBuilder:
        """Build a workspace map builder from validated runtime config."""

        return cls(config, cwd=cwd)

    def collect(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        workspace_inventory: WorkspaceInventory,
        reader: StorageReader,
        profiles: Sequence[ProfileRecord] | None = None,
        profile_resolution: Mapping[str, object] | None = None,
    ) -> WorkspaceMapArtifact:
        """Collect one workspace map artifact from indexed logical views."""

        scope = QueryScope(snapshot_id=snapshot_id)
        entities = tuple(
            item.record
            for item in reader.logical_entities(scope)
            if item.record.profile_id == ALL_SCOPE
        )
        relations = tuple(item.record for item in reader.logical_relations(scope))
        base_relations = tuple(record for record in relations if record.profile_id == ALL_SCOPE)
        active_profiles = tuple(
            sorted(
                tuple(reader.iter_profiles(snapshot_id=snapshot_id))
                if profiles is None
                else tuple(profiles),
                key=lambda record: (record.profile_id, record.profile_record_id),
            )
        )
        module_paths, file_to_modules = _module_mappings(entities, base_relations)
        tree = _build_workspace_tree(
            workspace_inventory=workspace_inventory,
            module_paths=module_paths,
        )
        tree_nodes = _index_tree_nodes(tree)
        role_projections = _collect_role_projections(
            tree_nodes=tree_nodes,
            entities=entities,
            module_paths=module_paths,
        )
        views = {
            "workspace": _build_workspace_view(
                workspace_inventory=workspace_inventory,
                role_projections=role_projections,
            ),
            "layer": _build_layer_view(
                workspace_inventory=workspace_inventory,
                role_projections=role_projections,
            ),
            "domain": _build_domain_view(role_projections=role_projections),
            "feature": _build_feature_view(role_projections=role_projections),
            "profile": _build_profile_view(
                profiles=active_profiles,
                entities=entities,
                relations=relations,
                file_to_modules=file_to_modules,
            ),
        }
        metadata: dict[str, object] = {}
        if profile_resolution is not None:
            metadata["profile_resolution"] = dict(profile_resolution)
        return WorkspaceMapArtifact(
            schema_version=WORKSPACE_MAP_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            workspace_root=workspace_inventory.workspace_root,
            inventory_hash=workspace_inventory.inventory_hash,
            generated_at=_utc_now(),
            summary={
                "area_count": len(workspace_inventory.areas),
                "repository_count": len(workspace_inventory.repositories),
                "file_count": len(workspace_inventory.files),
                "profile_count": len(active_profiles),
                "view_names": sorted(views),
            },
            workspace_tree=tree,
            views=views,
            metadata=metadata,
        )

    def write(
        self,
        artifact: WorkspaceMapArtifact,
        *,
        output_root: Path | None = None,
    ) -> tuple[Path, ...]:
        """Write one workspace map artifact under `workspace-maps/`."""

        root = (output_root or self._default_output_root).expanduser()
        target_dir = root / _ARTIFACT_DIRNAME
        target_dir.mkdir(parents=True, exist_ok=True)
        file_name = _artifact_file_name(artifact.snapshot_id)
        payload = json.dumps(
            artifact.to_dict(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )

        snapshot_path = target_dir / file_name
        snapshot_path.write_text(payload, encoding="utf-8")
        return (snapshot_path,)

    def collect_and_write(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        workspace_inventory: WorkspaceInventory,
        reader: StorageReader,
        profiles: Sequence[ProfileRecord] | None = None,
        profile_resolution: Mapping[str, object] | None = None,
        output_root: Path | None = None,
    ) -> WorkspaceMapWriteResult:
        """Collect and persist one workspace map artifact."""

        artifact = self.collect(
            snapshot_id=snapshot_id,
            workspace_inventory=workspace_inventory,
            reader=reader,
            profiles=profiles,
            profile_resolution=profile_resolution,
        )
        return WorkspaceMapWriteResult(
            artifact=artifact,
            artifact_paths=self.write(artifact, output_root=output_root),
        )


def _build_workspace_tree(
    *,
    workspace_inventory: WorkspaceInventory,
    module_paths: Mapping[str, tuple[str, ...]],
) -> WorkspaceTreeNode:
    states: dict[str, _DirectoryAccumulator] = {"": _DirectoryAccumulator(path="")}
    for area in workspace_inventory.areas:
        if area.relative_path:
            _ensure_directory(states, area.relative_path)
    for entry in workspace_inventory.files:
        _register_file(states, entry.relative_path)
    for directory_path, names in module_paths.items():
        if not directory_path:
            continue
        state = _ensure_directory(states, directory_path)
        state.module_names.update(names)
    return _build_tree_node(
        "",
        states=states,
        workspace_name=Path(workspace_inventory.workspace_root).name or "workspace",
    )


def _build_tree_node(
    path: str,
    *,
    states: Mapping[str, _DirectoryAccumulator],
    workspace_name: str,
) -> WorkspaceTreeNode:
    state = states[path]
    children = tuple(
        _build_tree_node(child, states=states, workspace_name=workspace_name)
        for child in sorted(state.children)
    )
    classification = classify_path(path)
    total_file_count = len(state.direct_files) + sum(child.total_file_count for child in children)
    name = workspace_name if not path else Path(path).name
    return WorkspaceTreeNode(
        node_id=f"dir:{path or '.'}",
        name=name,
        path=path,
        role=classification.role,
        layer=classification.layer,
        domain=classification.domain,
        feature=classification.feature,
        summary=_directory_summary(path, classification),
        direct_file_count=len(state.direct_files),
        total_file_count=total_file_count,
        module_count=len(state.module_names),
        key_files=tuple(sorted(state.direct_files)[:_MAX_SAMPLES]),
        key_modules=tuple(sorted(state.module_names)[:_MAX_SAMPLES]),
        children=children,
        metadata={
            "area": classification.area,
            "child_directory_count": len(children),
        },
    )


def _collect_role_projections(
    *,
    tree_nodes: Mapping[str, WorkspaceTreeNode],
    entities: Sequence[EntityRecord],
    module_paths: Mapping[str, tuple[str, ...]],
) -> tuple[_RoleProjection, ...]:
    entity_ids_by_anchor: dict[str, set[str]] = defaultdict(set)
    for entity in entities:
        entity_path = _entity_projection_path(entity)
        if entity_path is None:
            continue
        classification = classify_path(entity_path)
        if not classification.anchor_path:
            continue
        entity_ids_by_anchor[classification.anchor_path].add(entity.entity_id)

    items: list[_RoleProjection] = []
    for path, node in sorted(tree_nodes.items(), key=lambda item: item[0]):
        if not path:
            continue
        classification = classify_path(path)
        if classification.role == "directory" or classification.anchor_path != path:
            continue
        items.append(
            _RoleProjection(
                path=path,
                layer=classification.layer,
                role=classification.role,
                area=classification.area,
                domain=classification.domain,
                feature=classification.feature,
                display_domain=classification.display_domain,
                display_feature=classification.display_feature,
                file_count=node.total_file_count,
                module_names=module_paths.get(path, ()),
                entity_ids=tuple(sorted(entity_ids_by_anchor.get(path, ()))),
            )
        )
    return tuple(items)


def _build_workspace_view(
    *,
    workspace_inventory: WorkspaceInventory,
    role_projections: Sequence[_RoleProjection],
) -> WorkspaceProjectionView:
    items: list[WorkspaceViewItem] = []
    for area in sorted(workspace_inventory.areas, key=lambda item: item.relative_path):
        items.append(
            WorkspaceViewItem(
                item_id=f"area:{area.relative_path or area.name}",
                kind="area",
                name=area.name,
                summary=f"Workspace area `{area.relative_path}` with {area.file_count} files.",
                source_paths=(area.relative_path,),
                metadata={
                    "display_path": area.display_path,
                    "file_count": area.file_count,
                    "directory_count": area.directory_count,
                },
            )
        )
    for repository in sorted(
        workspace_inventory.repositories,
        key=lambda item: (not item.is_workspace_root, item.relative_path),
    ):
        items.append(
            WorkspaceViewItem(
                item_id=f"repo:{repository.relative_path or '.'}",
                kind="repository",
                name=repository.relative_path or ".",
                summary=_repository_summary(repository),
                source_paths=(repository.relative_path or ".",),
                metadata={
                    "commit": repository.commit,
                    "branch": repository.branch,
                    "dirty": repository.dirty,
                    "boundary_kind": repository.boundary_kind,
                    "is_workspace_root": repository.is_workspace_root,
                },
            )
        )
    for projection in role_projections:
        items.append(
            WorkspaceViewItem(
                item_id=f"path-role:{projection.path}",
                kind="directory_role",
                name=projection.path,
                summary=_projection_summary(projection),
                source_paths=(projection.path,),
                module_names=projection.module_names,
                entity_ids=projection.entity_ids,
                metadata={
                    "area": projection.area,
                    "layer": projection.layer,
                    "role": projection.role,
                    "domain": projection.domain,
                    "feature": projection.feature,
                    "file_count": projection.file_count,
                },
            )
        )
    return WorkspaceProjectionView(
        view_name="workspace",
        summary="Workspace areas, repositories, and Active-aware directory responsibilities.",
        items=tuple(items),
        metadata={
            "area_count": len(workspace_inventory.areas),
            "repository_count": len(workspace_inventory.repositories),
            "role_count": len(role_projections),
        },
    )


def _build_layer_view(
    *,
    workspace_inventory: WorkspaceInventory,
    role_projections: Sequence[_RoleProjection],
) -> WorkspaceProjectionView:
    grouped: dict[str, dict[str, object]] = {}
    for projection in role_projections:
        if projection.layer is None:
            continue
        bucket = grouped.setdefault(
            projection.layer,
            {
                "paths": set(),
                "modules": set(),
                "roles": set(),
                "areas": set(),
                "domains": set(),
                "features": set(),
                "entity_ids": set(),
                "file_count": 0,
            },
        )
        cast_set(bucket["paths"]).add(projection.path)
        cast_set(bucket["modules"]).update(projection.module_names)
        cast_set(bucket["roles"]).add(projection.role)
        if projection.area is not None:
            cast_set(bucket["areas"]).add(projection.area)
        if projection.domain is not None:
            cast_set(bucket["domains"]).add(projection.domain)
        if projection.feature is not None:
            cast_set(bucket["features"]).add(projection.feature)
        cast_set(bucket["entity_ids"]).update(projection.entity_ids)
        bucket["file_count"] = int(bucket["file_count"]) + projection.file_count

    items: list[WorkspaceViewItem] = []
    for layer, bucket in sorted(grouped.items(), key=lambda item: item[0]):
        paths = tuple(sorted(cast_set(bucket["paths"])))
        modules = tuple(sorted(cast_set(bucket["modules"])))
        items.append(
            WorkspaceViewItem(
                item_id=f"layer:{layer}",
                kind="layer",
                name=layer,
                summary=f"Layer `{layer}` covers {len(paths)} anchor paths and {len(modules)} modules.",
                source_paths=paths,
                module_names=modules,
                entity_ids=tuple(sorted(cast_set(bucket["entity_ids"]))),
                metadata={
                    "roles": sorted(cast_set(bucket["roles"])),
                    "areas": sorted(cast_set(bucket["areas"])),
                    "domains": sorted(cast_set(bucket["domains"])),
                    "features": sorted(cast_set(bucket["features"])),
                    "file_count": int(bucket["file_count"]),
                },
            )
        )

    if not items:
        for area in sorted(workspace_inventory.areas, key=lambda item: item.relative_path):
            items.append(
                WorkspaceViewItem(
                    item_id=f"layer:{_normalize_key(area.name)}",
                    kind="layer",
                    name=area.name,
                    summary=f"Fallback layer bucket for workspace area `{area.relative_path}`.",
                    source_paths=(area.relative_path,),
                    metadata={
                        "roles": ["workspace_area"],
                        "file_count": area.file_count,
                    },
                )
            )
    return WorkspaceProjectionView(
        view_name="layer",
        summary="Layer projection derived from Active path mapping and module anchors.",
        items=tuple(items),
        metadata={"layer_count": len(items)},
    )


def _build_domain_view(
    *,
    role_projections: Sequence[_RoleProjection],
) -> WorkspaceProjectionView:
    grouped: dict[str, dict[str, object]] = {}
    feature_lookup: dict[str, set[str]] = defaultdict(set)
    for projection in role_projections:
        if projection.feature is not None:
            feature_lookup[projection.feature].add(projection.path)
    for projection in role_projections:
        if projection.domain is None:
            continue
        bucket = grouped.setdefault(
            projection.domain,
            {
                "display_name": projection.display_domain or projection.domain,
                "paths": set(),
                "service_paths": set(),
                "engine_paths": set(),
                "modules": set(),
                "entity_ids": set(),
                "file_count": 0,
                "related_features": set(),
            },
        )
        cast_set(bucket["paths"]).add(projection.path)
        cast_set(bucket["modules"]).update(projection.module_names)
        cast_set(bucket["entity_ids"]).update(projection.entity_ids)
        bucket["file_count"] = int(bucket["file_count"]) + projection.file_count
        if projection.role == "service_package":
            cast_set(bucket["service_paths"]).add(projection.path)
        if projection.role == "engine_component":
            cast_set(bucket["engine_paths"]).add(projection.path)

    for domain_id, bucket in grouped.items():
        for feature_id, feature_paths in feature_lookup.items():
            if _keys_related(domain_id, feature_id):
                cast_set(bucket["related_features"]).update(feature_paths)

    items = [
        WorkspaceViewItem(
            item_id=f"domain:{domain_id}",
            kind="domain",
            name=str(bucket["display_name"]),
            summary=(
                f"Domain `{bucket['display_name']}` links "
                f"{len(cast_set(bucket['service_paths']))} service roots and "
                f"{len(cast_set(bucket['engine_paths']))} engine roots."
            ),
            source_paths=tuple(sorted(cast_set(bucket["paths"]))),
            module_names=tuple(sorted(cast_set(bucket["modules"]))),
            entity_ids=tuple(sorted(cast_set(bucket["entity_ids"]))),
            related_items=tuple(sorted(cast_set(bucket["related_features"]))),
            metadata={
                "service_paths": sorted(cast_set(bucket["service_paths"])),
                "engine_paths": sorted(cast_set(bucket["engine_paths"])),
                "file_count": int(bucket["file_count"]),
            },
        )
        for domain_id, bucket in sorted(grouped.items(), key=lambda item: item[0])
    ]
    return WorkspaceProjectionView(
        view_name="domain",
        summary="Domain projection seeded from `packages/services/*` and `framework/engine/*`.",
        items=tuple(items),
        metadata={"domain_count": len(items)},
    )


def _build_feature_view(
    *,
    role_projections: Sequence[_RoleProjection],
) -> WorkspaceProjectionView:
    grouped: dict[str, dict[str, object]] = {}
    domain_lookup: dict[str, set[str]] = defaultdict(set)
    for projection in role_projections:
        if projection.domain is not None:
            domain_lookup[projection.domain].add(projection.path)
    for projection in role_projections:
        if projection.feature is None:
            continue
        bucket = grouped.setdefault(
            projection.feature,
            {
                "display_name": projection.display_feature or projection.feature,
                "paths": set(),
                "app_paths": set(),
                "ui_paths": set(),
                "modules": set(),
                "entity_ids": set(),
                "file_count": 0,
                "related_domains": set(),
            },
        )
        cast_set(bucket["paths"]).add(projection.path)
        cast_set(bucket["modules"]).update(projection.module_names)
        cast_set(bucket["entity_ids"]).update(projection.entity_ids)
        bucket["file_count"] = int(bucket["file_count"]) + projection.file_count
        if projection.role == "app_package":
            cast_set(bucket["app_paths"]).add(projection.path)
        if projection.role == "ui_screen_family":
            cast_set(bucket["ui_paths"]).add(projection.path)

    for feature_id, bucket in grouped.items():
        for domain_id, domain_paths in domain_lookup.items():
            if _keys_related(domain_id, feature_id):
                cast_set(bucket["related_domains"]).update(domain_paths)

    items = [
        WorkspaceViewItem(
            item_id=f"feature:{feature_id}",
            kind="feature",
            name=str(bucket["display_name"]),
            summary=(
                f"Feature `{bucket['display_name']}` links "
                f"{len(cast_set(bucket['app_paths']))} app roots and "
                f"{len(cast_set(bucket['ui_paths']))} UI roots."
            ),
            source_paths=tuple(sorted(cast_set(bucket["paths"]))),
            module_names=tuple(sorted(cast_set(bucket["modules"]))),
            entity_ids=tuple(sorted(cast_set(bucket["entity_ids"]))),
            related_items=tuple(sorted(cast_set(bucket["related_domains"]))),
            metadata={
                "app_paths": sorted(cast_set(bucket["app_paths"])),
                "ui_paths": sorted(cast_set(bucket["ui_paths"])),
                "file_count": int(bucket["file_count"]),
            },
        )
        for feature_id, bucket in sorted(grouped.items(), key=lambda item: item[0])
    ]
    return WorkspaceProjectionView(
        view_name="feature",
        summary="Feature projection seeded from `packages/apps/*` and `ui/*`.",
        items=tuple(items),
        metadata={"feature_count": len(items)},
    )


def _build_profile_view(
    *,
    profiles: Sequence[ProfileRecord],
    entities: Sequence[EntityRecord],
    relations: Sequence[RelationRecord],
    file_to_modules: Mapping[str, tuple[str, ...]],
) -> WorkspaceProjectionView:
    entity_by_id = {entity.entity_id: entity for entity in entities}
    file_entity_id_by_file_id = {
        entity.file_id: entity.entity_id
        for entity in entities
        if entity.entity_type == "File"
    }
    grouped_relations: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for relation in relations:
        status = _PROFILE_STATUS_BY_RELATION.get(relation.relation_type)
        if status is None or relation.profile_id == ALL_SCOPE:
            continue
        grouped_relations[relation.profile_id][relation.src_entity_id].add(status)

    items: list[WorkspaceViewItem] = []
    for profile in profiles:
        statuses_by_entity = grouped_relations.get(profile.profile_id, {})
        counts = {"enabled": 0, "disabled": 0, "unknown": 0}
        impacted_paths: set[str] = set()
        impacted_modules: set[str] = set()
        impacted_entities: list[str] = []
        for entity_id, statuses in sorted(statuses_by_entity.items()):
            status = _dominant_status(statuses)
            counts[status] += 1
            entity = entity_by_id.get(entity_id)
            if entity is None:
                continue
            impacted_entities.append(entity.name)
            entity_path = _entity_projection_path(entity)
            if entity_path is not None:
                impacted_paths.add(entity_path)
            if entity.entity_type == "Module":
                impacted_modules.add(_module_display_name(entity))
                continue
            file_entity_id = (
                entity.entity_id
                if entity.entity_type == "File"
                else file_entity_id_by_file_id.get(entity.file_id)
            )
            if file_entity_id is None:
                continue
            impacted_modules.update(file_to_modules.get(file_entity_id, ()))
        macro_summary = profile.metadata.get("macro_summary")
        summary = (
            f"Profile `{profile.profile_id}` projects {sum(counts.values())} conditioned entities "
            f"({counts['enabled']} enabled, {counts['disabled']} disabled, {counts['unknown']} unknown)."
        )
        items.append(
            WorkspaceViewItem(
                item_id=f"profile:{profile.profile_id}",
                kind="profile",
                name=profile.profile_id,
                summary=summary,
                source_paths=tuple(sorted(impacted_paths)[:_MAX_PROFILE_SAMPLES]),
                module_names=tuple(sorted(impacted_modules)[:_MAX_PROFILE_SAMPLES]),
                entity_ids=tuple(sorted(statuses_by_entity)[:_MAX_PROFILE_SAMPLES]),
                metadata={
                    "profile_record_id": profile.profile_record_id,
                    "app": profile.app,
                    "board": profile.board,
                    "defconfig_path": profile.defconfig_path,
                    "dotconfig_path": profile.dotconfig_path,
                    "counts": counts,
                    "macro_summary": macro_summary if isinstance(macro_summary, Mapping) else {},
                    "impacted_entities": impacted_entities[:_MAX_PROFILE_SAMPLES],
                },
            )
        )
    return WorkspaceProjectionView(
        view_name="profile",
        summary="Profile projection derived from profile-conditioned relations.",
        items=tuple(items),
        metadata={"profile_count": len(items)},
    )


def classify_path(relative_path: str) -> PathClassification:
    """Classify one workspace-relative path using Active workspace conventions."""

    normalized = relative_path.strip("/")
    if not normalized:
        return PathClassification(
            area=None,
            layer=None,
            role="workspace_root",
            anchor_path="",
        )
    parts = normalized.split("/")
    area = parts[0]
    if len(parts) >= 3 and parts[0] == "packages" and parts[1] == "services":
        domain_display = parts[2]
        return PathClassification(
            area=area,
            layer="service",
            role="service_package",
            anchor_path="/".join(parts[:3]),
            domain=_normalize_key(domain_display),
            display_domain=domain_display,
        )
    if len(parts) >= 3 and parts[0] == "packages" and parts[1] == "apps":
        feature_display = parts[2]
        return PathClassification(
            area=area,
            layer="app",
            role="app_package",
            anchor_path="/".join(parts[:3]),
            feature=_normalize_key(feature_display),
            display_feature=feature_display,
        )
    if len(parts) >= 2 and parts[0] == "ui":
        feature_display = parts[1]
        return PathClassification(
            area=area,
            layer="ui",
            role="ui_screen_family",
            anchor_path="/".join(parts[:2]),
            feature=_normalize_key(feature_display),
            display_feature=feature_display,
        )
    if len(parts) >= 3 and parts[0] == "framework" and parts[1] == "engine":
        component = parts[2]
        domain_display = _strip_suffix(component, "Engine")
        return PathClassification(
            area=area,
            layer="engine",
            role="engine_component",
            anchor_path="/".join(parts[:3]),
            domain=_normalize_key(domain_display),
            display_domain=domain_display,
        )
    if parts[0] == "uiframework":
        return PathClassification(
            area=area,
            layer="uiframework",
            role="uiframework_component",
            anchor_path="uiframework",
        )
    if parts[0] == "framework":
        return PathClassification(
            area=area,
            layer="framework",
            role="framework_root",
            anchor_path="framework",
        )
    if parts[0] == "drivers":
        return PathClassification(
            area=area,
            layer="driver",
            role="driver_root",
            anchor_path="drivers",
        )
    return PathClassification(
        area=area,
        layer=None,
        role="directory",
        anchor_path=normalized,
    )


def _module_mappings(
    entities: Sequence[EntityRecord],
    relations: Sequence[RelationRecord],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    modules_by_id = {
        entity.entity_id: entity
        for entity in entities
        if entity.entity_type == "Module"
    }
    module_paths: dict[str, set[str]] = defaultdict(set)
    for module in modules_by_id.values():
        module_path = _module_directory_path(module)
        if module_path:
            module_paths[module_path].add(_module_display_name(module))

    file_to_modules: dict[str, set[str]] = defaultdict(set)
    for relation in relations:
        if relation.relation_type != "belongs_to_module":
            continue
        module = modules_by_id.get(relation.dst_entity_id)
        if module is None:
            continue
        file_to_modules[relation.src_entity_id].add(_module_display_name(module))

    return (
        {path: tuple(sorted(names)) for path, names in sorted(module_paths.items())},
        {entity_id: tuple(sorted(names)) for entity_id, names in sorted(file_to_modules.items())},
    )


def _entity_projection_path(entity: EntityRecord) -> str | None:
    if entity.entity_type == "Directory":
        return entity.path
    if entity.entity_type == "Module":
        return _module_directory_path(entity) or entity.path
    if entity.entity_type == "File":
        return entity.qualified_name
    path = entity.qualified_name
    if "::" in path:
        return path.split("::", maxsplit=1)[0]
    if "#" in path:
        return path.split("#", maxsplit=1)[0]
    return entity.path or None


def _module_directory_path(entity: EntityRecord) -> str | None:
    value = entity.metadata.get("module_path")
    return value if isinstance(value, str) and value else None


def _module_display_name(entity: EntityRecord) -> str:
    return entity.qualified_name or entity.name


def _build_tree_paths(relative_path: str) -> list[str]:
    path = relative_path.strip("/")
    if not path:
        return []
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _ensure_directory(
    states: dict[str, _DirectoryAccumulator],
    directory_path: str,
) -> _DirectoryAccumulator:
    normalized = directory_path.strip("/")
    if not normalized:
        return states[""]
    if normalized in states:
        return states[normalized]
    ancestors = _build_tree_paths(normalized)
    parent = ""
    for ancestor in ancestors:
        states.setdefault(ancestor, _DirectoryAccumulator(path=ancestor))
        if parent != ancestor:
            states[parent].children.add(ancestor)
            parent = ancestor
    states.setdefault(normalized, _DirectoryAccumulator(path=normalized))
    if parent != normalized:
        states[parent].children.add(normalized)
    return states[normalized]


def _register_file(states: dict[str, _DirectoryAccumulator], relative_path: str) -> None:
    normalized = relative_path.strip("/")
    if not normalized:
        return
    parent_path = Path(normalized).parent.as_posix()
    file_name = Path(normalized).name
    if parent_path in {"", "."}:
        states[""].direct_files.append(file_name)
        return
    parent_state = _ensure_directory(states, parent_path)
    parent_state.direct_files.append(file_name)


def _index_tree_nodes(root: WorkspaceTreeNode) -> dict[str, WorkspaceTreeNode]:
    items = {root.path: root}
    for child in root.children:
        items.update(_index_tree_nodes(child))
    return items


def _directory_summary(path: str, classification: PathClassification) -> str:
    if classification.role == "workspace_root":
        return "Workspace root with aggregated directory and module counts."
    if classification.role == "service_package" and classification.display_domain:
        return f"Service package for the `{classification.display_domain}` domain."
    if classification.role == "app_package" and classification.display_feature:
        return f"Application package for the `{classification.display_feature}` feature."
    if classification.role == "ui_screen_family" and classification.display_feature:
        return f"UI screen family for the `{classification.display_feature}` feature."
    if classification.role == "engine_component" and classification.display_domain:
        return f"Engine component for the `{classification.display_domain}` domain."
    if classification.role == "uiframework_component":
        return "UI framework components and reusable widgets."
    if classification.role == "framework_root":
        return "Framework-level shared runtime and infrastructure code."
    if classification.role == "driver_root":
        return "Driver-level hardware adaptation code."
    if path:
        return f"Directory `{path}` inside the workspace."
    return "Workspace directory."


def _projection_summary(projection: _RoleProjection) -> str:
    classification = PathClassification(
        area=projection.area,
        layer=projection.layer,
        role=projection.role,
        anchor_path=projection.path,
        domain=projection.domain,
        feature=projection.feature,
        display_domain=projection.display_domain,
        display_feature=projection.display_feature,
    )
    return _directory_summary(projection.path, classification)


def _repository_summary(repository: RepositoryInfo) -> str:
    commit = repository.commit or "unknown"
    branch = repository.branch or "detached"
    prefix = "Workspace root repository" if repository.is_workspace_root else "Nested repository"
    return f"{prefix} `{repository.relative_path or '.'}` at {branch} ({commit[:12]})."


def _artifact_file_name(snapshot_id: str) -> str:
    if snapshot_id == CURRENT_SNAPSHOT_ID:
        return "current.json"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", snapshot_id).strip("_") or "snapshot"
    return f"{safe}.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_key(value: str) -> str:
    tokens = _tokens(value)
    if not tokens:
        fallback = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
        return fallback or value.lower()
    return "_".join(tokens)


def _tokens(value: str) -> tuple[str, ...]:
    items: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", value):
        if not part:
            continue
        matches = _TOKEN_RE.findall(part)
        if matches:
            items.extend(match.lower() for match in matches)
            continue
        items.append(part.lower())
    return tuple(items)


def _strip_suffix(value: str, suffix: str) -> str:
    return value[: -len(suffix)] if value.endswith(suffix) and len(value) > len(suffix) else value


def _keys_related(left: str, right: str) -> bool:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    return bool(left_tokens and right_tokens and left_tokens.intersection(right_tokens))


def _dominant_status(statuses: set[str]) -> str:
    if "unknown" in statuses:
        return "unknown"
    if "disabled" in statuses:
        return "disabled"
    return "enabled"


def cast_set(value: object) -> set[str]:
    """Narrow a projection bucket field back into a mutable string set."""

    return value if isinstance(value, set) else set()
