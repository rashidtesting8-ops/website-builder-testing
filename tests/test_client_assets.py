from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from app.db import ProjectRecord, connect_db, init_db, upsert_project
from app.pipeline import main as pipeline_cli_main
from PIL import Image
from scripts.m1_provenance_audit import audit_project


def test_client_asset_import_deduplicates_and_passes_provenance_audit(
    tmp_path: Path,
    capsys,
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "client-salon"
    (project_root / "research").mkdir(parents=True)
    (project_root / "research" / "asset-manifest.json").write_text("[]", encoding="utf-8")
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps({"facts": []}),
        encoding="utf-8",
    )
    db_path = projects_dir / "editvista.sqlite3"
    conn = connect_db(db_path)
    init_db(conn)
    upsert_project(
        conn,
        ProjectRecord(
            project_id="client-salon",
            business_name="Synthetic Test Salon",
            primary_url="https://example.test/",
            status="needs_review",
        ),
    )
    conn.close()

    source_folder = tmp_path / "client-images"
    source_folder.mkdir()
    Image.new("RGB", (20, 10), (200, 10, 10)).save(source_folder / "hero.png")
    Image.new("RGB", (18, 12), (10, 200, 10)).save(source_folder / "gallery.png")
    Image.new("RGB", (16, 14), (10, 10, 200)).save(source_folder / "detail.png")
    shutil.copyfile(source_folder / "hero.png", source_folder / "z-duplicate.png")

    exit_code = pipeline_cli_main(
        [
            "import-client-assets",
            "--project-id",
            "client-salon",
            "--folder",
            str(source_folder),
            "--projects-dir",
            str(projects_dir),
            "--db-path",
            str(db_path),
            "--supplied-by",
            "Test Client",
        ]
    )
    output = capsys.readouterr()
    result = json.loads(output.out)
    manifest = json.loads(
        (project_root / "research" / "asset-manifest.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert result["assets_imported"] == 3
    assert result["duplicates"] == 1
    assert result["optimized_assets"] == 3
    assert result["contact_sheet"]["images_shown"] == 3
    assert len(manifest) == 3
    assert {row["source"] for row in manifest} == {"client_supplied"}
    assert {row["status"] for row in manifest} == {"client_supplied"}
    assert {row["supplied_by"] for row in manifest} == {"Test Client"}
    assert all(row["rights_confirmed_for_preview"] is False for row in manifest)
    for row in manifest:
        original = source_folder / row["original_filename"]
        imported = project_root / row["file"]
        assert row["original_filename"] in {"detail.png", "gallery.png", "hero.png"}
        assert row["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
        assert row["sha256"] == hashlib.sha256(imported.read_bytes()).hexdigest()
        with Image.open(original) as image:
            assert row["dimensions"] == [image.width, image.height]
        assert datetime.fromisoformat(row["captured_at"]).tzinfo is not None
        assert row["source_url"].startswith("client-supplied://sha256/")
        assert row["source_page"] == row["source_url"]
    assert len(list((project_root / "assets" / "website").iterdir())) == 3
    assert len(list((project_root / "assets" / "optimized").iterdir())) == 3
    assert (project_root / "qa" / "contact-sheet.jpg").is_file()

    report = audit_project(project_root)
    assert report["summary"]["assets_checked"] == 3
    assert report["summary"]["violations"] == 0
