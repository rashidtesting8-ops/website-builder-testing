"""Auditable rights confirmation workflow and append-only audit trail for EditVista."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.db import (
    AssetProvenanceRecord,
    ProjectRecord,
    connect_db,
    get_project,
    init_db,
    list_assets_for_project,
    upsert_asset_provenance,
    upsert_project,
)
from engine.contracts import freeze_demo_metadata, read_demo_metadata

RightsBasis = Literal["client_provided", "owner_permission", "licensed"]
ALLOWED_BASES: set[str] = {"client_provided", "owner_permission", "licensed"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _make_writable(target: Path) -> None:
    if target.is_file():
        try:
            os.chmod(target, target.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _make_writable(target)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        _make_writable(target)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _resolve_project_root(
    project_id_or_path: str | Path,
    projects_dir: str | Path = "./projects",
) -> Path:
    candidate = Path(project_id_or_path)
    if candidate.is_dir():
        return candidate.resolve()
    return (Path(projects_dir).resolve() / str(project_id_or_path)).resolve()


def confirm_asset_rights(
    project_id_or_path: str | Path,
    *,
    asset: str | None = None,
    all_assets: bool = False,
    basis: str,
    operator: str,
    evidence: str,
    projects_dir: str | Path = "./projects",
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Confirm rights for assets, updating audit log, manifest, and DB atomically."""
    clean_operator = (operator or "").strip()
    if not clean_operator:
        raise ValueError("operator must not be empty or whitespace")
    if clean_operator.lower() == "dev":
        raise ValueError("operator 'Dev' is rejected; human reviewer signature required")

    clean_basis = (basis or "").strip().lower()
    if clean_basis not in ALLOWED_BASES:
        raise ValueError(
            f"Invalid rights basis '{basis}'. Must be one of: {sorted(ALLOWED_BASES)}"
        )

    clean_evidence = (evidence or "").strip()
    if not clean_evidence:
        raise ValueError("evidence must not be empty or whitespace")

    if not all_assets and not (asset and asset.strip()):
        raise ValueError("Either --asset <sha256-or-file> or --all must be specified")

    project_root = _resolve_project_root(project_id_or_path, projects_dir)
    if not project_root.is_dir():
        raise ValueError(f"Project directory not found: {project_root}")

    demo = read_demo_metadata(project_root)
    if demo is not None and demo.is_fixture and demo.kind == "third_party_internal_demo":
        raise ValueError(
            "Rights confirmation is blocked for third-party internal demo projects"
        )
    freeze_demo_metadata(project_root)

    project_id = project_root.name
    manifest_path = project_root / "research" / "asset-manifest.json"
    audit_path = project_root / "research" / "rights-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_db_path = (
        Path(db_path).resolve()
        if db_path is not None
        else (project_root.parent / "editvista.sqlite3")
    )
    conn = connect_db(resolved_db_path)
    init_db(conn)
    if get_project(conn, project_id) is None:
        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_id,
                business_name=project_id.replace("-", " ").title(),
                primary_url="https://local.invalid/project",
                status="draft",
            ),
        )

    manifest_items: list[dict[str, Any]] = []
    if manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                manifest_items = raw
        except Exception:
            manifest_items = []

    existing_db_records = {r.asset_id: r for r in list_assets_for_project(conn, project_id)}

    confirmed_entries: list[dict[str, Any]] = []
    target_clean = (asset or "").strip()

    # Find candidates from manifest or SQLite
    matched_count = 0
    now_iso = _utc_now().isoformat()

    for item in manifest_items:
        rel_file = str(item.get("file") or "")
        item_sha = str(item.get("sha256") or "")
        file_name = Path(rel_file).name

        matches = all_assets or (
            target_clean in (item_sha, rel_file, file_name)
            or (item_sha and target_clean.startswith(item_sha[:12]))
        )
        if not matches:
            continue

        matched_count += 1
        # Determine actual file on disk to verify current bytes
        disk_path = project_root / rel_file
        if disk_path.is_file():
            actual_sha = hashlib.sha256(disk_path.read_bytes()).hexdigest()
        else:
            actual_sha = item_sha

        item["rights_confirmed_for_preview"] = True
        item["sha256"] = actual_sha
        item["rights_basis"] = clean_basis
        item["rights_operator"] = clean_operator
        item["rights_evidence"] = clean_evidence

        audit_entry = {
            "who": clean_operator,
            "when": now_iso,
            "basis": clean_basis,
            "evidence": clean_evidence,
            "asset_sha256": actual_sha,
            "sha256": actual_sha,
            "file": rel_file,
        }
        confirmed_entries.append(audit_entry)

        # Update SQLite
        asset_id = file_name or rel_file
        source_url = str(item.get("url") or item.get("source_url") or "")
        existing_rec = existing_db_records.get(asset_id)
        if existing_rec:
            updated_rec = AssetProvenanceRecord(
                asset_id=existing_rec.asset_id,
                project_id=project_id,
                sha256_hash=actual_sha,
                phash=existing_rec.phash,
                source_url=existing_rec.source_url,
                local_path=existing_rec.local_path,
                rights_confirmed_for_preview=True,
            )
        else:
            updated_rec = AssetProvenanceRecord(
                asset_id=asset_id,
                project_id=project_id,
                sha256_hash=actual_sha,
                source_url=source_url,
                local_path=rel_file,
                rights_confirmed_for_preview=True,
            )
        upsert_asset_provenance(conn, updated_rec)

    conn.close()

    if not all_assets and matched_count == 0:
        raise ValueError(f"No asset found matching '{target_clean}' in project '{project_id}'")

    # Append to research/rights-audit.jsonl (append-only)
    with audit_path.open("a", encoding="utf-8") as f:
        for entry in confirmed_entries:
            f.write(json.dumps(entry) + "\n")

    # Atomically write updated asset-manifest.json
    _atomic_write_text(manifest_path, json.dumps(manifest_items, indent=2))

    return confirmed_entries


def read_rights_audit_log(project_root: Path) -> list[dict[str, Any]]:
    """Read all entries from research/rights-audit.jsonl."""
    audit_path = project_root / "research" / "rights-audit.jsonl"
    if not audit_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, dict):
                entries.append(parsed)
        except Exception:
            continue
    return entries


def check_used_assets_audit_signatures(
    project_root: Path,
    used_asset_rel_paths: list[str],
) -> list[dict[str, Any]]:
    """Check that each used asset has a matching audit entry for its CURRENT sha256 on disk."""
    audit_entries = read_rights_audit_log(project_root)
    # Map valid confirmed sha256 values
    confirmed_shas = {
        str(e.get("asset_sha256") or e.get("sha256") or "")
        for e in audit_entries
    }

    unconfirmed_or_tampered: list[dict[str, Any]] = []

    for rel_path in used_asset_rel_paths:
        disk_path = project_root / rel_path
        if not disk_path.is_file():
            # Might be in subfolder
            candidates = [
                project_root / "assets" / "optimized" / Path(rel_path).name,
                project_root / "assets" / "website" / Path(rel_path).name,
            ]
            for cand in candidates:
                if cand.is_file():
                    disk_path = cand
                    break

        if not disk_path.is_file():
            unconfirmed_or_tampered.append(
                {
                    "file": rel_path,
                    "reason": "File does not exist on disk",
                }
            )
            continue

        current_sha = hashlib.sha256(disk_path.read_bytes()).hexdigest()
        if current_sha not in confirmed_shas:
            unconfirmed_or_tampered.append(
                {
                    "file": rel_path,
                    "current_sha256": current_sha,
                    "reason": "No matching audit entry in rights-audit.jsonl for CURRENT sha256",
                }
            )

    return unconfirmed_or_tampered

