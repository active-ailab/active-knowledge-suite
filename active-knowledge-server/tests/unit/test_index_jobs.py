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
)
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
