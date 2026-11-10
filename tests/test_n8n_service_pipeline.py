"""Tests for n8n Worker Service full 9-stage pipeline orchestration and workflow JSON contracts."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from app.qa_engine import QADefectReport
from PIL import Image


def test_container_qa_runner_propagates_browser_errors(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Browser QA errors must not be converted into a synthetic passing report."""
    import app.pipeline as pipe_mod

    import engine.service as srv

    project_root = tmp_path / "project"
    (project_root / "website").mkdir(parents=True)
    (project_root / "website" / "index.html").write_text(
        "<!doctype html><html><body>Draft</body></html>", encoding="utf-8"
    )

    def _fail_qa(*_args: Any, **_kwargs: Any) -> QADefectReport:
        raise RuntimeError("Playwright browser launch failed")

    monkeypatch.setattr(pipe_mod, "run_qa_engine", _fail_qa)

    with pytest.raises(RuntimeError, match="Playwright browser launch failed"):
        srv._container_safe_qa_runner(project_root)

    assert not (project_root / "qa" / "screenshots").exists()


def _fake_qa_pass(project_folder: str | Path, **kwargs: Any) -> QADefectReport:
    from app.approval import stage_release_candidate

    root = Path(project_folder)
    qa_dir = root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    standalone_path, _ = stage_release_candidate(root)
    sha = hashlib.sha256(standalone_path.read_bytes()).hexdigest()
    report = QADefectReport(
        project_id=root.name,
        target_url="http://127.0.0.1/index.html",
        candidate_sha256=sha,
        passed=True,
        viewports=[],
    )
    (qa_dir / "qa-report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def test_service_pipeline_run_and_approval_endpoints(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Verify POST /pipeline/run executes all 9 stages and POST /pipeline/{id}/approve freezes."""
    import engine.service as srv

    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "DB", tmp_path / "_system" / "jobs.sqlite")
    (tmp_path / "_system").mkdir(parents=True, exist_ok=True)

    def _mock_collect(
        url: str,
        projects_dir: Path,
        max_pages: int = 25,
        max_media: int = 80,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        slug = "royal-paws-spa"
        proj = projects_dir / slug
        (proj / "research" / "snapshots").mkdir(parents=True, exist_ok=True)
        (proj / "assets" / "website").mkdir(parents=True, exist_ok=True)
        snap_html = (
            "<html><head><title>Royal Paws Luxury Spa</title></head>"
            "<body><h1>Royal Paws Luxury Spa</h1>"
            "<p>Call us at (555) 234-5678</p>"
            "<p>Full Luxury Bath & Brush - $85</p>"
            "</body></html>"
        )
        snap_bytes = snap_html.encode("utf-8")
        snap_sha = hashlib.sha256(snap_bytes).hexdigest()
        (proj / "research" / "snapshots" / f"{snap_sha}.html").write_text(
            snap_html, encoding="utf-8"
        )
        pages = [
            {
                "url": url,
                "title": "Royal Paws Luxury Spa",
                "snapshot_file": f"research/snapshots/{snap_sha}.html",
                "snapshot_sha256": snap_sha,
                "text": (
                    "Royal Paws Luxury Spa Call us at (555) 234-5678 "
                    "Full Luxury Bath & Brush - $85"
                ),
            }
        ]
        (proj / "research" / "pages.json").write_text(
            json.dumps(pages, indent=2), encoding="utf-8"
        )
        img_path = proj / "assets" / "website" / "hero-dog.jpg"
        Image.new("RGB", (1200, 900), color=(212, 185, 150)).save(img_path)
        img_sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
        manifest = [
            {
                "url": f"{url.rstrip('/')}/hero-dog.jpg",
                "source_page": url,
                "file": "assets/website/hero-dog.jpg",
                "sha256": img_sha,
                "status": "downloaded",
                "width": 1200,
                "height": 900,
                "rights_confirmed_for_preview": True,
            }
        ]
        (proj / "research" / "asset-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return {
            "site_url": url,
            "pages_crawled": 1,
            "assets_saved": 1,
            "project_folder": str(proj),
        }

    import app.pipeline as pipe_mod

    monkeypatch.setattr(pipe_mod, "collect", _mock_collect)
    monkeypatch.setattr(pipe_mod, "run_qa_engine", _fake_qa_pass)

    # 1. Trigger full 9-stage pipeline via srv.create_pipeline_job
    req = srv.PipelineRequest(
        url="https://royalpawsspa.example.com",
        business_name="Royal Paws Luxury Spa",
        business_id="royal-paws-spa",
    )
    created = srv.create_pipeline_job(req)
    job_id = created["job_id"]

    # 2. Poll srv.get_job(job_id) until finished
    status_data: dict[str, Any] = {}
    for _ in range(40):
        status_data = srv.get_job(job_id)
        if status_data.get("status") in ("done", "failed"):
            break
        time.sleep(0.1)

    assert status_data.get("status") == "done", status_data
    res = status_data["result"]
    assert res["project_id"] == "royal-paws-spa"
    assert res["pipeline_status"] == "awaiting_approval"
    assert res["qa_passed"] is True
    assert "/review/royal-paws-spa" in res["cockpit_review_url"]

    from app.rights import confirm_asset_rights

    confirm_asset_rights(
        tmp_path / "royal-paws-spa",
        all_assets=True,
        basis="client_provided",
        operator="Test Rights Reviewer",
        evidence="Synthetic asset used by the service approval test.",
        projects_dir=tmp_path,
        db_path=tmp_path / "editvista.sqlite3",
    )

    # 3. Approve release via srv.approve_pipeline_project
    app_data = srv.approve_pipeline_project(
        "royal-paws-spa",
        srv.ApprovePipelineRequest(
            version="v1",
            operator="Rashid",
            notes="Approved via n8n workflow",
        ),
    )
    assert app_data["status"] == "approved"
    assert app_data["project_id"] == "royal-paws-spa"
    assert (tmp_path / "royal-paws-spa" / "delivery" / "ghl-ready.html").is_file()


def test_n8n_workflow_json_files_connect_full_pipeline() -> None:
    """Verify the 4 n8n workflow JSON files in n8n/ wire full pipeline and use agency node names."""
    root = Path(__file__).resolve().parent.parent
    wf1_path = root / "n8n" / "01-client-intake-and-website-build.json"
    wf2_path = root / "n8n" / "02-live-build-telemetry-and-cockpit-router.json"
    wf3_path = root / "n8n" / "03-approve-and-export-ghl.json"
    wf4_path = root / "n8n" / "04-interactive-one-click-luxury-demo.json"
    assert wf1_path.is_file(), "Expected n8n/01-client-intake-and-website-build.json"
    assert wf2_path.is_file(), "Expected n8n/02-live-build-telemetry-and-cockpit-router.json"
    assert wf3_path.is_file(), "Expected n8n/03-approve-and-export-ghl.json"
    assert wf4_path.is_file(), "Expected n8n/04-interactive-one-click-luxury-demo.json"
    assert not (root / "n8n" / "01-submit-asset-research.json").exists()
    assert not (root / "n8n" / "02-check-asset-job.json").exists()

    wf1 = json.loads(wf1_path.read_text(encoding="utf-8"))
    wf2 = json.loads(wf2_path.read_text(encoding="utf-8"))
    wf3 = json.loads(wf3_path.read_text(encoding="utf-8"))
    wf4 = json.loads(wf4_path.read_text(encoding="utf-8"))

    # Verify professional agency node names (no "Job" in node names) and >=7 nodes with IF gates
    for wf in (wf1, wf2, wf3, wf4):
        nodes = wf.get("nodes", [])
        assert len(nodes) >= 7, (
            f"Expected >=7 nodes in {wf.get('name')}, got {len(nodes)}"
        )
        node_types = {n.get("type") for n in nodes}
        assert "n8n-nodes-base.if" in node_types, (
            f"Expected conditional IF gate in {wf.get('name')}"
        )
        assert "n8n-nodes-base.set" in node_types, (
            f"Expected payload normalization / dossier Set node in {wf.get('name')}"
        )
        for node in nodes:
            assert "job" not in node.get("name", "").lower(), (
                f"Unprofessional node name {node.get('name')} in {wf.get('name')}"
            )

    # Workflow 1 should support full_pipeline submission to collector:8787
    wf1_str = json.dumps(wf1)
    assert "http://collector:8787/" in wf1_str
    assert "full_pipeline" in wf1_str or "/pipeline/run" in wf1_str

    # Workflow 2 checks build telemetry
    wf2_str = json.dumps(wf2)
    assert "http://collector:8787/jobs/" in wf2_str

    # Workflow 3 triggers approval and GHL export
    wf3_str = json.dumps(wf3)
    assert "/approve" in wf3_str

    # Workflow 4 is an interactive manual-trigger polling loop demo
    wf4_str = json.dumps(wf4)
    assert "n8n-nodes-base.manualTrigger" in wf4_str
    assert "/pipeline/run" in wf4_str
    assert "Compile Executive Review Dossier" in wf4_str


def test_service_sqlite_connection_enforces_wal_and_busy_timeout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Worker service jobs.sqlite must enforce WAL journal mode and a >=5000ms busy_timeout."""
    import engine.service as srv

    monkeypatch.setattr(srv, "DB", tmp_path / "_system" / "jobs.sqlite")
    with srv.connect() as con:
        journal_mode = str(con.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
        busy_timeout = int(con.execute("PRAGMA busy_timeout;").fetchone()[0])
    assert journal_mode == "wal"
    assert busy_timeout >= 5000
