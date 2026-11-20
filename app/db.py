"""SQLite data access layer, PRAGMA hardening, BEGIN IMMEDIATE transactions, and Pydantic models."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

REQUIRED_PRAGMAS: tuple[str, ...] = (
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA temp_store = MEMORY;",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_tz_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value


def _ensure_non_blank(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty or whitespace")
    return cleaned


class ProjectRecord(BaseModel):
    """Pydantic v2 model for the `projects` table."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    business_name: str
    primary_url: str
    status: str
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("project_id", "business_name", "primary_url", "status")
    @classmethod
    def _validate_non_blank(cls, v: str, info: Any) -> str:
        return _ensure_non_blank(v, str(info.field_name))

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, v: datetime, info: Any) -> datetime:
        return _ensure_tz_aware(v, str(info.field_name))


class JobRunRecord(BaseModel):
    """Pydantic v2 model for the `job_runs` table."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    project_id: str
    module_name: str
    status: str
    idempotency_key: str
    error_message: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("run_id", "project_id", "module_name", "status", "idempotency_key")
    @classmethod
    def _validate_non_blank(cls, v: str, info: Any) -> str:
        return _ensure_non_blank(v, str(info.field_name))

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, v: datetime, info: Any) -> datetime:
        return _ensure_tz_aware(v, str(info.field_name))


class AssetProvenanceRecord(BaseModel):
    """Pydantic v2 model for the `asset_provenance` table."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    project_id: str
    sha256_hash: str
    phash: str | None = None
    source_url: str
    local_path: str
    rights_confirmed_for_preview: bool = False

    @field_validator("asset_id", "project_id", "sha256_hash", "source_url", "local_path")
    @classmethod
    def _validate_non_blank(cls, v: str, info: Any) -> str:
        return _ensure_non_blank(v, str(info.field_name))


class BusinessFactRecord(BaseModel):
    """Pydantic v2 model for the `business_facts` table."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    project_id: str
    category: str
    claim_key: str
    claim_value: str
    source_url: str
    source_text_snippet: str
    is_verified: bool = False

    @field_validator(
        "fact_id",
        "project_id",
        "category",
        "claim_key",
        "claim_value",
        "source_url",
        "source_text_snippet",
    )
    @classmethod
    def _validate_non_blank(cls, v: str, info: Any) -> str:
        return _ensure_non_blank(v, str(info.field_name))


def apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Execute mandatory SQLite PRAGMAs on an open connection."""
    for pragma in REQUIRED_PRAGMAS:
        conn.execute(pragma)


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection in manual transaction mode with all required PRAGMAs."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None enables manual BEGIN IMMEDIATE transaction control
    conn = sqlite3.connect(
        str(path),
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    apply_connection_pragmas(conn)
    return conn


@contextmanager
def immediate_transaction(
    conn: sqlite3.Connection,
    *,
    max_attempts: int = 20,
    retry_sleep: float = 0.02,
) -> Iterator[sqlite3.Connection]:
    """Start a write transaction with BEGIN IMMEDIATE to prevent SQLITE_BUSY deadlocks."""
    for attempt in range(max_attempts):
        try:
            conn.execute("BEGIN IMMEDIATE;")
            break
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" in msg or "busy" in msg) and attempt + 1 < max_attempts:
                time.sleep(retry_sleep * (attempt + 1))
                continue
            raise

    try:
        yield conn
        conn.execute("COMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise


def init_db(conn: sqlite3.Connection, schema_path: str | Path = SCHEMA_PATH) -> None:
    """Apply the idempotent baseline schema, then any pending versioned migrations."""
    from app.migrations import _apply_migrations

    sql = Path(schema_path).read_text(encoding="utf-8")
    statements = [stmt.strip() for stmt in sql.split(";") if stmt.strip()]
    with immediate_transaction(conn):
        for stmt in statements:
            conn.execute(stmt)
        _apply_migrations(conn)


def upsert_project(conn: sqlite3.Connection, record: ProjectRecord) -> ProjectRecord:
    """Insert or update a project row inside a BEGIN IMMEDIATE transaction."""
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO projects (
                project_id, business_name, primary_url, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                business_name = excluded.business_name,
                primary_url = excluded.primary_url,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                record.project_id,
                record.business_name,
                record.primary_url,
                record.status,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
    return record


def get_project(conn: sqlite3.Connection, project_id: str) -> ProjectRecord | None:
    """Fetch a single project by project_id."""
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return ProjectRecord(
        project_id=str(row["project_id"]),
        business_name=str(row["business_name"]),
        primary_url=str(row["primary_url"]),
        status=str(row["status"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def list_projects(conn: sqlite3.Connection) -> list[ProjectRecord]:
    """Fetch all projects ordered by updated_at descending."""
    rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
    return [
        ProjectRecord(
            project_id=str(row["project_id"]),
            business_name=str(row["business_name"]),
            primary_url=str(row["primary_url"]),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
        for row in rows
    ]


def upsert_job_run(conn: sqlite3.Connection, record: JobRunRecord) -> JobRunRecord:
    """Insert or update a job_runs row inside a BEGIN IMMEDIATE transaction."""
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO job_runs (
                run_id, project_id, module_name, status,
                idempotency_key, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at
            """,
            (
                record.run_id,
                record.project_id,
                record.module_name,
                record.status,
                record.idempotency_key,
                record.error_message,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
    return record


def get_job_run(conn: sqlite3.Connection, run_id: str) -> JobRunRecord | None:
    """Fetch a single job_run by run_id."""
    row = conn.execute(
        "SELECT * FROM job_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return JobRunRecord(
        run_id=str(row["run_id"]),
        project_id=str(row["project_id"]),
        module_name=str(row["module_name"]),
        status=str(row["status"]),
        idempotency_key=str(row["idempotency_key"]),
        error_message=str(row["error_message"]) if row["error_message"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def mark_running_jobs_interrupted(
    conn: sqlite3.Connection,
    project_id: str | None = None,
) -> int:
    """Mark any job_runs left in 'running' state from a crashed earlier run as 'interrupted'."""
    with immediate_transaction(conn):
        now_str = _utc_now().isoformat()
        if project_id:
            cursor = conn.execute(
                """
                UPDATE job_runs
                SET status = 'interrupted',
                    error_message = COALESCE(error_message, 'Interrupted by prior crashed run'),
                    updated_at = ?
                WHERE status = 'running' AND project_id = ?
                """,
                (now_str, project_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE job_runs
                SET status = 'interrupted',
                    error_message = COALESCE(error_message, 'Interrupted by prior crashed run'),
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now_str,),
            )
        return cursor.rowcount


def upsert_asset_provenance(
    conn: sqlite3.Connection,
    record: AssetProvenanceRecord,
) -> AssetProvenanceRecord:
    """Insert or update an asset_provenance row inside a BEGIN IMMEDIATE transaction."""
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO asset_provenance (
                asset_id, project_id, sha256_hash, phash,
                source_url, local_path, rights_confirmed_for_preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, asset_id) DO UPDATE SET
                sha256_hash = excluded.sha256_hash,
                phash = excluded.phash,
                source_url = excluded.source_url,
                local_path = excluded.local_path,
                rights_confirmed_for_preview = excluded.rights_confirmed_for_preview
            """,
            (
                record.asset_id,
                record.project_id,
                record.sha256_hash,
                record.phash,
                record.source_url,
                record.local_path,
                1 if record.rights_confirmed_for_preview else 0,
            ),
        )
    return record


def list_assets_for_project(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[AssetProvenanceRecord]:
    """List all asset_provenance rows for a given project_id."""
    rows = conn.execute(
        "SELECT * FROM asset_provenance WHERE project_id = ? ORDER BY asset_id",
        (project_id,),
    ).fetchall()
    return [
        AssetProvenanceRecord(
            asset_id=str(r["asset_id"]),
            project_id=str(r["project_id"]),
            sha256_hash=str(r["sha256_hash"]),
            phash=str(r["phash"]) if r["phash"] is not None else None,
            source_url=str(r["source_url"]),
            local_path=str(r["local_path"]),
            rights_confirmed_for_preview=bool(r["rights_confirmed_for_preview"]),
        )
        for r in rows
    ]


def upsert_business_fact(
    conn: sqlite3.Connection,
    record: BusinessFactRecord,
) -> BusinessFactRecord:
    """Insert or update a business_facts row inside a BEGIN IMMEDIATE transaction."""
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO business_facts (
                fact_id, project_id, category, claim_key,
                claim_value, source_url, source_text_snippet, is_verified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET
                category = excluded.category,
                claim_key = excluded.claim_key,
                claim_value = excluded.claim_value,
                source_url = excluded.source_url,
                source_text_snippet = excluded.source_text_snippet,
                is_verified = excluded.is_verified
            """,
            (
                record.fact_id,
                record.project_id,
                record.category,
                record.claim_key,
                record.claim_value,
                record.source_url,
                record.source_text_snippet,
                1 if record.is_verified else 0,
            ),
        )
    return record


def list_facts_for_project(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[BusinessFactRecord]:
    """List all business_facts rows for a given project_id."""
    rows = conn.execute(
        "SELECT * FROM business_facts WHERE project_id = ? ORDER BY fact_id",
        (project_id,),
    ).fetchall()
    return [
        BusinessFactRecord(
            fact_id=str(r["fact_id"]),
            project_id=str(r["project_id"]),
            category=str(r["category"]),
            claim_key=str(r["claim_key"]),
            claim_value=str(r["claim_value"]),
            source_url=str(r["source_url"]),
            source_text_snippet=str(r["source_text_snippet"]),
            is_verified=bool(r["is_verified"]),
        )
        for r in rows
    ]
