"""Approval-safe backups with verified restore drills (brief §6 retention rule).

Creates a consistent SQLite snapshot via the sqlite backup API (not a raw file
copy), stores every file with SHA-256 in a manifest, verifies archive integrity,
and proves restorability by extracting into an isolated sandbox — never over
live project data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "node_modules"})
_SKIP_SUFFIXES = (".pyc", ".tmp", ".log")
_SKIP_PREFIXES = (".collect_staging_",)
MANIFEST_NAME = "backup-manifest.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _should_skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in _SKIP_DIRS for part in parts):
        return True
    if any(part.startswith(_SKIP_PREFIXES) for part in parts):
        return True
    return rel.endswith(_SKIP_SUFFIXES) or rel in {MANIFEST_NAME}


def create_backup(
    projects_dir: str | Path,
    backup_dir: str | Path,
    *,
    label: str = "manual",
) -> dict[str, Any]:
    """Create a verified, manifest-backed backup of projects + SQLite state."""
    projects = Path(projects_dir).resolve()
    backups = Path(backup_dir).resolve()
    if not projects.is_dir():
        raise ValueError(f"Projects directory not found: {projects}")
    backups.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    token = uuid.uuid4().hex[:6]
    staging = backups / f".staging-{stamp}-{token}"
    staging.mkdir(parents=True)
    files: dict[str, dict[str, Any]] = {}

    try:
        for path in sorted(projects.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(projects).as_posix()
            if _should_skip(rel):
                continue
            # Raw SQLite files are replaced by a consistent snapshot below.
            if rel.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db")):
                continue
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            files[rel] = {"sha256": _sha256_file(dest), "size": dest.stat().st_size}

        for db_path in sorted(projects.glob("*.sqlite3")):
            snapshot = staging / "db" / db_path.name
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            rel = f"db/{db_path.name}"
            files[rel] = {"sha256": _sha256_file(snapshot), "size": snapshot.stat().st_size}

        manifest = {
            "schema": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "label": label,
            "project_id": projects.name,
            "file_count": len(files),
            "files": files,
        }
        manifest_path = staging / MANIFEST_NAME
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

        base = backups / f"editvista-backup-{stamp}-{label}"
        tmp_archive = Path(shutil.make_archive(str(base), "zip", root_dir=staging))
        zip_path = backups / f"{base.name}.zip"
        os.replace(tmp_archive, zip_path)
        sidecar = backups / f"{zip_path.name}.sha256"
        _atomic_write_text(sidecar, _sha256_file(zip_path) + "\n")
        manifest_out = backups / f"{zip_path.name}.manifest.json"
        _atomic_write_text(
            manifest_out, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "zip_path": str(zip_path),
        "sidecar_path": str(sidecar),
        "manifest_path": str(manifest_out),
        "file_count": len(files),
        "created_at": manifest["created_at"],
    }


def _read_manifest_from_zip(zip_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    import zipfile

    errors: list[str] = []
    if not zip_path.is_file():
        return None, [f"Backup archive not found: {zip_path}"]
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if MANIFEST_NAME not in archive.namelist():
                return None, [f"Manifest {MANIFEST_NAME} missing from archive"]
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            for rel, meta in manifest.get("files", {}).items():
                if rel not in archive.namelist():
                    errors.append(f"Missing archived file: {rel}")
                    continue
                actual = _sha256_bytes(archive.read(rel))
                if actual != str(meta.get("sha256", "")):
                    errors.append(f"Checksum mismatch: {rel}")
    except Exception as exc:  # corrupt zip / bad JSON
        return None, [f"Archive unreadable: {type(exc).__name__}: {str(exc)[:160]}"]
    return manifest, errors


def verify_backup(zip_path: str | Path) -> dict[str, Any]:
    """Verify the archive sidecar and every file checksum inside the manifest."""
    path = Path(zip_path).resolve()
    errors: list[str] = []
    sidecar = Path(str(path) + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").strip()
        if expected != _sha256_file(path):
            errors.append("Archive SHA-256 does not match sidecar")
    manifest, manifest_errors = _read_manifest_from_zip(path)
    errors.extend(manifest_errors)
    return {
        "zip_path": str(path),
        "valid": not errors,
        "file_count": int((manifest or {}).get("file_count", 0)),
        "errors": errors,
    }


def restore_drill(zip_path: str | Path, target_dir: str | Path) -> dict[str, Any]:
    """Extract into an isolated sandbox and verify every restored file checksum."""
    import zipfile

    path = Path(zip_path).resolve()
    verification = verify_backup(path)
    if not verification["valid"]:
        return {
            "passed": False,
            "restore_path": "",
            "checked_files": [],
            "errors": verification["errors"],
        }

    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    sandbox = target / f"restore-{stamp}-{uuid.uuid4().hex[:6]}"
    sandbox.mkdir(parents=True)
    manifest, _ = _read_manifest_from_zip(path)

    try:
        with zipfile.ZipFile(path) as archive:
            archive.extractall(sandbox)
        checked: list[dict[str, Any]] = []
        for rel, meta in (manifest or {}).get("files", {}).items():
            restored = sandbox / rel
            match = restored.is_file() and _sha256_file(restored) == str(meta.get("sha256", ""))
            checked.append({"path": rel, "match": bool(match)})
        return {
            "passed": bool(checked) and all(item["match"] for item in checked),
            "restore_path": str(sandbox),
            "checked_files": checked,
            "errors": [],
        }
    except Exception as exc:
        return {
            "passed": False,
            "restore_path": str(sandbox),
            "checked_files": [],
            "errors": [f"Restore failed: {type(exc).__name__}: {str(exc)[:160]}"],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="EditVista backup and restore drill")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--projects-dir", default="./projects")
    create.add_argument("--backup-dir", default="./backups")
    create.add_argument("--label", default="manual")

    verify = sub.add_parser("verify")
    verify.add_argument("archive")

    drill = sub.add_parser("restore-drill")
    drill.add_argument("archive")
    drill.add_argument("--target-dir", default="./backups/_restore-drills")

    args = parser.parse_args()
    if args.command == "create":
        result = create_backup(args.projects_dir, args.backup_dir, label=args.label)
        print(json.dumps(result, indent=2))
    elif args.command == "verify":
        print(json.dumps(verify_backup(args.archive), indent=2))
    else:
        print(json.dumps(restore_drill(args.archive, args.target_dir), indent=2))


if __name__ == "__main__":
    main()
