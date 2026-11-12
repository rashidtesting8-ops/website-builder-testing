"""Tests for pipeline rerun safety on approved projects (P0 bug prevention).

Ensures that re-running `run_pipeline` without `--resume` on an approved project:
1. Raises `ProjectLockedError` unless `new_draft=True` / `--new-draft` is passed.
2. Preserves DB status as "approved" when rerun is refused.
3. Resets only draft directories (assets/, research/, design/, website/, release-candidate/, qa/)
   and NEVER deletes delivery/, versions/, or _archives/.
4. Keeps delivery/ and versions/ byte-identical with verify_release() remaining valid.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from app.approval import approve_release, verify_release
from app.db import connect_db, get_project
from app.pipeline import ProjectLockedError, main, run_pipeline
from app.qa_engine import BREAKPOINTS, QADefectReport, ViewportQAResult
from app.rights import confirm_asset_rights
from PIL import Image
from tests.test_pipeline_e2e import _ephemeral_grooming_salon_server


def _mock_qa_runner(project_folder: str | Path, **_kwargs: Any) -> QADefectReport:
    import io

    project_root = Path(project_folder).resolve()
    screenshots_dir = project_root / "qa" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    viewports: list[ViewportQAResult] = []

    # Fast blank PNG bytes
    img = Image.new("RGB", (320, 240), color=(27, 54, 93))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    for label, width in BREAKPOINTS:
        shot = screenshots_dir / f"{width}px.png"
        shot.write_bytes(png_bytes)
        viewports.append(
            ViewportQAResult(
                label=label,
                width=width,
                screenshot_path=shot.relative_to(project_root).as_posix(),
                horizontal_overflow=False,
                scroll_width=width,
                inner_width=width,
                broken_assets=[],
                console_errors=[],
                contrast_failures=[],
                passed=True,
            )
        )

    cand_file = project_root / "release-candidate" / "standalone.html"
    cand_sha = ""
    if cand_file.is_file():
        cand_sha = hashlib.sha256(cand_file.read_bytes()).hexdigest()

    report = QADefectReport(
        project_id=project_root.name,
        target_url=f"http://127.0.0.1/{project_root.name}/standalone.html",
        passed=True,
        candidate_sha256=cand_sha,
        viewports=viewports,
    )
    (project_root / "qa" / "qa-report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


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


def _hash_directory_tree(base_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not base_dir.is_dir():
        return hashes
    for p in sorted(base_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(base_dir).as_posix()
            hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def test_approved_project_rerun_refuses_with_project_locked_error(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    db_path = projects_dir / "editvista.sqlite3"
    biz_id = "safety-salon"

    with _ephemeral_grooming_salon_server() as target_url:
        # 1. Initial pipeline execution
        res1 = run_pipeline(
            url=target_url,
            business_name="Safety Paws Salon",
            business_id=biz_id,
            projects_dir=projects_dir,
            db_path=db_path,
            allow_private=True,
            pause_for_approval=True,
            qa_runner=_mock_qa_runner,
        )
        assert res1.status == "awaiting_approval"

        project_root = projects_dir / biz_id

        # 2. Confirm asset rights
        confirm_asset_rights(
            project_root,
            all_assets=True,
            basis="client_provided",
            operator="Test Rights Reviewer",
            evidence="Synthetic asset used by the rerun safety test.",
            projects_dir=projects_dir,
            db_path=db_path,
        )

        # 3. Approve and freeze release
        release_tree_sha = approve_release(
            project_root,
            version="v1",
            operator_name="Lead Auditor",
            notes="Release approved for production",
            projects_dir=projects_dir,
            db_path=db_path,
            review_attestation=_human_review_attestation(project_root),
            acknowledge_open_issues=True,
        )
        assert bool(release_tree_sha)

        # Confirm release artifacts exist
        delivery_dir = project_root / "delivery"
        versions_dir = project_root / "versions"
        archive_zip = projects_dir / "_archives" / f"{biz_id}-approved.zip"
        assert delivery_dir.is_dir()
        assert (delivery_dir / "release-manifest.json").is_file()
        assert versions_dir.is_dir()
        assert archive_zip.is_file()
        assert verify_release(project_root) is True

        delivery_hashes_before = _hash_directory_tree(delivery_dir)
        versions_hashes_before = _hash_directory_tree(versions_dir)
        assert len(delivery_hashes_before) > 0
        assert len(versions_hashes_before) > 0

        # Verify DB status is 'approved'
        conn = connect_db(db_path)
        try:
            proj = get_project(conn, biz_id)
            assert proj is not None
            assert proj.status == "approved"
        finally:
            conn.close()

        # 4. Attempt re-running without --resume and without new_draft
        # MUST raise ProjectLockedError
        with pytest.raises(ProjectLockedError, match="approved and locked"):
            run_pipeline(
                url=target_url,
                business_name="Safety Paws Salon",
                business_id=biz_id,
                projects_dir=projects_dir,
                db_path=db_path,
                allow_private=True,
                pause_for_approval=True,
                qa_runner=_mock_qa_runner,
                new_draft=False,
            )

        # Assert (a) ProjectLockedError was raised
        # Assert (b) SHA-256 of every file under delivery/ and versions/ unchanged
        assert _hash_directory_tree(delivery_dir) == delivery_hashes_before
        assert _hash_directory_tree(versions_dir) == versions_hashes_before

        # Assert (c) _archives zip exists
        assert archive_zip.is_file()

        # Assert (d) verify_release() still True
        assert verify_release(project_root) is True

        # Assert (3) DB status must stay "approved" after a refused rerun
        conn = connect_db(db_path)
        try:
            proj_after = get_project(conn, biz_id)
            assert proj_after is not None
            assert proj_after.status == "approved"
        finally:
            conn.close()

        # 5. Now re-run with new_draft=True
        res_new_draft = run_pipeline(
            url=target_url,
            business_name="Safety Paws Salon",
            business_id=biz_id,
            projects_dir=projects_dir,
            db_path=db_path,
            allow_private=True,
            pause_for_approval=True,
            qa_runner=_mock_qa_runner,
            new_draft=True,
        )
        assert res_new_draft.status == "awaiting_approval"

        # Assert (e) with new_draft=True drafts are rebuilt
        assert (project_root / "release-candidate" / "standalone.html").is_file()
        assert (project_root / "website" / "index.html").is_file()

        # BUT (b)-(d) still hold:
        assert _hash_directory_tree(delivery_dir) == delivery_hashes_before
        assert _hash_directory_tree(versions_dir) == versions_hashes_before
        assert archive_zip.is_file()
        assert verify_release(project_root) is True


def test_cli_new_draft_flag_and_locked_error(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    db_path = projects_dir / "editvista.sqlite3"
    biz_id = "cli-locked-salon"

    with _ephemeral_grooming_salon_server() as target_url:
        exit1 = main(
            [
                "--url",
                target_url,
                "--name",
                "CLI Salon",
                "--business-id",
                biz_id,
                "--projects-dir",
                str(projects_dir),
                "--db-path",
                str(db_path),
                "--allow-private",
            ],
            qa_runner=_mock_qa_runner,
        )
        assert exit1 == 0

        project_root = projects_dir / biz_id

        # Confirm rights & approve
        confirm_asset_rights(
            project_root,
            all_assets=True,
            basis="client_provided",
            operator="Test Rights Reviewer",
            evidence="Synthetic asset used by the CLI rerun test.",
            projects_dir=projects_dir,
            db_path=db_path,
        )

        approve_release(
            project_root,
            version="v1",
            operator_name="Auditor",
            notes="Approved",
            projects_dir=projects_dir,
            db_path=db_path,
            review_attestation=_human_review_attestation(project_root),
            acknowledge_open_issues=True,
        )

        delivery_hashes_before = _hash_directory_tree(project_root / "delivery")

        # Running CLI without --new-draft raises ProjectLockedError
        with pytest.raises(ProjectLockedError):
            main(
                [
                    "--url",
                    target_url,
                    "--name",
                    "CLI Salon",
                    "--business-id",
                    biz_id,
                    "--projects-dir",
                    str(projects_dir),
                    "--db-path",
                    str(db_path),
                    "--allow-private",
                    "--pause-for-approval",
                ],
                qa_runner=_mock_qa_runner,
            )

        # Running CLI with --new-draft succeeds
        exit_draft = main(
            [
                "--url",
                target_url,
                "--name",
                "CLI Salon",
                "--business-id",
                biz_id,
                "--projects-dir",
                str(projects_dir),
                "--db-path",
                str(db_path),
                "--allow-private",
                "--pause-for-approval",
                "--new-draft",
            ],
            qa_runner=_mock_qa_runner,
        )
        assert exit_draft == 0
        assert _hash_directory_tree(project_root / "delivery") == delivery_hashes_before
        assert verify_release(project_root) is True
