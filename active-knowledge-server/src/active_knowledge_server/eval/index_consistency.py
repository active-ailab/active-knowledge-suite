"""Helpers for resume/fresh logical-output consistency acceptance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.storage import QueryScope
from active_knowledge_server.storage.sqlite_store import SQLiteStorageAdapter

_COLLECTION_ID_ATTRS: dict[str, str] = {
    "file": "file_id",
    "chunk": "logical_object_id",
    "entity": "logical_object_id",
    "relation": "logical_object_id",
    "evidence": "logical_object_id",
    "vector_ref": "vector_ref_id",
}


def load_live_index_collections(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    scope: QueryScope | None = None,
) -> dict[str, tuple[object, ...]]:
    """Load the live logical collections that AR4-03 compares."""

    adapter = SQLiteStorageAdapter.from_config(config, cwd=cwd)
    reader = adapter.reader()
    query_scope = scope or QueryScope(snapshot_id=config.project.default_snapshot)
    return {
        "file": tuple(reader.iter_files(query_scope)),
        "chunk": tuple(reader.logical_chunks(query_scope)),
        "entity": tuple(reader.logical_entities(query_scope)),
        "relation": tuple(reader.logical_relations(query_scope)),
        "evidence": tuple(reader.logical_evidence(query_scope)),
        "vector_ref": tuple(reader.iter_vector_refs(query_scope)),
    }


def summarize_live_index_collections(
    collections: Mapping[str, Sequence[object]],
) -> dict[str, dict[str, object]]:
    """Return stable count + digest summaries for each live collection."""

    summary: dict[str, dict[str, object]] = {}
    for name, records in collections.items():
        summary[name] = summarize_record_collection(records, id_attr=_id_attr_for_collection(name))
    return summary


def compare_live_index_collections(
    fresh: Mapping[str, Sequence[object]],
    resumed: Mapping[str, Sequence[object]],
    *,
    sample_limit: int = 10,
) -> dict[str, object]:
    """Compare fresh and resumed logical outputs collection-by-collection."""

    collection_names = tuple(
        sorted(set(fresh).union(resumed), key=lambda item: _collection_sort_key(item))
    )
    comparisons: dict[str, dict[str, object]] = {}
    for name in collection_names:
        comparisons[name] = compare_record_collections(
            fresh.get(name, ()),
            resumed.get(name, ()),
            id_attr=_id_attr_for_collection(name),
            sample_limit=sample_limit,
        )
    return {
        "all_equal": all(bool(item["equal"]) for item in comparisons.values()),
        "collections": comparisons,
    }


def summarize_record_collection(
    records: Sequence[object],
    *,
    id_attr: str,
) -> dict[str, object]:
    """Summarize one record collection with a stable digest."""

    canonical = canonical_record_map(records, id_attr=id_attr)
    ordered_items = tuple(sorted(canonical.items()))
    return {
        "count": len(ordered_items),
        "digest": _collection_digest(ordered_items),
    }


def compare_record_collections(
    fresh: Sequence[object],
    resumed: Sequence[object],
    *,
    id_attr: str,
    sample_limit: int = 10,
) -> dict[str, object]:
    """Compare two canonicalized record collections."""

    fresh_map = canonical_record_map(fresh, id_attr=id_attr)
    resumed_map = canonical_record_map(resumed, id_attr=id_attr)
    fresh_ids = set(fresh_map)
    resumed_ids = set(resumed_map)
    changed_ids = [
        record_id
        for record_id in sorted(fresh_ids & resumed_ids)
        if fresh_map[record_id] != resumed_map[record_id]
    ]
    fresh_only = sorted(fresh_ids - resumed_ids)
    resumed_only = sorted(resumed_ids - fresh_ids)
    ordered_fresh = tuple(sorted(fresh_map.items()))
    ordered_resumed = tuple(sorted(resumed_map.items()))
    return {
        "equal": ordered_fresh == ordered_resumed,
        "fresh_count": len(fresh_map),
        "resumed_count": len(resumed_map),
        "fresh_digest": _collection_digest(ordered_fresh),
        "resumed_digest": _collection_digest(ordered_resumed),
        "fresh_only_sample": fresh_only[:sample_limit],
        "resumed_only_sample": resumed_only[:sample_limit],
        "changed_sample": changed_ids[:sample_limit],
    }


def canonical_record_map(
    records: Sequence[object],
    *,
    id_attr: str,
) -> dict[str, str]:
    """Canonicalize records into one stable id -> JSON mapping."""

    canonical: dict[str, str] = {}
    for record in records:
        payload = _normalize_value(_record_payload(record))
        record_id = payload.get(id_attr)
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"record is missing required id attribute {id_attr!r}")
        canonical[record_id] = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    return canonical


def _record_payload(record: object) -> dict[str, object]:
    if is_dataclass(record):
        payload = asdict(record)  # type: ignore[arg-type]
    elif isinstance(record, Mapping):
        payload = dict(record)
    elif hasattr(record, "to_dict"):
        raw_payload = record.to_dict()
        payload = raw_payload if isinstance(raw_payload, dict) else {"value": raw_payload}
    else:
        payload = dict(vars(record))
    return {
        str(key): value
        for key, value in payload.items()
        if key not in {"created_at", "updated_at"}
    }


def _normalize_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if key not in {"created_at", "updated_at", "freshness_ts"}
        }
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _collection_digest(entries: Sequence[tuple[str, str]]) -> str:
    encoded = json.dumps(entries, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _id_attr_for_collection(name: str) -> str:
    if name not in _COLLECTION_ID_ATTRS:
        raise KeyError(f"unsupported live collection {name!r}")
    return _COLLECTION_ID_ATTRS[name]


def _collection_sort_key(name: str) -> tuple[int, str]:
    order = {key: index for index, key in enumerate(_COLLECTION_ID_ATTRS)}
    return (order.get(name, len(order)), name)
