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
        "claims_checked": 1,
        "violations": 0,
    }
    assert "PASS" in captured.err


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
