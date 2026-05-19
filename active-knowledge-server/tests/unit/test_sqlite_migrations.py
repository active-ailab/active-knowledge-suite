from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import ResolvedConfig, resolve_config
from active_knowledge_server.storage.sqlite_store import (
    LATEST_SQLITE_SCHEMA_VERSION,
    SQLiteMigrationError,
    SQLiteMigrationStep,
    SQLiteTarget,
    configured_sqlite_paths,
    migrate_local_sqlite_stores,
    migrate_sqlite_store,
    plan_sqlite_migration,
)


def table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def schema_version(path: Path, *, target: str) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT schema_version FROM schema_version WHERE target = ?",
            (target,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def migration_history_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM migration_history").fetchone()
    assert row is not None
    return int(row[0])


def legacy_overlay_store(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_version (
              target TEXT PRIMARY KEY,
              schema_version TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO schema_version(target, schema_version, updated_at)
            VALUES (?, ?, ?)
            """,
            ("overlay_metadata", "0.9.0", "2026-05-19T00:00:00Z"),
        )
        connection.execute("CREATE TABLE legacy_marker (marker TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO legacy_marker(marker) VALUES ('kept')")
        connection.commit()


def resolve_with_workdir(tmp_path: Path) -> ResolvedConfig:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    return resolve_config(
        cli_overrides={
            "runtime": {
                "workdir": str(workdir),
                "source_docs_root": str(source_docs),
            },
            "project": {"workspace_root": str(workspace)},
        },
        env={},
        cwd=tmp_path,
    )


@pytest.mark.parametrize(
    ("target", "expected_tables"),
    (
        (
            "baseline_metadata",
            {"source", "snapshot", "profile", "file", "chunk", "entity", "relation", "evidence"},
        ),
        (
            "overlay_metadata",
            {
                "source",
                "snapshot",
                "profile",
                "file",
                "chunk",
                "entity",
                "relation",
                "evidence",
                "tombstone",
                "replacement",
            },
        ),
        (
            "jobs",
            {"job", "job_checkpoint", "job_lock"},
        ),
    ),
)
def test_migrate_sqlite_store_creates_expected_schema(
    tmp_path: Path,
    target: SQLiteTarget,
    expected_tables: set[str],
) -> None:
    path = tmp_path / f"{target}.db"

    result = migrate_sqlite_store(path, target=target)

    assert result.created is True
    assert result.applied_migration_ids
    assert schema_version(path, target=target) == LATEST_SQLITE_SCHEMA_VERSION
    assert {"schema_version", "migration_history"}.issubset(table_names(path))
    assert expected_tables.issubset(table_names(path))


def test_migrate_sqlite_store_is_idempotent_across_three_runs(tmp_path: Path) -> None:
    path = tmp_path / "overlay.db"

    first = migrate_sqlite_store(path, target="overlay_metadata")
    second = migrate_sqlite_store(path, target="overlay_metadata")
    third = migrate_sqlite_store(path, target="overlay_metadata")

    assert first.applied_migration_ids == ("overlay_metadata.v1.bootstrap",)
    assert second.applied_migration_ids == ()
    assert third.applied_migration_ids == ()
    assert migration_history_count(path) == 1


def test_migrate_sqlite_store_supports_dry_run_without_creating_db(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"

    result = migrate_sqlite_store(path, target="jobs", dry_run=True)

    assert result.pending_migration_ids == ("jobs.v1.bootstrap",)
    assert not path.exists()


def test_local_major_migration_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "overlay.db"
    legacy_overlay_store(path)

    result = migrate_sqlite_store(path, target="overlay_metadata")

    assert result.backup_ref is not None
    assert result.backup_ref.exists()
    assert "legacy_marker" in table_names(result.backup_ref)
    assert schema_version(path, target="overlay_metadata") == LATEST_SQLITE_SCHEMA_VERSION


def test_baseline_major_migration_requires_confirmation(tmp_path: Path) -> None:
    path = tmp_path / "metadata.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_version (
              target TEXT PRIMARY KEY,
              schema_version TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO schema_version(target, schema_version, updated_at)
            VALUES (?, ?, ?)
            """,
            ("baseline_metadata", "0.9.0", "2026-05-19T00:00:00Z"),
        )
        connection.commit()

    with pytest.raises(SQLiteMigrationError, match="confirm_major=True"):
        migrate_sqlite_store(path, target="baseline_metadata")


def test_failed_migration_rolls_back_existing_db(tmp_path: Path) -> None:
    path = tmp_path / "overlay.db"
    legacy_overlay_store(path)
    failing_step = SQLiteMigrationStep(
        migration_id="overlay_metadata.v1.fail",
        target="overlay_metadata",
        from_versions=("0.9.0",),
        to_version=LATEST_SQLITE_SCHEMA_VERSION,
        statements=("CREATE TABLE broken(",),
    )

    with pytest.raises(SQLiteMigrationError, match="failed to migrate overlay_metadata"):
        migrate_sqlite_store(
            path,
            target="overlay_metadata",
            steps_override=(failing_step,),
        )

    assert "legacy_marker" in table_names(path)
    assert "source" not in table_names(path)
    assert schema_version(path, target="overlay_metadata") == "0.9.0"


def test_migrate_local_sqlite_stores_from_config(tmp_path: Path) -> None:
    resolved = resolve_with_workdir(tmp_path)
    paths = configured_sqlite_paths(resolved.model, cwd=tmp_path)

    overlay_result, jobs_result = migrate_local_sqlite_stores(resolved.model, cwd=tmp_path)

    assert overlay_result.path == paths["overlay_metadata"]
    assert jobs_result.path == paths["jobs"]
    assert overlay_result.path.exists()
    assert jobs_result.path.exists()


def test_plan_sqlite_migration_reports_pending_steps(tmp_path: Path) -> None:
    path = tmp_path / "overlay.db"

    plan = plan_sqlite_migration(path, target="overlay_metadata")

    assert plan.current_version is None
    assert tuple(step.migration_id for step in plan.pending_steps) == (
        "overlay_metadata.v1.bootstrap",
    )
    assert plan.requires_backup is False
