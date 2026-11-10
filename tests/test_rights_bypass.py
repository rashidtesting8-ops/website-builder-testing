"""Tests closing the rights-gate bypasses and tightening approval.

Requirements tested:
1. approve_release: audit check is MANDATORY for every used asset
   (missing audit file = violation, not a skip).
2. Cockpit confirm-rights must call app.rights.confirm_asset_rights with required basis,
   operator, evidence. No boolean-only flip. Remove the endswith() suffix match;
   match by exact file or sha256.
3. Remove default operator/notes from ApproveReleaseRequest. Remove --auto-approve-test and
   the auto-rights block from production code; tests use a fixture helper instead.
4. Remove the effective_allow_private substring hack in pipeline.py.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests
import uvicorn
from app.approval import ReleaseApprovalGateError, approve_release, stage_release_candidate
from app.cockpit import create_cockpit_app
from app.db import ProjectRecord, connect_db, init_db, upsert_project
from app.pipeline import build_arg_parser, run_pipeline
from app.qa_engine import QADefectReport
from PIL import Image


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def _ephemeral_cockpit_server(projects_dir: Path) -> Iterator[str]:
    app = create_cockpit_app(projects_dir)
    port = _get_free_port()
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(40):
        try:
            r = requests.get(f"{base_url}/api/health", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.05)

    if not ready:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError("Failed to start ephemeral cockpit server")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


def _make_mock_image(path: Path, color: tuple[int, int, int] = (20, 40, 80)) -> tuple[bytes, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (320, 240), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()
    path.write_bytes(data)
    return data, hashlib.sha256(data).hexdigest()


def _scaffold_bypass_test_project(
    projects_dir: Path,
    project_id: str = "bypass-project",
    *,
    include_used_asset: bool = True,
    rights_confirmed_in_manifest: bool = False,
) -> tuple[Path, str]:
    project_root = projects_dir / project_id
    for sub in (
        "assets/optimized",
        "research",
        "qa/screenshots",
        "release-candidate",
        "website/css",
        "website/js",
    ):
        (project_root / sub).mkdir(parents=True, exist_ok=True)

    db_path = projects_dir / "editvista.sqlite3"
    conn = connect_db(db_path)
    init_db(conn)
    upsert_project(
        conn,
        ProjectRecord(
            project_id=project_id,
            business_name="Bypass Grooming Co",
            primary_url="https://bypass.example/",
            status="awaiting_approval",
        ),
    )
    conn.close()

    hero_sha = ""
    asset_manifest: list[dict[str, Any]] = []
    if include_used_asset:
        hero_path = project_root / "assets" / "optimized" / "hero.jpg"
        _, hero_sha = _make_mock_image(hero_path)
        asset_manifest.append(
            {
                "file": "assets/optimized/hero.jpg",
                "sha256": hero_sha,
                "url": "https://bypass.example/hero.jpg",
                "rights_confirmed_for_preview": rights_confirmed_in_manifest,
            }
        )

    (project_root / "research" / "asset-manifest.json").write_text(
        json.dumps(asset_manifest, indent=2), encoding="utf-8"
    )

    facts_payload = {
        "project_id": project_id,
        "facts": [
            {
                "category": "business_name",
                "claim_key": "name",
                "claim_value": "Bypass Grooming Co",
                "is_verified": True,
                "status": "verified",
            }
        ],
    }
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps(facts_payload, indent=2), encoding="utf-8"
    )

    img_tag = '<img src="assets/optimized/hero.jpg" alt="Hero">' if include_used_asset else ""
    (project_root / "website" / "index.html").write_text(
        "<!DOCTYPE html><html><head></head><body>"
        f"<h1>Bypass Grooming Co</h1>{img_tag}"
        "</body></html>",
        encoding="utf-8",
    )
    (project_root / "website" / "css" / "styles.css").write_text(
        "body { margin: 0; }", encoding="utf-8"
    )
    (project_root / "website" / "js" / "main.js").write_text("console.log('ok');", encoding="utf-8")

    stage_release_candidate(project_root)

    cand_html = project_root / "release-candidate" / "standalone.html"
    cand_sha = hashlib.sha256(cand_html.read_bytes()).hexdigest()

    qa_report = {
        "project_id": project_id,
        "passed": True,
        "candidate_sha256": cand_sha,
        "critical_defects": [],
    }
    (project_root / "qa" / "qa-report.json").write_text(json.dumps(qa_report), encoding="utf-8")

    return project_root, hero_sha


# --- REQUIREMENT 1 TESTS ---


def test_approve_release_refuses_when_audit_file_missing_for_used_assets(
    tmp_path: Path,
) -> None:
    """Old bypass: manifest has rights_confirmed=True but research/rights-audit.jsonl is missing.

    approve_release must refuse approval because audit check is mandatory for every used asset.
    """
    projects_dir = tmp_path / "projects"
    project_root, _ = _scaffold_bypass_test_project(
        projects_dir,
        "missing-audit-file-salon",
        include_used_asset=True,
        rights_confirmed_in_manifest=True,
    )
    audit_file = project_root / "research" / "rights-audit.jsonl"
    assert not audit_file.exists()

    with pytest.raises(ReleaseApprovalGateError) as exc_info:
        approve_release(
            project_root,
            operator_name="Auditor Alice",
            notes="Attempting approve without audit file",
            projects_dir=projects_dir,
        )

    err_msg = str(exc_info.value).lower()
    assert "audit" in err_msg or "rights-audit.jsonl" in err_msg


# --- REQUIREMENT 2 TESTS ---


def test_cockpit_confirm_rights_refuses_boolean_only_flip(tmp_path: Path) -> None:
    """Cockpit confirm-rights must require basis, operator, and evidence.

    Boolean-only flip payload without required audit fields must be refused.
    """
    projects_dir = tmp_path / "projects"
    _scaffold_bypass_test_project(projects_dir, "cockpit-bool-flip", include_used_asset=True)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        resp = requests.post(
            f"{base_url}/api/projects/cockpit-bool-flip/confirm-rights",
            json={"asset_file": "assets/optimized/hero.jpg", "confirmed": True},
            timeout=3.0,
        )
        assert resp.status_code in (400, 422)


def test_cockpit_confirm_rights_refuses_suffix_match(tmp_path: Path) -> None:
    """Cockpit confirm-rights must not use endswith() suffix matching.

    Must match only by exact file path or exact sha256.
    """
    projects_dir = tmp_path / "projects"
    _scaffold_bypass_test_project(projects_dir, "cockpit-suffix-match", include_used_asset=True)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        resp = requests.post(
            f"{base_url}/api/projects/cockpit-suffix-match/confirm-rights",
            json={
                "asset_file": "ptimized/hero.jpg",
                "basis": "licensed",
                "operator": "Auditor Bob",
                "evidence": "Stock license receipt #9981",
            },
            timeout=3.0,
        )
        assert resp.status_code in (400, 404)


def test_cockpit_confirm_rights_and_approve_flow_with_audit_trail(tmp_path: Path) -> None:
    """Cockpit confirm with basis, operator, evidence creates audit entry and allows approval."""
    projects_dir = tmp_path / "projects"
    project_root, hero_sha = _scaffold_bypass_test_project(
        projects_dir, "cockpit-legit-flow", include_used_asset=True
    )

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        pre_appr = requests.post(
            f"{base_url}/api/projects/cockpit-legit-flow/approve",
            json={
                "operator_name": "Auditor Bob",
                "notes": "Premature attempt",
                "version": "v1",
            },
            timeout=3.0,
        )
        assert pre_appr.status_code == 400

        confirm_resp = requests.post(
            f"{base_url}/api/projects/cockpit-legit-flow/confirm-rights",
            json={
                "asset_file": "assets/optimized/hero.jpg",
                "basis": "owner_permission",
                "operator": "Auditor Bob",
                "evidence": "Owner signed written consent form on 2026-11-10",
            },
            timeout=3.0,
        )
        assert confirm_resp.status_code == 200

        audit_path = project_root / "research" / "rights-audit.jsonl"
        assert audit_path.is_file()
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["who"] == "Auditor Bob"
        assert entry["basis"] == "owner_permission"
        assert entry["asset_sha256"] == hero_sha

        ok_appr = requests.post(
            f"{base_url}/api/projects/cockpit-legit-flow/approve",
            json={
                "operator_name": "Auditor Bob",
                "notes": "Approved with verified audit",
                "version": "v1",
            },
            timeout=3.0,
        )
        assert ok_appr.status_code == 200
        assert ok_appr.json()["status"] == "approved"
        assert (project_root / "delivery" / "release-manifest.json").is_file()


# --- REQUIREMENT 3 TESTS ---


def test_approve_release_request_requires_operator_and_notes(tmp_path: Path) -> None:
    """ApproveReleaseRequest in Cockpit must not have default operator_name or notes."""
    projects_dir = tmp_path / "projects"
    _scaffold_bypass_test_project(projects_dir, "cockpit-defaults-req", include_used_asset=False)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        resp = requests.post(
            f"{base_url}/api/projects/cockpit-defaults-req/approve",
            json={},
            timeout=3.0,
        )
        assert resp.status_code == 422


def test_cli_parser_removes_auto_approve_test() -> None:
    """The --auto-approve-test CLI flag must be removed from production CLI."""
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--url", "https://example.com", "--name", "Example", "--auto-approve-test"]
        )


# --- REQUIREMENT 4 TESTS ---


def test_pipeline_removes_effective_allow_private_substring_hack(tmp_path: Path) -> None:
    """allow_private=False must NOT be overridden if 127.0.0.1 or localhost is in the URL."""
    projects_dir = tmp_path / "projects"

    with patch("app.pipeline.collect") as mock_collect:
        mock_collect.side_effect = RuntimeError("Stop after collect")

        def dummy_qa(project_folder: Path, **_kwargs: object) -> QADefectReport:
            return QADefectReport(
                project_id=project_folder.name,
                target_url="http://127.0.0.1/test",
                passed=True,
                viewports=[],
            )

        try:
            run_pipeline(
                url="http://127.0.0.1/test",
                business_name="Private Test",
                business_id="private-test",
                projects_dir=projects_dir,
                allow_private=False,
                qa_runner=dummy_qa,
            )
        except RuntimeError:
            pass

        assert mock_collect.called
        call_kwargs = mock_collect.call_args.kwargs
        assert call_kwargs.get("allow_private") is False
