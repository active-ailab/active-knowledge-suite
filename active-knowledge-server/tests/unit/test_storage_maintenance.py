from __future__ import annotations

import json
import os
from pathlib import Path

from active_knowledge_server.cli import main
from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing.jobs import SQLiteJobStore
from active_knowledge_server.storage import (
    ChunkRecord,
    FileRecord,
    FTSQuery,
    QueryScope,
    ReplacementRecord,
    SnapshotRecord,
    StorageWriteRequest,
    TombstoneRecord,
    activate_published_storage,
    resolve_published_storage_for_job,
    resolve_staging_storage_paths,
)
from active_knowledge_server.storage.maintenance import clean_local_state
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    migrate_sqlite_store,
    sqlite_connection,
)


def resolve_model(tmp_path: Path) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    docs.mkdir()
    overrides: ConfigDict = {
        "runtime": {
            "workdir": str(tmp_path / ".active-kb"),
            "baseline_dir": str(tmp_path / ".active-kb" / "baseline"),
            "local_dir": str(tmp_path / ".active-kb" / "local"),
            "source_docs_root": str(docs),
        },
        "project": {"workspace_root": str(workspace)},
        "storage": {
            "baseline": {
                "manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")
            },
            "metadata": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "db" / "metadata.db"),
                "mode": "readwrite",
            },
            "overlay": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "overlay.db"),
                "mode": "readwrite",
            },
            "jobs": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "jobs.db"),
                "mode": "readwrite",
            },
            "vector": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "vectors"),
                "mode": "readwrite",
            },
            "vector_delta": {
                "path": str(tmp_path / ".active-kb" / "local" / "vectors"),
                "mode": "readwrite",
            },
            "cache_root": str(tmp_path / ".active-kb" / "local" / "cache"),
        },
    }
    return resolve_config(cli_overrides=overrides, env={}, cwd=tmp_path).model


def build_adapter(config: ActiveKnowledgeConfig) -> SQLiteStorageAdapter:
    baseline_path = Path(config.storage.metadata.path)
    overlay_path = Path(config.storage.overlay.path)
    jobs_path = Path(config.storage.jobs.path)
    migrate_sqlite_store(baseline_path, target="baseline_metadata")
    migrate_sqlite_store(overlay_path, target="overlay_metadata")
    migrate_sqlite_store(jobs_path, target="jobs")
    return SQLiteStorageAdapter(
        baseline_metadata_path=baseline_path,
        overlay_metadata_path=overlay_path,
        jobs_path=jobs_path,
    )


def test_clean_cache_and_tmp_preserves_baseline(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    baseline_file = tmp_path / ".active-kb" / "baseline" / "keep.txt"
    cache_file = tmp_path / ".active-kb" / "local" / "cache" / "parser.tmp"
    tmp_file = tmp_path / ".active-kb" / "local" / "tmp" / "batch.tmp"
    baseline_file.parent.mkdir(parents=True)
    cache_file.parent.mkdir(parents=True)
    tmp_file.parent.mkdir(parents=True)
    baseline_file.write_text("baseline", encoding="utf-8")
    cache_file.write_text("cache", encoding="utf-8")
    tmp_file.write_text("tmp", encoding="utf-8")

    report = clean_local_state(config, cwd=tmp_path, clean_cache=True, clean_tmp=True)

    assert baseline_file.read_text(encoding="utf-8") == "baseline"
    assert not cache_file.exists()
    assert not tmp_file.exists()
    assert report.deleted_files == 2


def test_clean_old_jobs_keeps_newest_terminal_jobs_and_active_jobs(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    build_adapter(config)
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    for job_id in ("job-old", "job-mid", "job-new"):
        job = store.create_job(job_id=job_id)
        store.transition_job(job.job_id, "discovering")
        store.transition_job(job.job_id, "failed", error_summary="done")
    active = store.create_job(job_id="job-active")
    store.transition_job(active.job_id, "discovering")
    job_artifacts_root = Path(config.storage.local_artifacts_root) / "index-jobs"
    for job_id in ("job-old", "job-mid", "job-new", "job-active"):
        artifact_dir = job_artifacts_root / job_id / "collect"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "code.json").write_text("{}", encoding="utf-8")

    report = clean_local_state(config, cwd=tmp_path, old_jobs_keep=1)

    remaining = {
        store.resume_job("job-active").job.job_id,
        store.resume_job("job-new").job.job_id,
    }
    assert report.deleted_jobs == 2
    assert remaining == {"job-active", "job-new"}
    assert store.get_job("job-old") is None
    assert store.get_job("job-mid") is None
    assert not (job_artifacts_root / "job-old").exists()
    assert not (job_artifacts_root / "job-mid").exists()
    assert (job_artifacts_root / "job-new").exists()
    assert (job_artifacts_root / "job-active").exists()


def test_clean_old_snapshots_only_removes_overlay_rows(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    baseline_writer = adapter.writer(StorageWriteRequest(target="baseline"))
    baseline_writer.upsert_snapshot(
        SnapshotRecord(
            snapshot_id="baseline-snapshot",
            workspace_revision="base",
            created_at="2026-01-01T00:00:00Z",
        )
    )
    overlay_writer = adapter.writer(StorageWriteRequest(target="overlay"))
    for snapshot_id, created_at in (
        ("old-local", "2026-01-01T00:00:00Z"),
        ("new-local", "2026-02-01T00:00:00Z"),
    ):
        overlay_writer.upsert_snapshot(
            SnapshotRecord(
                snapshot_id=snapshot_id,
                workspace_revision=snapshot_id,
                created_at=created_at,
            )
        )

    report = clean_local_state(config, cwd=tmp_path, old_snapshots_keep=1)

    reader = adapter.reader()
    assert report.deleted_snapshots == 1
    assert reader.get_snapshot("baseline-snapshot") is not None
    assert reader.get_snapshot("new-local") is not None
    assert reader.get_snapshot("old-local") is None


def test_clean_staging_jobs_removes_failed_full_index_staging_artifacts(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    build_adapter(config)
    staging = resolve_staging_storage_paths(
        config,
        cwd=tmp_path,
        target="overlay",
        job_id="index:failed-full-stage",
    )
    store = SQLiteJobStore(Path(config.storage.jobs.path))
    job = store.create_job(job_id=staging.job_id, metadata={"staging_storage": staging.to_dict()})
    store.transition_job(job.job_id, "discovering")
    store.transition_job(job.job_id, "failed", error_summary="boom")

    staging.staging.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    staging.staging.metadata_path.write_text("metadata", encoding="utf-8")
    Path(f"{staging.staging.metadata_path}-wal").write_text("wal", encoding="utf-8")
    Path(f"{staging.staging.metadata_path}-shm").write_text("shm", encoding="utf-8")
    staging.staging.vector_path.mkdir(parents=True)
    (staging.staging.vector_path / "manifest.json").write_text("{}", encoding="utf-8")

    report = clean_local_state(config, cwd=tmp_path, clean_staging_jobs=True)

    assert report.deleted_staging_artifacts == 4
    assert not staging.staging.metadata_path.exists()
    assert not Path(f"{staging.staging.metadata_path}-wal").exists()
    assert not Path(f"{staging.staging.metadata_path}-shm").exists()
    assert not staging.staging.vector_path.exists()
    assert store.get_job(job.job_id) is not None


def test_clean_old_live_versions_keeps_current_pointer_even_when_older(
    tmp_path: Path,
) -> None:
    config = resolve_model(tmp_path)
    live = resolve_staging_storage_paths(
        config,
        cwd=tmp_path,
        target="overlay",
        job_id="index:live-anchor",
    ).live
    versions = {
        "active-old": 10,
        "prune-mid": 20,
        "keep-newest": 30,
    }
    for token, mtime in versions.items():
        published = resolve_published_storage_for_job(
            target="overlay",
            job_id=f"index:{token}",
            publish_token=token,
            metadata_anchor_path=live.metadata_path,
            vector_anchor_path=live.vector_path,
        )
        published.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        published.metadata_path.write_text(token, encoding="utf-8")
        published.vector_path.mkdir(parents=True, exist_ok=True)
        (published.vector_path / "manifest.json").write_text("{}", encoding="utf-8")
        os.utime(published.metadata_path, (mtime, mtime))
        os.utime(published.vector_path, (mtime, mtime))
        if token == "active-old":
            activate_published_storage(published)

    report = clean_local_state(config, cwd=tmp_path, live_versions_keep=1)

    active = resolve_published_storage_for_job(
        target="overlay",
        job_id="index:active-old",
        publish_token="active-old",
        metadata_anchor_path=live.metadata_path,
        vector_anchor_path=live.vector_path,
    )
    newest = resolve_published_storage_for_job(
        target="overlay",
        job_id="index:keep-newest",
        publish_token="keep-newest",
        metadata_anchor_path=live.metadata_path,
        vector_anchor_path=live.vector_path,
    )
    pruned = resolve_published_storage_for_job(
        target="overlay",
        job_id="index:prune-mid",
        publish_token="prune-mid",
        metadata_anchor_path=live.metadata_path,
        vector_anchor_path=live.vector_path,
    )
    assert report.deleted_live_versions == 1
    assert active.metadata_path.exists()
    assert active.vector_path.exists()
    assert newest.metadata_path.exists()
    assert newest.vector_path.exists()
    assert not pruned.metadata_path.exists()
    assert not pruned.vector_path.exists()


def test_compact_overlay_keeps_query_results_stable(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    adapter = build_adapter(config)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    writer.upsert_file(
        FileRecord(
            file_id="file-overlay",
            snapshot_id="current",
            source_id="docs",
            relative_path="docs/runtime.md",
            content_hash="hash:file",
            language="md",
        )
    )
    writer.upsert_chunk(
        ChunkRecord(
            chunk_id="chunk-overlay",
            snapshot_id="current",
            file_id="file-overlay",
            content_hash="hash:chunk",
            chunk_type="doc_section",
            ordinal=0,
            text="Runtime queue handler compact check.",
            metadata={"doc_type": "engineering"},
        )
    )
    writer.upsert_tombstone(
        TombstoneRecord(
            tombstone_id="inactive-ts",
            object_type="chunk",
            object_id="old-chunk",
            reason="deleted",
            created_by_job="job-old",
            snapshot_id="old",
            active=False,
        )
    )
    writer.upsert_replacement(
        ReplacementRecord(
            replacement_id="inactive-rp",
            object_type="chunk",
            old_object_id="old-chunk",
            new_object_id="chunk-overlay",
            reason="chunk_rebuilt",
            created_by_job="job-old",
            scope=QueryScope(snapshot_id="old"),
            active=False,
        )
    )
    before = [
        match.logical_object_id
        for match in adapter.reader().search_fts(
            FTSQuery(
                index_name="doc_fts",
                query="queue handler",
                scope=QueryScope(snapshot_id="current"),
            )
        )
    ]

    report = clean_local_state(config, cwd=tmp_path, compact_overlay=True)
    after = [
        match.logical_object_id
        for match in adapter.reader().search_fts(
            FTSQuery(
                index_name="doc_fts",
                query="queue handler",
                scope=QueryScope(snapshot_id="current"),
            )
        )
    ]

    with sqlite_connection(Path(config.storage.overlay.path)) as connection:
        tombstones = connection.execute("SELECT COUNT(*) FROM tombstone").fetchone()[0]
        replacements = connection.execute("SELECT COUNT(*) FROM replacement").fetchone()[0]

    assert before == ["chunk-overlay"]
    assert after == before
    assert tombstones == 0
    assert replacements == 0
    assert report.compact["deleted_inactive_tombstones"] == 1
    assert report.compact["deleted_inactive_replacements"] == 1


def test_clean_cli_json_is_machine_readable(tmp_path: Path, capsys) -> None:
    workdir = tmp_path / ".active-kb"
    exit_code = main(
        [
            "clean",
            "--workdir",
            str(workdir),
            "--cache",
            "--tmp",
            "--format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["command"] == "clean"
    assert payload["clean_report"]["schema_version"] == "clean_report.v1"
