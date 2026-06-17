from __future__ import annotations

from active_knowledge_server.eval.index_consistency import (
    compare_live_index_collections,
    compare_record_collections,
    summarize_record_collection,
)


def test_summarize_record_collection_ignores_runtime_fields_and_order() -> None:
    first = [
        {
            "file_id": "file:b",
            "relative_path": "b.c",
            "metadata": {"freshness_ts": "ignored", "kind": "code"},
            "updated_at": "ignored",
        },
        {
            "file_id": "file:a",
            "relative_path": "a.c",
            "metadata": {"kind": "code"},
            "created_at": "ignored",
        },
    ]
    second = [
        {
            "file_id": "file:a",
            "relative_path": "a.c",
            "metadata": {"kind": "code", "freshness_ts": "ignored-again"},
        },
        {
            "file_id": "file:b",
            "relative_path": "b.c",
            "metadata": {"kind": "code"},
        },
    ]

    assert summarize_record_collection(first, id_attr="file_id") == summarize_record_collection(
        second,
        id_attr="file_id",
    )


def test_compare_record_collections_reports_missing_and_changed_ids() -> None:
    fresh = [
        {"file_id": "file:a", "relative_path": "a.c"},
        {"file_id": "file:b", "relative_path": "b.c"},
    ]
    resumed = [
        {"file_id": "file:a", "relative_path": "a-renamed.c"},
        {"file_id": "file:c", "relative_path": "c.c"},
    ]

    result = compare_record_collections(fresh, resumed, id_attr="file_id", sample_limit=5)

    assert result["equal"] is False
    assert result["fresh_only_sample"] == ["file:b"]
    assert result["resumed_only_sample"] == ["file:c"]
    assert result["changed_sample"] == ["file:a"]


def test_compare_live_index_collections_orders_standard_collections() -> None:
    fresh = {
        "vector_ref": [{"vector_ref_id": "vector:1", "object_id": "chunk:1"}],
        "file": [{"file_id": "file:1", "relative_path": "a.c"}],
    }
    resumed = {
        "vector_ref": [{"vector_ref_id": "vector:1", "object_id": "chunk:1"}],
        "file": [{"file_id": "file:1", "relative_path": "a.c"}],
    }

    result = compare_live_index_collections(fresh, resumed)

    assert result["all_equal"] is True
    assert list(result["collections"]) == ["file", "vector_ref"]
