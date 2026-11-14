from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
from scripts.m1_provenance_audit import main


def _create_project(root: Path, *, violating: bool) -> None:
    asset_path = root / "assets" / "website" / "hero.png"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 10), color=(30, 60, 90)).save(asset_path)
    sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()

    if violating:
        asset = {
            "file": "assets/website/hero.png",
            "source_url": "https://instagram.com/example/p/post/",
            "originating_page": "https://example.com/gallery",
            "sha256": "0" * 64,
            "captured_at": "2026-11-13T00:00:00Z",
        }
        fact = {
            "claim_value": "Unsupported claim",
            "verified": True,
            "status": "verified",
        }
    else:
        asset = {
            "file": "assets/website/hero.png",
            "url": "https://www.instagram.com/example/p/post/",
            "source_page": "https://example.com/gallery",
            "sha256": sha256,
            "captured_at": "2026-11-13T00:00:00Z",
            "rights_confirmed_for_preview": False,
            "dimensions": [20, 10],
            "date_unverified": True,
        }
        fact = {
            "claim_value": "Example Grooming",
            "source_url": "https://example.com/about",
            "source_text_snippet": "The business is named Example Grooming.",
            "is_verified": True,
            "last_checked_at": "2026-11-13T00:00:00Z",
            "status": "verified",
        }

    research = root / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "asset-manifest.json").write_text(
        json.dumps([asset]), encoding="utf-8"
    )
    (research / "business-facts.json").write_text(
        json.dumps({"facts": [fact]}), encoding="utf-8"
    )


def test_auditor_accepts_complete_provenance_project(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = tmp_path / "complete-project"
    _create_project(project_root, violating=False)

    exit_code = main(["--project-dir", str(project_root)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["summary"] == {
        "assets_checked": 1,
        "assets_uncollected": 0,
        "claims_checked": 1,
        "violations": 0,
    }
    assert "PASS" in captured.err


def test_auditor_reports_failed_and_blocked_assets_as_uncollected(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = tmp_path / "uncollected-project"
    research = project_root / "research"
    research.mkdir(parents=True)
    (research / "asset-manifest.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://example.com/missing.jpg",
                    "source_page": "https://example.com/",
                    "status": "failed",
                    "error": "404 Not Found",
                },
                {
                    "url": "https://static.example.com/blocked.jpg",
                    "source_page": "https://example.com/",
                    "status": "blocked_by_robots",
                },
            ]
        ),
        encoding="utf-8",
    )
    (research / "business-facts.json").write_text(
        json.dumps({"facts": []}),
        encoding="utf-8",
    )

    exit_code = main(["--project-dir", str(project_root)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["summary"] == {
        "assets_checked": 0,
        "assets_uncollected": 2,
        "claims_checked": 0,
        "violations": 0,
    }
    assert [row["status"] for row in report["uncollected"]] == [
        "failed",
        "blocked_by_robots",
    ]
    assert report["uncollected"][0]["reason"] == "404 Not Found"
    assert report["uncollected"][1]["reason"]
    assert "Uncollected assets: 2" in captured.err


def test_auditor_reports_provenance_violations_and_exits_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = tmp_path / "violating-project"
    _create_project(project_root, violating=True)

    exit_code = main(["--project-dir", str(project_root)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    codes = {violation["code"] for violation in report["violations"]}

    assert exit_code == 1
    assert {
        "rights_flag_missing",
        "dimensions_missing",
        "sha256_mismatch",
        "social_date_unverified_missing",
        "claim_source_url_missing",
        "claim_source_text_missing",
        "claim_last_checked_at_missing",
        "unsupported_claim_not_needs_review",
    } <= codes
    assert "FAIL" in captured.err


def test_auditor_still_rejects_wrong_hash_for_saved_asset(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = tmp_path / "wrong-hash-project"
    _create_project(project_root, violating=False)
    manifest_path = project_root / "research" / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    exit_code = main(["--project-dir", str(project_root)])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert any(item["code"] == "sha256_mismatch" for item in report["violations"])


def test_auditor_requires_client_supplier_provenance(
    tmp_path: Path,
    capsys,
) -> None:
    project_root = tmp_path / "incomplete-client-project"
    _create_project(project_root, violating=False)
    manifest_path = project_root / "research" / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[0].update(
        {
            "source": "client_supplied",
            "source_url": (
                "client-supplied://sha256/"
                f"{manifest[0]['sha256']}/hero.png"
            ),
            "url": (
                "client-supplied://sha256/"
                f"{manifest[0]['sha256']}/hero.png"
            ),
            "source_page": (
                "client-supplied://sha256/"
                f"{manifest[0]['sha256']}/hero.png"
            ),
            "original_filename": "hero.png",
            "status": "client_supplied",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    exit_code = main(["--project-dir", str(project_root)])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert any(
        item["code"] == "client_supplied_by_missing"
        for item in report["violations"]
    )
