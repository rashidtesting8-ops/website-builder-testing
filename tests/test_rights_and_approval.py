"""Tests for auditable asset rights workflow and tightened approve_release gate.

Requirements tested:
1. New CLI: python -m app.pipeline confirm-rights --project-id X
   (--asset <sha256-or-file> | --all) --basis {client_provided|owner_permission|licensed}
   --operator NAME --evidence "free text".
2. Append-only audit file research/rights-audit.jsonl (who, when UTC, basis, evidence,
   asset sha256). Update asset-manifest.json AND SQLite record atomically. Reject empty
   operator/basis/evidence; reject operator "Dev".
3. approve_release must:
   (a) refuse if each used asset has no matching audit entry for its CURRENT sha256.
   (b) refuse if latest visual critique has open defects or page uses any needs_review fact,
       UNLESS --acknowledge-open-issues is passed; acknowledged list stored in manifest.
4. Remove default operator "Dev" and notes "Verified": both become required.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.approval import ReleaseApprovalGateError, approve_release
from app.db import (
    AssetProvenanceRecord,
    ProjectRecord,
    connect_db,
    init_db,
    list_assets_for_project,
    upsert_asset_provenance,
    upsert_project,
)
from app.pipeline import main as pipeline_cli_main


def _scaffold_project_for_rights(tmp_path: Path, project_id: str = "luxe-rights-salon") -> Path:
    project_root = tmp_path / project_id
    for sub in (
        "assets/optimized",
        "assets/website",
        "research",
        "qa/screenshots",
        "release-candidate",
        "website/css",
        "website/js",
    ):
        (project_root / sub).mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "editvista.sqlite3"
    conn = connect_db(db_path)
    init_db(conn)
    upsert_project(
        conn,
        ProjectRecord(
            project_id=project_id,
            business_name="Luxe Rights Salon",
            primary_url="https://luxerights.example/",
            status="awaiting_approval",
        ),
    )

    # 1. Create asset on disk
    hero_bytes = b"hero-image-bytes-v1"
    hero_sha = hashlib.sha256(hero_bytes).hexdigest()
    hero_path = project_root / "assets" / "optimized" / "hero.jpg"
    hero_path.write_bytes(hero_bytes)

    # 2. Record asset in SQLite provenance (rights unconfirmed)
    upsert_asset_provenance(
        conn,
        AssetProvenanceRecord(
            asset_id="hero.jpg",
            project_id=project_id,
            sha256_hash=hero_sha,
            source_url="https://luxerights.example/hero.jpg",
            local_path="assets/optimized/hero.jpg",
            rights_confirmed_for_preview=False,
        ),
    )
    conn.close()

    # 3. Record asset in asset-manifest.json (rights unconfirmed)
    asset_manifest = [
        {
            "file": "assets/optimized/hero.jpg",
            "sha256": hero_sha,
            "url": "https://luxerights.example/hero.jpg",
            "status": "downloaded",
            "rights_confirmed_for_preview": False,
        }
    ]
    (project_root / "research" / "asset-manifest.json").write_text(
        json.dumps(asset_manifest, indent=2), encoding="utf-8"
    )

    # 4. Create facts
    facts_payload = {
        "project_id": project_id,
        "facts": [
            {
                "fact_id": "f1",
                "category": "business_name",
                "claim_key": "name",
                "claim_value": "Luxe Rights Salon",
                "is_verified": True,
                "status": "verified",
            }
        ],
    }
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps(facts_payload, indent=2), encoding="utf-8"
    )

    # 5. Create QA and visual critique
    for width in (1440, 1024, 768, 390, 360):
        (project_root / "qa" / "screenshots" / f"{width}px.png").write_bytes(b"png")

    html_content = (
        '<!DOCTYPE html><html><body>'
        '<header data-section="header"><h1>Luxe Rights Salon</h1></header>'
        '<main><section id="hero" data-section="hero">'
        '<img src="../assets/optimized/hero.jpg" alt="Hero">'
        '</section></main>'
        '<footer data-section="footer"><p>Footer</p></footer>'
        '</body></html>'
    )
    rc_dir = project_root / "release-candidate"
    (rc_dir / "standalone.html").write_text(html_content, encoding="utf-8")
    (rc_dir / "approved-homepage.html").write_text(html_content, encoding="utf-8")
    (rc_dir / "ghl-ready.html").write_text(html_content, encoding="utf-8")
    cand_sha = hashlib.sha256(html_content.encode("utf-8")).hexdigest()

    qa_report = {
        "project_id": project_id,
        "passed": True,
        "candidate_sha256": cand_sha,
        "viewports": [],
    }
    (project_root / "qa" / "qa-report.json").write_text(json.dumps(qa_report), encoding="utf-8")
    (project_root / "qa" / "visual-critique.json").write_text(
        json.dumps({"project_id": project_id, "status": "passed", "defects": []}),
        encoding="utf-8",
    )

    return project_root


def test_unconfirmed_asset_blocks_approval(tmp_path: Path) -> None:
    """An unconfirmed asset without an audit entry blocks release approval."""
    project_root = _scaffold_project_for_rights(tmp_path, "unconfirmed-salon")
    db_path = tmp_path / "editvista.sqlite3"

    with pytest.raises(ReleaseApprovalGateError, match="rights|audit"):
        approve_release(
            project_root,
            operator_name="Alice Smith",
            notes="Ready for release",
            version="v1",
            projects_dir=tmp_path,
            db_path=db_path,
        )


def test_confirm_rights_cli_and_subsequent_approval(tmp_path: Path) -> None:
    """confirm-rights CLI updates audit log, manifest, and DB; approval then passes."""
    project_root = _scaffold_project_for_rights(tmp_path, "confirm-salon")
    db_path = tmp_path / "editvista.sqlite3"

    # Step 1: Run confirm-rights CLI
    exit_code = pipeline_cli_main(
        [
            "confirm-rights",
            "--project-id",
            "confirm-salon",
            "--all",
            "--basis",
            "owner_permission",
            "--operator",
            "Sarah Jenkins",
            "--evidence",
            "Owner signed photo release waiver on 2026-11-09",
            "--projects-dir",
            str(tmp_path),
            "--db-path",
            str(db_path),
        ]
    )
    assert exit_code == 0

    # Verify audit file created
    audit_file = project_root / "research" / "rights-audit.jsonl"
    assert audit_file.is_file()
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    audit_entry = json.loads(lines[0])
    assert audit_entry["who"] == "Sarah Jenkins"
    assert audit_entry["basis"] == "owner_permission"
    assert "signed photo release" in audit_entry["evidence"]
    assert len(audit_entry["asset_sha256"]) == 64

    # Verify asset-manifest.json updated
    manifest_p = project_root / "research" / "asset-manifest.json"
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    assert manifest[0]["rights_confirmed_for_preview"] is True

    # Verify SQLite record updated
    conn = connect_db(db_path)
    records = list_assets_for_project(conn, "confirm-salon")
    assert len(records) == 1
    assert records[0].rights_confirmed_for_preview is True
    conn.close()

    # Step 2: Now approve_release passes
    manifest_sha = approve_release(
        project_root,
        operator_name="Sarah Jenkins",
        notes="Verified owner release documentation",
        version="v1",
        projects_dir=tmp_path,
        db_path=db_path,
        review_attestation={
            "candidate_sha256": hashlib.sha256(
                (project_root / "release-candidate" / "standalone.html").read_bytes()
            ).hexdigest(),
            "reviewed_preview": True,
            "reviewed_facts": True,
            "reviewed_assets": True,
            "reviewed_qa": True,
            "confirmed_release": True,
        },
    )
    assert len(manifest_sha) == 64
    assert (project_root / "delivery" / "release-manifest.json").is_file()


def test_changed_asset_bytes_invalidate_confirmation(tmp_path: Path) -> None:
    """Tampering with asset bytes after confirmation invalidates the audit entry."""
    project_root = _scaffold_project_for_rights(tmp_path, "tamper-salon")
    db_path = tmp_path / "editvista.sqlite3"

    # Confirm rights
    pipeline_cli_main(
        [
            "confirm-rights",
            "--project-id",
            "tamper-salon",
            "--all",
            "--basis",
            "client_provided",
            "--operator",
            "Bob Reviewer",
            "--evidence",
            "Uploaded directly by client via portal",
            "--projects-dir",
            str(tmp_path),
            "--db-path",
            str(db_path),
        ]
    )

    # Tamper with the image file bytes
    hero_path = project_root / "assets" / "optimized" / "hero.jpg"
    hero_path.write_bytes(b"tampered-modified-bytes")

    # Approval MUST now be refused because current SHA-256 no longer matches the audit log
    with pytest.raises(ReleaseApprovalGateError, match="CURRENT sha256|audit|mismatch"):
        approve_release(
            project_root,
            operator_name="Bob Reviewer",
            notes="Sign off",
            version="v1",
            projects_dir=tmp_path,
            db_path=db_path,
        )


def test_reject_empty_or_dev_operator_in_confirm_rights(tmp_path: Path) -> None:
    """confirm-rights rejects operator 'Dev' and empty operator/basis/evidence."""
    _scaffold_project_for_rights(tmp_path, "reject-salon")
    db_path = tmp_path / "editvista.sqlite3"

    with pytest.raises((ValueError, ReleaseApprovalGateError), match="Dev|operator"):
        pipeline_cli_main(
            [
                "confirm-rights",
                "--project-id",
                "reject-salon",
                "--all",
                "--basis",
                "licensed",
                "--operator",
                "Dev",
                "--evidence",
                "Valid license",
                "--projects-dir",
                str(tmp_path),
                "--db-path",
                str(db_path),
            ]
        )


def test_audit_file_is_append_only(tmp_path: Path) -> None:
    """Multiple confirmations append lines to rights-audit.jsonl and never overwrite."""
    project_root = _scaffold_project_for_rights(tmp_path, "append-salon")
    db_path = tmp_path / "editvista.sqlite3"

    for idx, (who, basis) in enumerate(
        [("Alice Reviewer", "client_provided"), ("Bob Reviewer", "licensed")], start=1
    ):
        pipeline_cli_main(
            [
                "confirm-rights",
                "--project-id",
                "append-salon",
                "--all",
                "--basis",
                basis,
                "--operator",
                who,
                "--evidence",
                f"Evidence signoff round {idx}",
                "--projects-dir",
                str(tmp_path),
                "--db-path",
                str(db_path),
            ]
        )

    audit_file = project_root / "research" / "rights-audit.jsonl"
    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "Second confirmation must append a new line, never rewrite"
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["who"] == "Alice Reviewer"
    assert second["who"] == "Bob Reviewer"


def test_acknowledge_open_issues_stored_in_release_manifest(tmp_path: Path) -> None:
    """Visual critique defects or needs_review facts require --acknowledge-open-issues."""
    project_root = _scaffold_project_for_rights(tmp_path, "ack-salon")
    db_path = tmp_path / "editvista.sqlite3"

    # Confirm asset rights
    pipeline_cli_main(
        [
            "confirm-rights",
            "--project-id",
            "ack-salon",
            "--all",
            "--basis",
            "licensed",
            "--operator",
            "Lead Reviewer",
            "--evidence",
            "Stock license agreement verified",
            "--projects-dir",
            str(tmp_path),
            "--db-path",
            str(db_path),
        ]
    )

    # Introduce a visual defect into visual-critique.json
    (project_root / "qa" / "visual-critique.json").write_text(
        json.dumps(
            {
                "project_id": "ack-salon",
                "status": "defects_found",
                "defects": [
                    {
                        "target_section_id": "hero",
                        "defect_type": "typography",
                        "description": "Heading contrast is slightly low on mobile viewports",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Attempting approve_release without explicit acknowledgment must be refused.
    # Omission must be as safe as an explicit False.
    with pytest.raises(ReleaseApprovalGateError, match="open issues|critique|defect"):
        approve_release(
            project_root,
            operator_name="Lead Reviewer",
            notes="Sign off without acknowledgment",
            version="v1",
            projects_dir=tmp_path,
            db_path=db_path,
        )

    # With acknowledge_open_issues=True, approval succeeds and records issues in manifest
    manifest_sha = approve_release(
        project_root,
        operator_name="Lead Reviewer",
        notes="Acknowledged minor mobile contrast defect for initial preview",
        version="v1",
        projects_dir=tmp_path,
        db_path=db_path,
        acknowledge_open_issues=True,
        review_attestation={
            "candidate_sha256": hashlib.sha256(
                (project_root / "release-candidate" / "standalone.html").read_bytes()
            ).hexdigest(),
            "reviewed_preview": True,
            "reviewed_facts": True,
            "reviewed_assets": True,
            "reviewed_qa": True,
            "confirmed_release": True,
        },
    )
    assert len(manifest_sha) == 64

    manifest_json = json.loads(
        (project_root / "delivery" / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert "acknowledged_issues" in manifest_json
    assert len(manifest_json["acknowledged_issues"]) >= 1
    assert any("Heading contrast" in str(issue) for issue in manifest_json["acknowledged_issues"])


def test_approval_refuses_critic_unavailable(tmp_path: Path) -> None:
    project_root = _scaffold_project_for_rights(tmp_path, "critic-unavailable-salon")
    db_path = tmp_path / "editvista.sqlite3"

    pipeline_cli_main(
        [
            "confirm-rights",
            "--project-id",
            "critic-unavailable-salon",
            "--all",
            "--basis",
            "owner_permission",
            "--operator",
            "Release Reviewer",
            "--evidence",
            "Owner provided written preview permission",
            "--projects-dir",
            str(tmp_path),
            "--db-path",
            str(db_path),
        ]
    )
    (project_root / "qa" / "visual-critique.json").write_text(
        json.dumps(
            {
                "project_id": "critic-unavailable-salon",
                "status": "critic_unavailable",
                "defects": [],
                "llm_used": False,
                "warnings": ["LLM provider unavailable"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseApprovalGateError, match="critic unavailable"):
        approve_release(
            project_root,
            operator_name="Release Reviewer",
            notes="Reviewed the candidate",
            version="v1",
            projects_dir=tmp_path,
            db_path=db_path,
            acknowledge_open_issues=True,
            review_attestation={
                "candidate_sha256": hashlib.sha256(
                    (project_root / "release-candidate" / "standalone.html").read_bytes()
                ).hexdigest(),
                "reviewed_preview": True,
                "reviewed_facts": True,
                "reviewed_assets": True,
                "reviewed_qa": True,
                "confirmed_release": True,
            },
        )

