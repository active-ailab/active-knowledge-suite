"""Index job state, locking, resume, and retry orchestration."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, cast

from active_knowledge_server.indexing.tasks import IndexTask
from active_knowledge_server.storage import (
    ALL_SCOPE,
    JobRecord,
    JobStatus,
    StorageMetadata,
    StorageWriteTarget,
)
from active_knowledge_server.storage.sqlite_store import (
    decode_metadata,
    encode_metadata,
    job_record_values,
    row_to_job_record,
    sqlite_connection,
    upsert_row,
    utc_now,
)

RUNNING_JOB_STATUSES: Final[tuple[JobStatus, ...]] = (
    "discovering",
    "parsing",
    "extracting",
    "embedding",
    "reporting",
)
TERMINAL_JOB_STATUSES: Final[tuple[JobStatus, ...]] = ("ready", "failed", "partial_ready")
INDEX_JOB_LOCK_ID: Final = "index:overlay"
INDEX_TASK_CHECKPOINT_SCHEMA_VERSION: Final = "index_task_checkpoint.v1"
_JOB_STATUS_TRANSITIONS: Final[dict[JobStatus, tuple[JobStatus, ...]]] = {
    "pending": ("discovering", "failed"),
    "discovering": ("parsing", "failed", "partial_ready"),
    "parsing": ("extracting", "failed", "partial_ready"),
    "extracting": ("embedding", "failed", "partial_ready"),
    "embedding": ("reporting", "failed", "partial_ready"),
    "reporting": ("ready", "failed", "partial_ready"),
    "ready": (),
    "failed": ("pending",),
    "partial_ready": ("pending",),
}


class JobStateTransitionError(RuntimeError):
    """Raised when a job status transition violates the state machine."""


class JobLockConflictError(RuntimeError):
    """Raised when another job owns an unexpired write lock."""


@dataclass(frozen=True)
class JobLockLease:
    """One SQLite-backed job lock lease."""

    lock_id: str
    owner_job_id: str
    acquired_at: str
    expires_at: str | None
    metadata: StorageMetadata = field(default_factory=dict)


@dataclass(frozen=True)
class JobResumeState:
    """Resume payload for a pending or interrupted job."""

    job: JobRecord
    checkpoints: dict[str, str]


@dataclass(frozen=True)
class IndexJobRunResult:
    """Result of one lightweight index job execution."""

    job: JobRecord
    parsed_files: tuple[str, ...]
    failed_files: tuple[str, ...]


@dataclass(frozen=True)
class IndexTaskCheckpoint:
    """One persisted task checkpoint written only after the corresponding commit."""

    schema_version: str
    status: Literal["collected", "applied"]
    task_key: str
    phase: str
    input_hash: str
    task_schema_version: str
    updated_at: str
    metadata: StorageMetadata = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "task_key": self.task_key,
            "phase": self.phase,
            "input_hash": self.input_hash,
            "task_schema_version": self.task_schema_version,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


class SQLiteJobStore:
    """SQLite implementation for job lifecycle, checkpoints, and write locks."""

    def __init__(self, jobs_path: Path) -> None:
        self._jobs_path = jobs_path

    def create_job(
        self,
        *,
        job_type: str = "index",
        write_target: StorageWriteTarget = "overlay",
        snapshot_id: str | None = "current",
        profile_id: str | None = ALL_SCOPE,
        job_id: str | None = None,
        metadata: StorageMetadata | None = None,
    ) -> JobRecord:
        now = utc_now()
        record = JobRecord(
            job_id=job_id or new_job_id(job_type),
            job_type=job_type,
            status="pending",
            write_target=write_target,
            snapshot_id=snapshot_id,
            profile_id=profile_id,
            created_at=now,
            updated_at=now,
            metadata={} if metadata is None else dict(metadata),
        )
        with sqlite_connection(self._jobs_path) as connection:
            upsert_row(connection, "job", job_record_values(record))
            connection.commit()
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        with sqlite_connection(self._jobs_path) as connection:
            row = connection.execute(
                "SELECT * FROM job WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
        return None if row is None else row_to_job_record(cast(sqlite3.Row, row))

    def transition_job(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error_summary: str | None = None,
        metadata_update: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        with sqlite_connection(self._jobs_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM job WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"job {job_id!r} does not exist")
            current = row_to_job_record(cast(sqlite3.Row, row))
            if status not in _JOB_STATUS_TRANSITIONS[current.status]:
                connection.rollback()
                raise JobStateTransitionError(
                    f"invalid job transition {current.status!r} -> {status!r}"
                )
            metadata = dict(current.metadata)
            if metadata_update:
                metadata.update(cast(StorageMetadata, dict(metadata_update)))
            updated = JobRecord(
                job_id=current.job_id,
                job_type=current.job_type,
                status=status,
                write_target=current.write_target,
                created_at=current.created_at,
                updated_at=utc_now(),
                snapshot_id=current.snapshot_id,
                profile_id=current.profile_id,
                error_summary=error_summary,
                metadata=metadata,
            )
            upsert_row(connection, "job", job_record_values(updated))
            connection.commit()
        return updated

    def set_checkpoint(self, job_id: str, key: str, value: str) -> None:
        with sqlite_connection(self._jobs_path) as connection:
            connection.execute(
                """
                INSERT INTO job_checkpoint (
                  job_id,
                  checkpoint_key,
                  checkpoint_value,
                  updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, checkpoint_key) DO UPDATE SET
                  checkpoint_value = excluded.checkpoint_value,
                  updated_at = excluded.updated_at
                """,
                (job_id, key, value, utc_now()),
            )
            connection.commit()

    def get_checkpoint(self, job_id: str, key: str) -> str | None:
        with sqlite_connection(self._jobs_path) as connection:
            row = connection.execute(
                """
                SELECT checkpoint_value
                FROM job_checkpoint
                WHERE job_id = ? AND checkpoint_key = ?
                LIMIT 1
                """,
                (job_id, key),
            ).fetchone()
        return None if row is None else str(row["checkpoint_value"])

    def get_checkpoints(self, job_id: str) -> dict[str, str]:
        with sqlite_connection(self._jobs_path) as connection:
            rows = connection.execute(
                """
                SELECT checkpoint_key, checkpoint_value
                FROM job_checkpoint
                WHERE job_id = ?
                ORDER BY checkpoint_key ASC
                """,
                (job_id,),
            ).fetchall()
        return {str(row["checkpoint_key"]): str(row["checkpoint_value"]) for row in rows}

    def resume_job(self, job_id: str) -> JobResumeState:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"job {job_id!r} does not exist")
        return JobResumeState(job=job, checkpoints=self.get_checkpoints(job_id))

    def retry_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"job {job_id!r} does not exist")
        if job.status not in {"failed", "partial_ready"}:
            raise JobStateTransitionError(f"job {job_id!r} is not retryable from {job.status!r}")
        retry_count = int_value(job.metadata.get("retry_count")) + 1
        return self.transition_job(
            job_id,
            "pending",
            metadata_update={"retry_count": retry_count},
        )

    def acquire_lock(
        self,
        lock_id: str,
        *,
        owner_job_id: str,
        ttl_seconds: int = 3600,
        metadata: StorageMetadata | None = None,
    ) -> JobLockLease:
        now = datetime.now(UTC)
        acquired_at = format_timestamp(now)
        expires_at = format_timestamp(now + timedelta(seconds=ttl_seconds))
        with sqlite_connection(self._jobs_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM job_lock WHERE lock_id = ? LIMIT 1",
                (lock_id,),
            ).fetchone()
            if row is not None:
                existing_owner = str(row["owner_job_id"])
                existing_expires_at = optional_text(row["expires_at"])
                if existing_owner != owner_job_id and not lock_expired(
                    existing_expires_at,
                    now,
                ):
                    connection.rollback()
                    raise JobLockConflictError(
                        f"lock {lock_id!r} is already owned by job {existing_owner!r}"
                    )
            values = {
                "lock_id": lock_id,
                "owner_job_id": owner_job_id,
                "acquired_at": acquired_at,
                "expires_at": expires_at,
                "metadata_json": encode_metadata({} if metadata is None else dict(metadata)),
            }
            upsert_row(connection, "job_lock", values)
            connection.commit()
        return JobLockLease(
            lock_id=lock_id,
            owner_job_id=owner_job_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
            metadata={} if metadata is None else dict(metadata),
        )

    def release_lock(self, lock_id: str, *, owner_job_id: str) -> bool:
        with sqlite_connection(self._jobs_path) as connection:
            cursor = connection.execute(
                "DELETE FROM job_lock WHERE lock_id = ? AND owner_job_id = ?",
                (lock_id, owner_job_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def get_lock(self, lock_id: str) -> JobLockLease | None:
        with sqlite_connection(self._jobs_path) as connection:
            row = connection.execute(
                "SELECT * FROM job_lock WHERE lock_id = ? LIMIT 1",
                (lock_id,),
            ).fetchone()
        if row is None:
            return None
        return JobLockLease(
            lock_id=str(row["lock_id"]),
            owner_job_id=str(row["owner_job_id"]),
            acquired_at=str(row["acquired_at"]),
            expires_at=optional_text(row["expires_at"]),
            metadata=decode_metadata(row["metadata_json"]),
        )


class IndexJobRunner:
    """Small resumable index runner used by ops and tests before full pipeline lands."""

    def __init__(
        self,
        store: SQLiteJobStore,
        *,
        lock_id: str = INDEX_JOB_LOCK_ID,
        lock_ttl_seconds: int = 3600,
    ) -> None:
        self._store = store
        self._lock_id = lock_id
        self._lock_ttl_seconds = lock_ttl_seconds

    def run_files(
        self,
        job_id: str,
        files: Sequence[str],
        parse_file: Callable[[str], None],
    ) -> IndexJobRunResult:
        self._store.acquire_lock(
            self._lock_id,
            owner_job_id=job_id,
            ttl_seconds=self._lock_ttl_seconds,
            metadata={"job_type": "index"},
        )
        parsed: list[str] = []
        failed: dict[str, str] = {}
        try:
            self._store.transition_job(job_id, "discovering")
            self._store.set_checkpoint(job_id, "discovered_files", encode_json_list(files))

            self._store.transition_job(job_id, "parsing")
            for path in files:
                try:
                    parse_file(path)
                except Exception as exc:  # noqa: BLE001 - parser errors are per-file degradations.
                    failed[path] = str(exc)
                    continue
                parsed.append(path)
            self._store.set_checkpoint(job_id, "parsed_files", encode_json_list(parsed))
            self._store.set_checkpoint(job_id, "failed_files", encode_json_mapping(failed))

            if failed:
                status: Literal["partial_ready", "failed"] = "partial_ready" if parsed else "failed"
                job = self._store.transition_job(
                    job_id,
                    status,
                    error_summary=first_error_summary(failed),
                    metadata_update={
                        "files_total": len(files),
                        "files_parsed": len(parsed),
                        "files_failed": len(failed),
                    },
                )
                return IndexJobRunResult(
                    job=job,
                    parsed_files=tuple(parsed),
                    failed_files=tuple(sorted(failed)),
                )

            self._store.transition_job(job_id, "extracting")
            self._store.transition_job(job_id, "embedding")
            self._store.transition_job(job_id, "reporting")
            job = self._store.transition_job(
                job_id,
                "ready",
                metadata_update={
                    "files_total": len(files),
                    "files_parsed": len(parsed),
                    "files_failed": 0,
                },
            )
            return IndexJobRunResult(job=job, parsed_files=tuple(parsed), failed_files=())
        finally:
            self._store.release_lock(self._lock_id, owner_job_id=job_id)


def new_job_id(job_type: str) -> str:
    return f"{job_type}:{uuid.uuid4().hex}"


def task_checkpoint_key(
    task: IndexTask | str,
    *,
    status: Literal["collected", "applied"] = "applied",
) -> str:
    """Return the job_checkpoint key for one task checkpoint status."""

    task_key = task.task_key if isinstance(task, IndexTask) else str(task)
    return f"task:{status}:{task_key}"


def record_task_collected_checkpoint(
    store: SQLiteJobStore,
    job_id: str,
    task: IndexTask,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record collect artifact availability; this is not a query visibility boundary."""

    _record_task_checkpoint(
        store,
        job_id,
        task,
        status="collected",
        metadata=metadata,
    )


def record_task_applied_checkpoint(
    store: SQLiteJobStore,
    job_id: str,
    task: IndexTask,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record that a task can be skipped; call only after metadata/vector commit succeeds."""

    _record_task_checkpoint(
        store,
        job_id,
        task,
        status="applied",
        metadata=metadata,
    )


def task_has_applied_checkpoint(
    checkpoints: Mapping[str, str],
    task: IndexTask,
) -> bool:
    """Return True only when a matching applied checkpoint exists for the task."""

    payload = decode_task_checkpoint(checkpoints.get(task_checkpoint_key(task)))
    return (
        payload is not None
        and payload.status == "applied"
        and payload.task_key == task.task_key
        and payload.phase == task.phase
        and payload.input_hash == task.input_hash
        and payload.task_schema_version == task.schema_version
    )


def encode_task_checkpoint(checkpoint: IndexTaskCheckpoint) -> str:
    return json.dumps(checkpoint.to_dict(), ensure_ascii=True, sort_keys=True)


def decode_task_checkpoint(value: str | None) -> IndexTaskCheckpoint | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    status = payload.get("status")
    if status not in {"collected", "applied"}:
        return None
    task_key = payload.get("task_key")
    phase = payload.get("phase")
    input_hash = payload.get("input_hash")
    task_schema_version = payload.get("task_schema_version")
    updated_at = payload.get("updated_at")
    if not all(
        isinstance(item, str)
        for item in (task_key, phase, input_hash, task_schema_version, updated_at)
    ):
        return None
    metadata = payload.get("metadata")
    return IndexTaskCheckpoint(
        schema_version=str(payload.get("schema_version", "")),
        status=cast(Literal["collected", "applied"], status),
        task_key=cast(str, task_key),
        phase=cast(str, phase),
        input_hash=cast(str, input_hash),
        task_schema_version=cast(str, task_schema_version),
        updated_at=cast(str, updated_at),
        metadata=decode_metadata(json.dumps(metadata if isinstance(metadata, Mapping) else {})),
    )


def _record_task_checkpoint(
    store: SQLiteJobStore,
    job_id: str,
    task: IndexTask,
    *,
    status: Literal["collected", "applied"],
    metadata: Mapping[str, Any] | None = None,
) -> None:
    checkpoint = IndexTaskCheckpoint(
        schema_version=INDEX_TASK_CHECKPOINT_SCHEMA_VERSION,
        status=status,
        task_key=task.task_key,
        phase=task.phase,
        input_hash=task.input_hash,
        task_schema_version=task.schema_version,
        updated_at=utc_now(),
        metadata={} if metadata is None else cast(StorageMetadata, dict(metadata)),
    )
    store.set_checkpoint(
        job_id,
        task_checkpoint_key(task, status=status),
        encode_task_checkpoint(checkpoint),
    )


def lock_expired(expires_at: str | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    return parse_timestamp(expires_at) <= now


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def encode_json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=True, sort_keys=True)


def encode_json_mapping(values: Mapping[str, str]) -> str:
    return json.dumps(dict(values), ensure_ascii=True, sort_keys=True)


def first_error_summary(failed: Mapping[str, str]) -> str:
    first_path = sorted(failed)[0]
    return f"{first_path}: {failed[first_path]}"
