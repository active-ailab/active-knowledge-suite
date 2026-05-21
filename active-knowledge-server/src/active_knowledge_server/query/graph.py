"""Graph expansion over storage logical relation views."""

from __future__ import annotations

from dataclasses import dataclass, field

from active_knowledge_server.storage import (
    LogicalRelation,
    QueryScope,
    StorageReader,
)


@dataclass(frozen=True)
class GraphTraversalResult:
    """Bounded graph traversal result built only from live logical views."""

    entity_ids: tuple[str, ...]
    relations: tuple[LogicalRelation, ...]
    skipped_relation_ids: tuple[str, ...] = ()
    depth_by_entity_id: dict[str, int] = field(default_factory=dict)


def traverse_entity_graph(
    reader: StorageReader,
    seed_entity_ids: tuple[str, ...],
    *,
    scope: QueryScope,
    max_depth: int = 1,
    relation_types: tuple[str, ...] | None = None,
) -> GraphTraversalResult:
    """Traverse live logical relations without returning tombstoned entities."""

    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")

    live_entities = {entity.logical_object_id for entity in reader.logical_entities(scope)}
    visited: dict[str, int] = {}
    frontier: set[str] = set()
    for entity_id in seed_entity_ids:
        if entity_id in live_entities:
            visited[entity_id] = 0
            frontier.add(entity_id)

    relations_by_id: dict[str, LogicalRelation] = {}
    skipped: set[str] = set()
    all_relations = tuple(reader.logical_relations(scope))

    for depth in range(max_depth):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for relation in all_relations:
            if relation_types is not None and relation.record.relation_type not in relation_types:
                continue
            src = relation.record.src_entity_id
            dst = relation.record.dst_entity_id
            if src not in live_entities or dst not in live_entities:
                skipped.add(relation.logical_object_id)
                continue
            if src not in frontier and dst not in frontier:
                continue
            relations_by_id[relation.logical_object_id] = relation
            for neighbor in (src, dst):
                if neighbor not in visited:
                    visited[neighbor] = depth + 1
                    next_frontier.add(neighbor)
        frontier = next_frontier

    return GraphTraversalResult(
        entity_ids=tuple(sorted(visited, key=lambda entity_id: (visited[entity_id], entity_id))),
        relations=tuple(
            sorted(relations_by_id.values(), key=lambda relation: relation.logical_object_id)
        ),
        skipped_relation_ids=tuple(sorted(skipped)),
        depth_by_entity_id=dict(visited),
    )
