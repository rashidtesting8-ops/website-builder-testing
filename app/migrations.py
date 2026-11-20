"""Explicit SQLite schema versioning and forward-only migrations.

`app/schema.sql` remains the idempotent baseline. `schema_meta.schema_version`
tracks the applied migration number; `migration_audit` records each applied
migration. A newer-than-supported version is rejected (never downgraded).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from app.db import immediate_transaction

SCHEMA_VERSION = 1

# Each migration is a list of idempotent statements applied once when the
# database moves from version N-1 to version N.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE TABLE IF NOT EXISTS migration_audit ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    ),
}


def ensure_schema_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the applied schema version; 0 means a legacy pre-migration DB."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations inside an existing transaction."""
    from datetime import UTC, datetime

    ensure_schema_meta(conn)
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than supported {SCHEMA_VERSION}; "
            "refusing to downgrade"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for stmt in MIGRATIONS.get(version, ()):
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO migration_audit (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )
    return SCHEMA_VERSION


def migrate_schema(conn: sqlite3.Connection) -> int:
    """Apply pending migrations atomically and return the resulting version."""
    with immediate_transaction(conn):
        return _apply_migrations(conn)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or migrate EditVista SQLite schema")
    parser.add_argument("--db", default="projects/editvista.sqlite3")
    parser.add_argument("--migrate", action="store_true", help="Apply pending migrations")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.is_file():
        print(json.dumps({"db": str(path), "exists": False, "version": 0}))
        return 0
    from app.db import connect_db

    conn = connect_db(path)
    try:
        before = schema_version(conn)
        after = migrate_schema(conn) if args.migrate else before
        print(json.dumps({"db": str(path), "exists": True, "before": before, "version": after}))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
