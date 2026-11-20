"""TDD tests: consistent backups with manifest, integrity verification and restore drill."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.backup import create_backup, restore_drill, verify_backup
from app.db import ProjectRecord, connect_db, init_db, upsert_project


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "projects"
    projects.mkdir()
    project = projects / "paws-spa"
    (project / "delivery").mkdir(parents=True)
    (project / "delivery/approved-homepage.html").write_text(
        "<html><body>Paws Spa</body></html>", encoding="utf-8"
    )
    (project / "qa").mkdir()
    (project / "qa/qa-report.json").write_text('{"passed": true}', encoding="utf-8")

    conn = connect_db(projects / "editvista.sqlite3")
    try:
        init_db(conn)
        upsert_project(
            conn,
            ProjectRecord(
                project_id="paws-spa",
                business_name="Paws Spa",
                primary_url="https://paws.example/",
                status="approved",
            ),
        )
    finally:
        conn.close()
    return projects, tmp_path / "backups"


def test_backup_captures_projects_and_consistent_db_snapshot(tmp_path: Path) -> None:
    projects, backups = _seed(tmp_path)
    result = create_backup(projects, backups, label="test")

    assert result["zip_path"] and Path(result["zip_path"]).is_file()
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    names = set(manifest["files"])
    assert "paws-spa/delivery/approved-homepage.html" in names
    assert "paws-spa/qa/qa-report.json" in names
    assert any(name.endswith(".sqlite3") and name.startswith("db/") for name in names)
    assert result["file_count"] == len(names)
    assert Path(result["sidecar_path"]).read_text(encoding="utf-8").strip() == hashlib.sha256(
        Path(result["zip_path"]).read_bytes()
    ).hexdigest()


def test_verify_backup_detects_corruption(tmp_path: Path) -> None:
    projects, backups = _seed(tmp_path)
    result = create_backup(projects, backups, label="test")
    zip_path = Path(result["zip_path"])

    assert verify_backup(zip_path)["valid"] is True

    payload = bytearray(zip_path.read_bytes())
    # Corrupt a byte inside the zip payload without changing its length.
    payload[80] ^= 0xFF
    zip_path.write_bytes(bytes(payload))
    assert verify_backup(zip_path)["valid"] is False


def test_restore_drill_restores_into_sandbox_and_matches_hashes(tmp_path: Path) -> None:
    projects, backups = _seed(tmp_path)
    result = create_backup(projects, backups, label="test")
    live_before = {
        p.relative_to(projects).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in projects.rglob("*")
        if p.is_file()
    }

    drill = restore_drill(Path(result["zip_path"]), tmp_path / "restore-sandbox")

    assert drill["passed"] is True
    restored_root = Path(drill["restore_path"])
    restored = (restored_root / "paws-spa/delivery/approved-homepage.html").read_text(
        encoding="utf-8"
    )
    assert "Paws Spa" in restored
    assert (restored_root / "db/editvista.sqlite3").is_file()
    assert all(item["match"] for item in drill["checked_files"])

    live_after = {
        p.relative_to(projects).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in projects.rglob("*")
        if p.is_file()
    }
    assert live_after == live_before, "Restore drill must never touch live data"
