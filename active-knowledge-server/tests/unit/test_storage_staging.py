from __future__ import annotations

import argparse
from pathlib import Path

from active_knowledge_server.cli import (
    INDEX_JOB_LOCK_ID,
    build_index_job_payload,
    prepare_nonresumable_index_job,
    resolve_index_resume_policy,
)
from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.storage import (
    resolve_staging_storage_paths,
    staging_job_token,
)


def resolve_test_config(tmp_path: Path):
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
                "path": str(tmp_path / ".active-kb" / "baseline" / "vectors" / "lancedb"),
                "mode": "readwrite",
            },
            "vector_delta": {
                "path": str(tmp_path / ".active-kb" / "local" / "vectors" / "lancedb-delta"),
                "mode": "readwrite",
            },
            "artifacts_root": str(tmp_path / ".active-kb" / "baseline" / "artifacts"),
            "local_artifacts_root": str(tmp_path / ".active-kb" / "local" / "artifacts"),
            "cache_root": str(tmp_path / ".active-kb" / "local" / "cache"),
        },
    }
    return resolve_config(cli_overrides=overrides, env={}, cwd=tmp_path)


def test_staging_job_token_is_filesystem_safe_and_deterministic() -> None:
    token = staging_job_token("index:Full/ZeppOS Smoke")

    assert token == staging_job_token("index:Full/ZeppOS Smoke")
    assert ":" not in token
    assert "/" not in token
    assert token.startswith("index-full-zeppos-smoke-")


def test_resolve_staging_storage_paths_for_baseline_and_overlay(tmp_path: Path) -> None:
    resolved = resolve_test_config(tmp_path)

    baseline = resolve_staging_storage_paths(
        resolved.model,
        cwd=tmp_path,
        target="baseline",
        job_id="index:baseline-stage",
    )
    overlay = resolve_staging_storage_paths(
        resolved.model,
        cwd=tmp_path,
        target="overlay",
        job_id="index:local-stage",
    )

    assert baseline.live.metadata_path == (
        tmp_path / ".active-kb" / "baseline" / "db" / "metadata.db"
    )
    assert baseline.staging.metadata_path.parent == baseline.live.metadata_path.parent
    assert baseline.staging.metadata_path.name.startswith("metadata.staging.")
    assert baseline.staging.metadata_path.suffix == ".db"
    assert baseline.staging.vector_path.parent == baseline.live.vector_path.parent
    assert baseline.staging.vector_path.name.startswith("lancedb.staging.")

    assert overlay.live.metadata_path == tmp_path / ".active-kb" / "local" / "db" / "overlay.db"
    assert overlay.staging.metadata_path.name.startswith("overlay.staging.")
    assert overlay.staging.vector_path.name.startswith("lancedb-delta.staging.")


def test_resolve_staging_storage_paths_reuses_same_paths_for_same_job(tmp_path: Path) -> None:
    resolved = resolve_test_config(tmp_path)

    first = resolve_staging_storage_paths(
        resolved.model,
        cwd=tmp_path,
        target="overlay",
        job_id="index:resume-me",
    )
    second = resolve_staging_storage_paths(
        resolved.model,
        cwd=tmp_path,
        target="overlay",
        job_id="index:resume-me",
    )
    different = resolve_staging_storage_paths(
        resolved.model,
        cwd=tmp_path,
        target="overlay",
        job_id="index:resume-other",
    )

    assert first.job_token == second.job_token
    assert first.staging.metadata_path == second.staging.metadata_path
    assert first.staging.vector_path == second.staging.vector_path
    assert first.staging.metadata_path != different.staging.metadata_path
    assert first.staging.vector_path != different.staging.vector_path


def test_prepare_nonresumable_full_job_persists_staging_storage_metadata(
    tmp_path: Path,
) -> None:
    resolved = resolve_test_config(tmp_path)
    resume_policy = resolve_index_resume_policy(
        argparse.Namespace(
            resume="auto",
            restart=False,
            no_resume=False,
            job_id="index:full-stage-smoke",
        )
    )

    job_context = prepare_nonresumable_index_job(
        resolved,
        mode="full",
        target="local",
        source="all",
        resume_policy=resume_policy,
    )
    try:
        staging = job_context.job.metadata["staging_storage"]
        assert isinstance(staging, dict)
        assert staging["target"] == "overlay"
        assert staging["job_id"] == "index:full-stage-smoke"

        payload = build_index_job_payload(
            resume_policy=resume_policy,
            status="running",
            mode="full",
            target="local",
            source="all",
            job_context=job_context,
        )
        assert payload["staging_storage"] == staging
    finally:
        job_context.store.release_lock(
            INDEX_JOB_LOCK_ID,
            owner_job_id=job_context.job.job_id,
        )
