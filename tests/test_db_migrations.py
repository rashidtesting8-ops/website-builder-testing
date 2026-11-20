"""TDD tests: explicit SQLite schema versioning and idempotent migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import (
    ProjectRecord,
    connect_db,
    init_db,
    upsert_project,
)
from app.migrations import SCHEMA_VERSION, migrate_schema, schema_version


def _legacy_db(path: Path) -> sqlite3.Connection:
    """Simulate a pre-migration DB: baseline schema.sql only, no schema_meta."""
    conn = connect_db(path)
    schema = Path("app/schema.sql").read_text(encoding="utf-8")
    for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
        conn.execute(stmt)
    return conn


def test_migration_versions_legacy_db_without_losing_data(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    conn = _legacy_db(db)
    try:
        upsert_project(
            conn,
            ProjectRecord(
                project_id="legacy-salon",
                business_name="Legacy Salon",
                primary_url="https://legacy.example/",
                status="approved",
            ),
        )
        assert schema_version(conn) == 0
        applied = migrate_schema(conn)
        assert applied == SCHEMA_VERSION
        assert schema_version(conn) == SCHEMA_VERSION

        rows = [dict(r) for r in conn.execute("SELECT project_id, business_name FROM projects")]
        assert rows == [{"project_id": "legacy-salon", "business_name": "Legacy Salon"}]
        audit = [dict(r) for r in conn.execute("SELECT version FROM migration_audit")]
        assert audit == [{"version": 1}]
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "twice.sqlite3")
    try:
        init_db(conn)
        first = migrate_schema(conn)
        second = migrate_schema(conn)
        assert first == second == SCHEMA_VERSION
        count = conn.execute("SELECT COUNT(*) FROM migration_audit").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_init_db_leaves_schema_at_current_version(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "fresh.sqlite3")
    try:
        init_db(conn)
        assert schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_future_schema_version_is_not_silently_downgraded(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "future.sqlite3")
    try:
        init_db(conn)
        with sqlite3.connect(tmp_path / "future.sqlite3") as raw:
            raw.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
        assert schema_version(conn) == 99
        try:
            migrate_schema(conn)
        except RuntimeError as exc:
            assert "99" in str(exc)
        else:
            raise AssertionError("Future schema version must be rejected")
    finally:
        conn.close()
