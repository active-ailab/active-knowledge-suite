from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.indexing.jobs import (
    INDEX_JOB_LOCK_ID,
    IndexJobRunner,
    JobLockConflictError,
    JobStateTransitionError,
    SQLiteJobStore,
    decode_task_checkpoint,
    parse_timestamp,
    record_task_applied_checkpoint,
    record_task_collected_checkpoint,
    task_checkpoint_key,
    task_has_applied_checkpoint,
)
from active_knowledge_server.indexing.tasks import IndexTask
from active_knowledge_server.storage.sqlite_store import migrate_sqlite_store


def build_store(tmp_path: Path) -> SQLiteJobStore:
    jobs_path = tmp_path / "jobs.db"
    migrate_sqlite_store(jobs_path, target="jobs")
    return SQLiteJobStore(jobs_path)


def test_concurrent_index_jobs_compete_for_single_overlay_lock(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    first = store.create_job(job_id="job-index-1")
    second = store.create_job(job_id="job-index-2")

    lease = store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=first.job_id)

    assert lease.owner_job_id == first.job_id
    with pytest.raises(JobLockConflictError, match="already owned"):
        store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=second.job_id)

    assert store.release_lock(INDEX_JOB_LOCK_ID, owner_job_id=first.job_id) is True
    second_lease = store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=second.job_id)
    assert second_lease.owner_job_id == second.job_id


def test_expired_lock_can_be_reacquired_by_another_job(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    first = store.create_job(job_id="job-index-1")
    second = store.create_job(job_id="job-index-2")

    store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=first.job_id, ttl_seconds=-1)
    lease = store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=second.job_id)

    assert lease.owner_job_id == second.job_id


def test_find_resumable_index_job_blocks_on_unexpired_lock(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(
        job_id="job-index",
        metadata={
            "plan_signature": "sig-current",
            "requested_mode": "incremental",
            "requested_source": "all",
        },
    )
    store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=job.job_id, ttl_seconds=3600)

    with pytest.raises(JobLockConflictError, match="still active"):
        store.find_resumable_index_job(
            plan_signature="sig-current",
            metadata_match={"requested_mode": "incremental", "requested_source": "all"},
        )


def test_find_resumable_index_job_allows_expired_lock(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(
        job_id="job-index",
        metadata={
            "plan_signature": "sig-current",
            "requested_mode": "incremental",
            "requested_source": "all",
        },
    )
    store.acquire_lock(INDEX_JOB_LOCK_ID, owner_job_id=job.job_id, ttl_seconds=-1)

    resumable = store.find_resumable_index_job(
        plan_signature="sig-current",
        metadata_match={"requested_mode": "incremental", "requested_source": "all"},
    )

    assert resumable is not None
    assert resumable.job_id == job.job_id


def test_find_resumable_index_job_requires_matching_signature(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.create_job(
        job_id="job-index",
        metadata={
            "plan_signature": "sig-old",
            "requested_mode": "incremental",
            "requested_source": "all",
        },
    )

    assert (
        store.find_resumable_index_job(
            plan_signature="sig-current",
            metadata_match={"requested_mode": "incremental", "requested_source": "all"},
        )
        is None
    )


def test_job_state_machine_rejects_invalid_transition(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")

    with pytest.raises(JobStateTransitionError, match="invalid job transition"):
        store.transition_job(job.job_id, "ready")


def test_resume_and_retry_preserve_checkpoints(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")
    store.set_checkpoint(job.job_id, "cursor", "file-17")
    store.transition_job(job.job_id, "discovering")
    store.transition_job(job.job_id, "failed", error_summary="interrupted")

    resume = store.resume_job(job.job_id)
    retry = store.retry_job(job.job_id)

    assert resume.job.status == "failed"
    assert resume.checkpoints["cursor"] == "file-17"
    assert retry.status == "pending"
    assert retry.metadata["retry_count"] == 1
    assert store.get_checkpoint(job.job_id, "cursor") == "file-17"


def test_resume_job_can_increment_resume_count(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index", metadata={"resume_count": 2})
    store.set_checkpoint(job.job_id, "cursor", "file-17")

    resume = store.resume_job(job.job_id, increment_resume_count=True)

    assert resume.job.metadata["resume_count"] == 3
    assert resume.job.metadata["execution_state"] == "running"
    assert resume.checkpoints["cursor"] == "file-17"


def test_transition_or_update_running_metadata_tracks_progress(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")

    discovering = store.transition_or_update_running_metadata(
        job.job_id,
        "discovering",
        metadata_update={
            "execution_state": "running",
            "last_phase": "discovering",
            "tasks_total": 3,
            "tasks_applied": 0,
        },
    )
    updated = store.transition_or_update_running_metadata(
        job.job_id,
        metadata_update={
            "last_task_key": "doc:apply:guide.md",
            "tasks_applied": 1,
        },
    )

    assert discovering.status == "discovering"
    assert updated.status == "discovering"
    assert updated.metadata["last_phase"] == "discovering"
    assert updated.metadata["last_task_key"] == "doc:apply:guide.md"
    assert updated.metadata["tasks_total"] == 3
    assert updated.metadata["tasks_applied"] == 1


def test_renew_lock_extends_ttl_and_records_heartbeat(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")
    first = store.acquire_lock(
        INDEX_JOB_LOCK_ID,
        owner_job_id=job.job_id,
        ttl_seconds=1,
        metadata={"last_phase": "parsing"},
    )

    renewed = store.renew_lock(
        INDEX_JOB_LOCK_ID,
        owner_job_id=job.job_id,
        ttl_seconds=3600,
        metadata_update={"last_task_key": "doc:apply:guide.md"},
    )

    assert renewed.expires_at is not None
    assert first.expires_at is not None
    assert parse_timestamp(renewed.expires_at) > parse_timestamp(first.expires_at)
    assert renewed.metadata["last_phase"] == "parsing"
    assert renewed.metadata["last_task_key"] == "doc:apply:guide.md"
    assert isinstance(renewed.metadata["heartbeat_at"], str)


def test_supersede_job_marks_old_job_and_excludes_resume(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    old = store.create_job(
        job_id="job-index-old",
        metadata={
            "plan_signature": "sig-current",
            "requested_mode": "incremental",
            "requested_source": "all",
        },
    )
    new = store.create_job(job_id="job-index-new")
    store.transition_or_update_running_metadata(old.job_id, "discovering")

    superseded = store.supersede_job(old.job_id, superseded_by_job_id=new.job_id)

    assert superseded.status == "failed"
    assert superseded.metadata["execution_state"] == "superseded"
    assert superseded.metadata["superseded_by_job_id"] == new.job_id
    assert (
        store.find_resumable_index_job(
            plan_signature="sig-current",
            metadata_match={"requested_mode": "incremental", "requested_source": "all"},
        )
        is None
    )


def test_single_file_parse_failure_enters_partial_ready(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")
    runner = IndexJobRunner(store)

    def parse_file(path: str) -> None:
        if path == "bad.c":
            raise ValueError("syntax error")

    result = runner.run_files(job.job_id, ("good.c", "bad.c"), parse_file)
    stored = store.get_job(job.job_id)

    assert stored is not None
    assert result.job.status == "partial_ready"
    assert stored.status == "partial_ready"
    assert result.parsed_files == ("good.c",)
    assert result.failed_files == ("bad.c",)
    assert stored.metadata["files_failed"] == 1
    assert json.loads(store.get_checkpoint(job.job_id, "failed_files") or "{}") == {
        "bad.c": "syntax error"
    }
    assert store.get_lock(INDEX_JOB_LOCK_ID) is None


def test_all_files_parsed_job_reaches_ready(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")
    runner = IndexJobRunner(store)

    result = runner.run_files(job.job_id, ("a.c", "b.c"), lambda _path: None)

    assert result.job.status == "ready"
    assert result.failed_files == ()
    assert store.get_lock(INDEX_JOB_LOCK_ID) is None


def test_task_checkpoint_failure_after_apply_replays_idempotent_apply(
    tmp_path: Path,
) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")
    task = IndexTask(
        task_key="doc:apply:guide.md",
        phase="doc_apply",
        source_kind="doc",
        operation="apply",
        relative_path="guide.md",
        input_hash="doc-hash",
        schema_version="doc_indexer.v1",
    )
    applies: list[str] = []
    original_set_checkpoint = store.set_checkpoint
    failures_remaining = 1

    def flaky_set_checkpoint(job_id: str, key: str, value: str) -> None:
        nonlocal failures_remaining
        if key == task_checkpoint_key(task) and failures_remaining:
            failures_remaining -= 1
            raise RuntimeError("checkpoint write failed")
        original_set_checkpoint(job_id, key, value)

    store.set_checkpoint = flaky_set_checkpoint  # type: ignore[method-assign]

    def run_once() -> None:
        if task_has_applied_checkpoint(store.get_checkpoints(job.job_id), task):
            return
        applies.append(task.task_key)
        record_task_applied_checkpoint(store, job.job_id, task)

    with pytest.raises(RuntimeError, match="checkpoint write failed"):
        run_once()

    run_once()
    run_once()

    assert applies == [task.task_key, task.task_key]
    checkpoint = decode_task_checkpoint(store.get_checkpoint(job.job_id, task_checkpoint_key(task)))
    assert checkpoint is not None
    assert checkpoint.status == "applied"
    assert task_has_applied_checkpoint(store.get_checkpoints(job.job_id), task) is True


def test_collected_checkpoint_is_not_an_applied_skip_boundary(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    job = store.create_job(job_id="job-index")
    task = IndexTask(
        task_key="vector:doc:guide.md",
        phase="vectors_apply",
        source_kind="vector",
        operation="doc",
        relative_path="guide.md",
        input_hash="vector-hash",
        schema_version="embedding_preparation.v1",
    )

    record_task_collected_checkpoint(store, job.job_id, task)

    assert task_has_applied_checkpoint(store.get_checkpoints(job.job_id), task) is False
    collected = decode_task_checkpoint(
        store.get_checkpoint(
            job.job_id,
            task_checkpoint_key(task, status="collected"),
        )
    )
    assert collected is not None
    assert collected.status == "collected"
