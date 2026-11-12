"""Unit tests for Human Review Package (app/approval.py) and Cleanup (app/cleanup.py)."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
from app.approval import (
    ReleaseApprovalGateError,
    ReleaseVerificationError,
    approve_and_freeze_release,
    approve_release,
    generate_review_summary,
    stage_release_candidate,
    verify_release,
)
from app.cleanup import cleanup, safe_prune_project
from app.db import ProjectRecord, connect_db, init_db, upsert_project
from app.pipeline import main as pipeline_cli_main
from app.reviser import compute_section_hashes
from app.rights import confirm_asset_rights

from engine.exporter import export_release


def _scaffold_reviewable_project(
    tmp_path: Path,
    project_id: str = "luxe-paws",
    *,
    qa_passed: bool = True,
    critical_defects: list[str] | None = None,
    asset_rights_confirmed: bool = True,
) -> tuple[Path, Path]:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / project_id
    db_path = projects_dir / "editvista.sqlite3"

    for sub in (
        "assets/website",
        "assets/instagram",
        "assets/optimized",
        "research",
        "design",
        "website/css",
        "website/js",
        "qa/screenshots",
        "tmp",
    ):
        (project_root / sub).mkdir(parents=True, exist_ok=True)

    # DB record
    conn = connect_db(db_path)
    try:
        init_db(conn)
        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_id,
                business_name="Luxe Paws Grooming",
                primary_url="https://luxepaws.example/",
                status="awaiting_approval",
            ),
        )
    finally:
        conn.close()

    # Assets: 1 retained hero asset, 2 unreferenced raw staging files, 1 temp scraper file
    hero_bytes = b"retained-hero-bytes"
    (project_root / "assets/optimized/hero_dog.jpg").write_bytes(hero_bytes)
    (project_root / "assets/website/raw_unused_1.jpg").write_bytes(b"unused-website-bytes")
    (project_root / "assets/instagram/raw_unused_reel.mp4").write_bytes(b"unused-insta-bytes")
    (project_root / "tmp/scraper_stage.tmp").write_bytes(b"temp-working-file")

    # Facts (2 verified, 1 needs_review)
    facts_payload = {
        "project_id": project_id,
        "extracted_at": "2026-11-05T12:00:00Z",
        "prompt_injection_warnings": [],
        "facts": [
            {
                "fact_id": "f1",
                "project_id": project_id,
                "category": "business_name",
                "claim_key": "business_name",
                "claim_value": "Luxe Paws Grooming",
                "source_url": "https://luxepaws.example/",
                "source_text_snippet": "Luxe Paws Grooming",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
            {
                "fact_id": "f2",
                "project_id": project_id,
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(512) 555-0190",
                "source_url": "https://luxepaws.example/",
                "source_text_snippet": "Call (512) 555-0190",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
            {
                "fact_id": "f3",
                "project_id": project_id,
                "category": "pricing",
                "claim_key": "price_deluxe",
                "claim_value": "$120",
                "source_url": "https://luxepaws.example/",
                "source_text_snippet": "Unverified",
                "is_verified": False,
                "status": "needs_review",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
        ],
    }
    (project_root / "research/business-facts.json").write_text(
        json.dumps(facts_payload), encoding="utf-8"
    )

    # Asset manifest with rights_confirmed_for_preview
    asset_manifest = [
        {
            "file": "assets/optimized/hero_dog.jpg",
            "sha256": hashlib.sha256(hero_bytes).hexdigest(),
            "url": "https://luxepaws.example/hero_dog.jpg",
            "status": "downloaded",
            "rights_confirmed_for_preview": asset_rights_confirmed,
        }
    ]
    (project_root / "research/asset-manifest.json").write_text(
        json.dumps(asset_manifest), encoding="utf-8"
    )
    if asset_rights_confirmed:
        confirm_asset_rights(
            project_root,
            all_assets=True,
            basis="client_provided",
            operator="Test Rights Reviewer",
            evidence="Synthetic asset used by the approval test fixture.",
            projects_dir=projects_dir,
            db_path=db_path,
        )

    # 5-viewport screenshots & QA report
    for width in (1440, 1024, 768, 390, 360):
        (project_root / "qa/screenshots" / f"{width}px.png").write_bytes(b"png-bytes")

    qa_payload: dict[str, object] = {
        "project_id": project_id,
        "passed": qa_passed,
        "viewports": [],
    }
    if critical_defects is not None:
        qa_payload["critical_defects"] = critical_defects
    (project_root / "qa/qa-report.json").write_text(
        json.dumps(qa_payload),
        encoding="utf-8",
    )
    (project_root / "qa/visual-critique.json").write_text(
        json.dumps({"project_id": project_id, "status": "passed", "defects": []}),
        encoding="utf-8",
    )

    # Website files with semantic <section id="..."> elements
    (project_root / "website/index.html").write_text(
        '<!DOCTYPE html><html><head><link rel="stylesheet" href="css/styles.css"></head>'
        '<body>'
        '<header class="ev-header" data-section="header"><h1>Luxe Paws Header</h1></header>'
        '<main>'
        '<section id="hero" data-section="hero"><h1>Luxe Paws</h1>'
        '<img src="../assets/optimized/hero_dog.jpg" alt="Hero"></section>'
        '<section id="services" data-section="services"><h2>Grooming Services</h2></section>'
        '</main>'
        '<footer class="ev-footer" data-section="footer"><p>Footer</p></footer>'
        '<script src="js/main.js"></script></body></html>',
        encoding="utf-8",
    )
    (project_root / "website/css/styles.css").write_text(
        "/* section: header */\n.ev-header { padding: 1rem; }\n/* endsection: header */\n"
        "/* section: hero */\n:root { --ev-primary: #1B365D; }\n"
        "main { padding: 2rem; }\n/* endsection: hero */\n"
        "/* section: services */\n.services { color: #333; }\n/* endsection: services */\n"
        "/* section: footer */\n.ev-footer { text-align: center; }\n/* endsection: footer */\n",
        encoding="utf-8",
    )
    (project_root / "website/js/main.js").write_text(
        "var ready = true;\n",
        encoding="utf-8",
    )

    # Stage candidate into release-candidate/ (never delivery/)
    stage_release_candidate(project_root)

    # Record candidate_sha256 in QA report
    cand_file = project_root / "release-candidate" / "standalone.html"
    if cand_file.is_file():
        qa_payload["candidate_sha256"] = hashlib.sha256(cand_file.read_bytes()).hexdigest()
        (project_root / "qa/qa-report.json").write_text(
            json.dumps(qa_payload),
            encoding="utf-8",
        )

    return projects_dir, project_root


def _human_review_attestation(project_root: Path) -> dict[str, object]:
    candidate = project_root / "release-candidate" / "standalone.html"
    return {
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "reviewed_preview": True,
        "reviewed_facts": True,
        "reviewed_assets": True,
        "reviewed_qa": True,
        "confirmed_release": True,
    }


def test_approve_blocks_on_qa_critical_defects_and_unconfirmed_asset_rights(
    tmp_path: Path,
) -> None:
    """approve must refuse if QA report has critical defects or any used asset lacks rights."""
    # Case 1: QA report has critical defects / passed=False
    projects_dir_qa, project_qa = _scaffold_reviewable_project(
        tmp_path / "case_qa",
        "defect-salon",
        qa_passed=False,
        critical_defects=["Horizontal overflow at 360px viewport"],
        asset_rights_confirmed=True,
    )
    assert not (project_qa / "delivery").exists()
    with pytest.raises(ReleaseApprovalGateError, match="QA|defect"):
        approve_release(project_qa, version="v1", projects_dir=projects_dir_qa)
    assert not (project_qa / "delivery").exists()
    assert not (project_qa / "versions" / "v1").exists()

    # Also test via CLI `approve --project X --version v1`
    with pytest.raises(ReleaseApprovalGateError):
        pipeline_cli_main(
            [
                "approve",
                "--project",
                str(project_qa),
                "--version",
                "v1",
                "--operator",
                "QA Operator",
                "--notes",
                "Attempted approval to verify QA gate.",
                "--reviewed-candidate-sha256",
                str(_human_review_attestation(project_qa)["candidate_sha256"]),
                "--reviewed-preview",
                "--reviewed-facts",
                "--reviewed-assets",
                "--reviewed-qa",
                "--confirmed-release",
            ]
        )
    assert not (project_qa / "delivery").exists()

    # Case 2: Used asset has rights_confirmed_for_preview=False
    projects_dir_rights, project_rights = _scaffold_reviewable_project(
        tmp_path / "case_rights",
        "unconfirmed-rights-salon",
        qa_passed=True,
        asset_rights_confirmed=False,
    )
    assert not (project_rights / "delivery").exists()
    with pytest.raises(ReleaseApprovalGateError, match="rights_confirmed_for_preview"):
        approve_release(project_rights, version="v1", projects_dir=projects_dir_rights)
    assert not (project_rights / "delivery").exists()
    assert not (project_rights / "versions" / "v1").exists()


def test_approve_requires_human_operator_and_decision_notes(tmp_path: Path) -> None:
    """Release approval cannot use fabricated fallback identity or notes."""
    projects_dir, missing_operator = _scaffold_reviewable_project(
        tmp_path / "missing_operator", "missing-operator"
    )
    with pytest.raises(ReleaseApprovalGateError, match="operator name"):
        approve_release(
            missing_operator,
            notes="Reviewed candidate and evidence.",
            projects_dir=projects_dir,
        )
    assert not (missing_operator / "delivery").exists()

    projects_dir, missing_notes = _scaffold_reviewable_project(
        tmp_path / "missing_notes", "missing-notes"
    )
    with pytest.raises(ReleaseApprovalGateError, match="approval notes"):
        approve_release(
            missing_notes,
            operator_name="Named Reviewer",
            projects_dir=projects_dir,
        )
    assert not (missing_notes / "delivery").exists()

    _, missing_review = _scaffold_reviewable_project(
        tmp_path / "missing_review", "missing-review"
    )
    with pytest.raises(ReleaseApprovalGateError, match="human review attestation"):
        approve_release(
            missing_review,
            operator_name="Named Reviewer",
            notes="Reviewed the candidate and evidence.",
            projects_dir=tmp_path / "missing_review" / "projects",
        )
    assert not (missing_review / "delivery").exists()


def test_approve_copies_candidate_writes_manifest_hashes_sets_readonly_and_blocks_duplicate_version(
    tmp_path: Path,
) -> None:
    """approve copies candidate to delivery/ & versions/vN/, sets read-only, blocks duplicate vN."""
    projects_dir, project_root = _scaffold_reviewable_project(tmp_path, "hash-freeze-salon")

    # Before approval, release-candidate/ exists, delivery/ does NOT exist
    assert (project_root / "release-candidate" / "standalone.html").is_file()
    assert (project_root / "release-candidate" / "ghl-ready.html").is_file()
    assert not (project_root / "delivery").exists()

    # Inspect review summary
    summary = generate_review_summary("hash-freeze-salon", projects_dir=projects_dir)
    assert summary["fact_metrics"]["verified_count"] == 2
    assert summary["fact_metrics"]["needs_review_count"] == 1
    assert len(summary["screenshots"]) == 5
    assert summary["technical_qa_passed"] is True
    assert summary["visual_critique_status"] == "passed"

    # First approval via CLI: `approve --project X --version v1`
    exit_code = pipeline_cli_main(
        [
            "approve",
            "--project",
            str(project_root),
            "--version",
            "v1",
            "--operator",
            "Alice Reviewer",
            "--notes",
            "Initial human sign-off",
            "--reviewed-candidate-sha256",
            str(_human_review_attestation(project_root)["candidate_sha256"]),
            "--reviewed-preview",
            "--reviewed-facts",
            "--reviewed-assets",
            "--reviewed-qa",
            "--confirmed-release",
        ]
    )
    assert exit_code == 0

    delivery_dir = project_root / "delivery"
    version_v1_dir = project_root / "versions" / "v1"
    assert delivery_dir.is_dir()
    assert version_v1_dir.is_dir()

    for target_dir in (delivery_dir, version_v1_dir):
        assert (target_dir / "approved-homepage.html").is_file()
        assert (target_dir / "standalone.html").is_file()
        assert (target_dir / "ghl-ready.html").is_file()
        assert (target_dir / "release-manifest.json").is_file()
        # Every file in delivery/ and versions/v1/ must be read-only
        for fpath in target_dir.rglob("*"):
            if fpath.is_file():
                assert (fpath.stat().st_mode & stat.S_IWRITE) == 0, f"{fpath} is not read-only"

    # Verify hashes in release-manifest.json match the actual files on disk + per-section hashes
    manifest_path = delivery_dir / "release-manifest.json"
    manifest_1 = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_1["version"] == "v1"
    assert manifest_1["operator_name"] == "Alice Reviewer"

    for rel_path, expected_sha in manifest_1["checksum_tree"].items():
        disk_file = project_root / rel_path
        assert disk_file.is_file(), f"Missing file listed in manifest: {rel_path}"
        actual_sha = hashlib.sha256(disk_file.read_bytes()).hexdigest()
        assert actual_sha == expected_sha, f"SHA-256 mismatch for {rel_path}"

    approved_html_text = (delivery_dir / "approved-homepage.html").read_text(encoding="utf-8")
    expected_section_hashes = compute_section_hashes(approved_html_text)
    assert expected_section_hashes
    assert manifest_1["section_hashes"] == expected_section_hashes

    # Second approve of the SAME version (v1) MUST fail and refuse to overwrite
    with pytest.raises(ReleaseApprovalGateError, match="v1"):
        pipeline_cli_main(
            [
                "approve",
                "--project",
                str(project_root),
                "--version",
                "v1",
                "--operator",
                "Alice Reviewer",
                "--notes",
                "Attempted duplicate version approval.",
                "--reviewed-candidate-sha256",
                str(_human_review_attestation(project_root)["candidate_sha256"]),
                "--reviewed-preview",
                "--reviewed-facts",
                "--reviewed-assets",
                "--reviewed-qa",
                "--confirmed-release",
            ]
        )

    # Alter CSS, re-stage candidate, and approve as a NEW version (v2)
    (project_root / "website/css/styles.css").write_text(
        ":root { --ev-primary: #D98A2B; }\nmain { padding: 3rem; }\n",
        encoding="utf-8",
    )
    stage_release_candidate(project_root)
    qa_path = project_root / "qa" / "qa-report.json"
    qa_payload = json.loads(qa_path.read_text(encoding="utf-8"))
    cand_2 = project_root / "release-candidate" / "standalone.html"
    qa_payload["candidate_sha256"] = hashlib.sha256(cand_2.read_bytes()).hexdigest()
    qa_path.write_text(json.dumps(qa_payload), encoding="utf-8")
    version_hash_2 = approve_and_freeze_release(
        "hash-freeze-salon",
        operator_name="Alice Reviewer",
        notes="Updated primary color token",
        review_attestation=_human_review_attestation(project_root),
        version="v2",
        projects_dir=projects_dir,
    )
    assert version_hash_2 != manifest_1["version_hash"]
    assert (project_root / "versions" / "v2" / "release-manifest.json").is_file()
    # v1 remains intact
    v1_manifest = json.loads((version_v1_dir / "release-manifest.json").read_text(encoding="utf-8"))
    assert v1_manifest["version_hash"] == manifest_1["version_hash"]


def test_retained_assets_and_delivery_files_never_touched_by_cleanup(tmp_path: Path) -> None:
    """Cleanup aborts before approval and preserves retained-assets and delivery/ after approval."""
    projects_dir, project_root = _scaffold_reviewable_project(tmp_path, "prune-safe-salon")

    # Cleanup aborts before human approval (delivery/ does not exist yet)
    with pytest.raises(ValueError):
        cleanup(project_root, mode="prune", confirm=True)
    with pytest.raises(ValueError):
        safe_prune_project(project_root, dry_run=False)

    # Approve and freeze release v1 (creates projects/_archives/prune-safe-salon-approved.zip)
    approve_and_freeze_release(
        "prune-safe-salon",
        operator_name="Bob Release Lead",
        notes="Approved for final delivery and staging cleanup",
        review_attestation=_human_review_attestation(project_root),
        version="v1",
        projects_dir=projects_dir,
    )

    archive_zip = projects_dir / "_archives" / "prune-safe-salon-approved.zip"
    assert archive_zip.is_file()

    # Dry-run mode returns eligible staging/temp files without deleting them
    dry_plan = safe_prune_project(project_root, dry_run=True)
    assert "assets/website/raw_unused_1.jpg" in dry_plan["eligible_for_deletion"]
    assert "assets/instagram/raw_unused_reel.mp4" in dry_plan["eligible_for_deletion"]
    assert "tmp/scraper_stage.tmp" in dry_plan["eligible_for_deletion"]
    assert "assets/optimized/hero_dog.jpg" not in dry_plan["eligible_for_deletion"]
    assert (project_root / "assets/website/raw_unused_1.jpg").is_file()

    # Execute mode deletes only unreferenced staging and tmp files
    exec_res = safe_prune_project(project_root, dry_run=False)
    assert exec_res["deleted_count"] >= 3

    # Unreferenced staging & temp files are deleted
    assert not (project_root / "assets/website/raw_unused_1.jpg").exists()
    assert not (project_root / "assets/instagram/raw_unused_reel.mp4").exists()
    assert not (project_root / "tmp/scraper_stage.tmp").exists()

    # Retained assets, versions/, and delivery/ files are NEVER touched
    assert (project_root / "assets/optimized/hero_dog.jpg").is_file()
    assert (project_root / "delivery/approved-homepage.html").is_file()
    assert (project_root / "delivery/ghl-ready.html").is_file()
    assert (project_root / "delivery/retained-assets.json").is_file()
    assert (project_root / "delivery/release-manifest.json").is_file()
    assert (project_root / "versions/v1/approved-homepage.html").is_file()

    with zipfile.ZipFile(archive_zip) as zf:
        names = zf.namelist()
        assert "delivery/approved-homepage.html" in names
        assert "delivery/ghl-ready.html" in names
        assert "assets/optimized/hero_dog.jpg" in names


def test_approve_refuses_when_qa_report_is_stale_or_candidate_hash_mismatched(
    tmp_path: Path,
) -> None:
    """Approve fails if QA report candidate sha256 differs from current candidate."""
    projects_dir, project_root = _scaffold_reviewable_project(tmp_path, "stale-qa-salon")
    cand_file = project_root / "release-candidate" / "standalone.html"
    real_sha = hashlib.sha256(cand_file.read_bytes()).hexdigest()

    # Case A: QA report has mismatched/stale sha256
    qa_path = project_root / "qa" / "qa-report.json"
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    qa_data["candidate_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
    qa_path.write_text(json.dumps(qa_data), encoding="utf-8")

    with pytest.raises(ReleaseApprovalGateError, match="stale|candidate"):
        approve_release("stale-qa-salon", version="v1", projects_dir=projects_dir)

    # Case B: QA report updated with matching sha256 -> approval succeeds
    qa_data["candidate_sha256"] = real_sha
    qa_path.write_text(json.dumps(qa_data), encoding="utf-8")
    v_hash = approve_release(
        "stale-qa-salon",
        version="v1",
        operator_name="QA Reviewer",
        notes="Reviewed candidate after QA hash was refreshed.",
        projects_dir=projects_dir,
        review_attestation=_human_review_attestation(project_root),
    )
    assert v_hash

    # Verify manifest records approver, UTC timestamp, and QA report sha256
    manifest_path = project_root / "delivery" / "release-manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["approver"] == "QA Reviewer"
    assert "approved_at" in manifest_data
    assert manifest_data["qa_report_sha256"] == hashlib.sha256(qa_path.read_bytes()).hexdigest()


def test_compute_section_hashes_tracks_header_and_footer_and_css_changes() -> None:
    """compute_section_hashes tracks header, footer, and individual section CSS changes."""
    html_1 = """<!DOCTYPE html>
<html>
<head><style>
/* section: hero */ .hero { color: red; }
/* section: header */ .header { color: blue; }
</style></head>
<body>
<header class="ev-header" data-section="header"><h1>Header Title</h1></header>
<main>
<section id="hero" class="ev-hero" data-section="hero"><h2>Hero Content</h2></section>
<section id="services" class="ev-section" data-section="services"><h2>Services</h2></section>
</main>
<footer class="ev-footer" data-section="footer"><p>Footer Content</p></footer>
</body>
</html>"""
    hashes_1 = compute_section_hashes(html_1)
    assert "header" in hashes_1
    assert "footer" in hashes_1
    assert "hero" in hashes_1
    assert "services" in hashes_1

    # Alter ONLY header
    html_header_changed = html_1.replace("Header Title", "Header Updated")
    hashes_header_changed = compute_section_hashes(html_header_changed)
    assert hashes_header_changed["header"] != hashes_1["header"]
    assert hashes_header_changed["hero"] == hashes_1["hero"]
    assert hashes_header_changed["services"] == hashes_1["services"]
    assert hashes_header_changed["footer"] == hashes_1["footer"]

    # Alter ONLY footer
    html_footer_changed = html_1.replace("Footer Content", "Footer Copyright 2026")
    hashes_footer_changed = compute_section_hashes(html_footer_changed)
    assert hashes_footer_changed["footer"] != hashes_1["footer"]
    assert hashes_footer_changed["header"] == hashes_1["header"]
    assert hashes_footer_changed["hero"] == hashes_1["hero"]
    assert hashes_footer_changed["services"] == hashes_1["services"]

    # Alter hero CSS
    html_hero_css_changed = html_1.replace("color: red;", "color: green;")
    hashes_hero_css_changed = compute_section_hashes(html_hero_css_changed)
    assert hashes_hero_css_changed["hero"] != hashes_1["hero"]
    assert hashes_hero_css_changed["services"] == hashes_1["services"]
    assert hashes_hero_css_changed["footer"] == hashes_1["footer"]


def test_rights_check_ignores_unreferenced_assets_and_blocks_referenced_unconfirmed_assets(
    tmp_path: Path,
) -> None:
    """Unreferenced assets without rights are ignored; unconfirmed referenced assets block."""
    projects_dir, project_root = _scaffold_reviewable_project(tmp_path, "rights-split-salon")

    # Add unconfirmed asset to asset manifest that is NOT referenced in candidate HTML
    manifest_path = project_root / "research" / "asset-manifest.json"
    manifest_items = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_items.append(
        {
            "file": "assets/optimized/unreferenced_unconfirmed.jpg",
            "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            "url": "https://luxepaws.example/unref.jpg",
            "status": "downloaded",
            "rights_confirmed_for_preview": False,
        }
    )
    manifest_path.write_text(json.dumps(manifest_items), encoding="utf-8")

    # Candidate HTML only references hero_dog.jpg (which has rights_confirmed_for_preview=True)
    cand_file = project_root / "release-candidate" / "standalone.html"
    cand_sha = hashlib.sha256(cand_file.read_bytes()).hexdigest()
    qa_path = project_root / "qa" / "qa-report.json"
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    qa_data["candidate_sha256"] = cand_sha
    qa_path.write_text(json.dumps(qa_data), encoding="utf-8")

    # Approval MUST SUCCEED because unreferenced_unconfirmed.jpg is unreferenced
    version_hash = approve_release(
        "rights-split-salon",
        version="v1",
        operator_name="Rights Reviewer",
        notes="Reviewed used asset rights and confirmed unused assets are excluded.",
        projects_dir=projects_dir,
        review_attestation=_human_review_attestation(project_root),
    )
    assert version_hash

    # Now create project where the candidate HTML references an unconfirmed asset
    projects_dir_2, project_2 = _scaffold_reviewable_project(tmp_path, "rights-blocked-salon")
    cand_file_2 = project_2 / "release-candidate" / "standalone.html"
    cand_text = cand_file_2.read_text(encoding="utf-8")
    unconfirmed_img = '<img src="../assets/optimized/unreferenced_unconfirmed.jpg" alt="No">'
    cand_text_with_unconfirmed = cand_text.replace(
        "Luxe Paws</h1>",
        f"Luxe Paws</h1>{unconfirmed_img}",
    )
    assert cand_text_with_unconfirmed != cand_text
    cand_file_2.write_text(cand_text_with_unconfirmed, encoding="utf-8")
    qa_path_2 = project_2 / "qa" / "qa-report.json"
    qa_data_2 = json.loads(qa_path_2.read_text(encoding="utf-8"))
    qa_data_2["candidate_sha256"] = hashlib.sha256(cand_file_2.read_bytes()).hexdigest()
    qa_path_2.write_text(json.dumps(qa_data_2), encoding="utf-8")

    # Add unconfirmed asset to manifest of project_2
    manifest_path_2 = project_2 / "research" / "asset-manifest.json"
    m_items_2 = json.loads(manifest_path_2.read_text(encoding="utf-8"))
    m_items_2.append(
        {
            "file": "assets/optimized/unreferenced_unconfirmed.jpg",
            "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            "url": "https://luxepaws.example/unref.jpg",
            "status": "downloaded",
            "rights_confirmed_for_preview": False,
        }
    )
    manifest_path_2.write_text(json.dumps(m_items_2), encoding="utf-8")

    # Approval MUST BE BLOCKED
    with pytest.raises(ReleaseApprovalGateError, match="rights_confirmed_for_preview"):
        approve_release("rights-blocked-salon", version="v1", projects_dir=projects_dir_2)


def test_verify_release_and_cli_fails_on_tampered_delivery_file_and_blocks_exporter_and_cleanup(
    tmp_path: Path,
) -> None:
    """verify detects tampered delivery files; exporter and cleanup refuse on mismatch."""
    projects_dir, project_root = _scaffold_reviewable_project(tmp_path, "tamper-proof-salon")
    cand_file = project_root / "release-candidate" / "standalone.html"
    cand_sha = hashlib.sha256(cand_file.read_bytes()).hexdigest()
    qa_path = project_root / "qa" / "qa-report.json"
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    qa_data["candidate_sha256"] = cand_sha
    qa_path.write_text(json.dumps(qa_data), encoding="utf-8")

    # Approve release
    approve_release(
        "tamper-proof-salon",
        version="v1",
        operator_name="Release Reviewer",
        notes="Reviewed the release candidate and approved the frozen version.",
        projects_dir=projects_dir,
        review_attestation=_human_review_attestation(project_root),
    )

    # Verification passes cleanly initially
    assert verify_release(project_root) is True
    assert pipeline_cli_main(["verify", "--project", str(project_root)]) == 0

    # Tamper with delivery/approved-homepage.html
    tamper_file = project_root / "delivery" / "approved-homepage.html"
    tamper_file.chmod(tamper_file.stat().st_mode | stat.S_IWRITE)
    tamper_file.write_text(
        tamper_file.read_text(encoding="utf-8") + "\n<!-- tampered -->",
        encoding="utf-8",
    )

    # verify_release and verify CLI must fail
    with pytest.raises(ReleaseVerificationError):
        verify_release(project_root)
    assert pipeline_cli_main(["verify", "--project", str(project_root)]) != 0

    # Exporter must refuse
    exp_res = export_release(project_root)
    assert (
        exp_res.get("status") in ("blocked_verification_failure", "blocked_immutable_release")
        or not exp_res.get("approved_built")
    )

    # Cleanup must refuse
    with pytest.raises((ReleaseVerificationError, ValueError)):
        safe_prune_project(project_root, dry_run=False)
