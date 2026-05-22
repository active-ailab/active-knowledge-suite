from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from active_knowledge_server.storage.sqlite_store import (
    LATEST_SQLITE_SCHEMA_VERSION,
    SQLiteMigrationError,
    SQLiteMigrationStep,
    migrate_sqlite_store,
)


def test_overlay_major_migration_keeps_backup_copy(tmp_path: Path) -> None:
    path = tmp_path / "overlay.db"
    _legacy_overlay_store(path)

    result = migrate_sqlite_store(path, target="overlay_metadata")

    assert result.backup_ref is not None
    assert result.backup_ref.exists()
    assert _schema_version(path, target="overlay_metadata") == LATEST_SQLITE_SCHEMA_VERSION
    assert _table_exists(result.backup_ref, "legacy_marker") is True


def test_failed_overlay_migration_restores_previous_store(tmp_path: Path) -> None:
    path = tmp_path / "overlay.db"
    _legacy_overlay_store(path)
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

    assert _table_exists(path, "legacy_marker") is True
    assert _table_exists(path, "source") is False
    assert _schema_version(path, target="overlay_metadata") == "0.9.0"


def _legacy_overlay_store(path: Path) -> None:
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


def _table_exists(path: Path, name: str) -> bool:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    return row is not None


def _schema_version(path: Path, *, target: str) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT schema_version FROM schema_version WHERE target = ?",
            (target,),
        ).fetchone()
    return None if row is None else str(row[0])
