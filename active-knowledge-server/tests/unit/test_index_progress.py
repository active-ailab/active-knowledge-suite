from __future__ import annotations

import pytest

from active_knowledge_server.indexing.progress import (
    INDEX_PROGRESS_PHASES,
    IndexProgressEvent,
    noop_progress_callback,
)


def test_progress_event_to_dict_is_json_safe() -> None:
    event = IndexProgressEvent(
        phase="code_collect",
        stage_total=10,
        stage_done=3,
        global_total=25,
        global_done=7,
        current_path="framework/core/task_manager.c",
        message="Collecting code bundle",
        warnings_count=1,
        started_at="2026-05-25T00:00:00Z",
        updated_at="2026-05-25T00:00:03Z",
        eta_seconds=12.5,
    )

    payload = event.to_dict()

    assert payload["phase"] == "code_collect"
    assert payload["stage_total"] == 10
    assert payload["stage_done"] == 3
    assert payload["global_total"] == 25
    assert payload["global_done"] == 7
    assert payload["current_path"] == "framework/core/task_manager.c"
    assert payload["warnings_count"] == 1
    assert payload["updated_at"] == "2026-05-25T00:00:03Z"


def test_progress_event_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="stage_done must be >= 0"):
        IndexProgressEvent(phase="plan", stage_done=-1)


def test_progress_event_rejects_done_above_total() -> None:
    with pytest.raises(ValueError, match="global_done cannot exceed global_total"):
        IndexProgressEvent(phase="doc_apply", global_done=4, global_total=3)


def test_progress_event_phases_match_contract() -> None:
    assert INDEX_PROGRESS_PHASES == (
        "plan",
        "discover",
        "code_collect",
        "code_apply",
        "doc_collect",
        "doc_apply",
        "vectors_apply",
        "profile_relations",
        "workspace_map",
        "done",
    )


def test_noop_progress_callback_accepts_event() -> None:
    noop_progress_callback(IndexProgressEvent(phase="done", global_done=1, global_total=1))
