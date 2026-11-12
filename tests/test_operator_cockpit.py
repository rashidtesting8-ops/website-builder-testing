"""Integration tests for the Local Visual Review & Operator Cockpit (Track 2).

Tests:
1. Dashboard lists projects and their statuses from SQLite and directory tree.
2. Review cockpit page serves HTML with fact verification, viewport tabs, and asset rights.
3. Secure preview file serving (with strict path traversal rejection).
4. Asset rights confirmation API toggles rights_confirmed_for_preview in asset-manifest.json.
5. Approval API executes approve_release and freezes delivery/ with release-manifest.json.
6. Surgical revision API patches a single section and leaves other sections untouched.
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

import requests
import uvicorn
from app.approval import stage_release_candidate
from app.cockpit import create_cockpit_app
from app.db import ProjectRecord, connect_db, init_db, upsert_project
from app.qa_engine import BREAKPOINTS, QADefectReport, ViewportQAResult
from app.rights import confirm_asset_rights
from PIL import Image


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mock_png_bytes(
    width: int = 320,
    height: int = 200,
    color: tuple[int, int, int] = (27, 54, 93),
) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scaffold_cockpit_project(
    projects_dir: Path,
    project_id: str = "cockpit-paws",
    *,
    rights_confirmed: bool = False,
) -> Path:
    project_root = projects_dir / project_id
    db_path = projects_dir / "editvista.sqlite3"

    for sub in (
        "assets/optimized",
        "research",
        "design",
        "website/sections",
        "website/css",
        "website/js",
        "qa/screenshots",
        "release-candidate",
    ):
        (project_root / sub).mkdir(parents=True, exist_ok=True)

    # 1. DB record
    conn = connect_db(db_path)
    try:
        init_db(conn)
        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_id,
                business_name="Cockpit Paws Spa",
                primary_url="https://cockpitpaws.example/",
                status="awaiting_approval",
            ),
        )
    finally:
        conn.close()

    # 2. Asset files & Manifest
    dog_png = project_root / "assets/optimized/dog.png"
    dog_png.write_bytes(_mock_png_bytes(400, 300))
    dog_sha = hashlib.sha256(dog_png.read_bytes()).hexdigest()

    asset_manifest = [
        {
            "file": "assets/optimized/dog.png",
            "sha256": dog_sha,
            "url": "https://cockpitpaws.example/dog.png",
            "rights_confirmed_for_preview": rights_confirmed,
        }
    ]
    (project_root / "research/asset-manifest.json").write_text(
        json.dumps(asset_manifest, indent=2), encoding="utf-8"
    )
    if rights_confirmed:
        confirm_asset_rights(
            project_root,
            all_assets=True,
            basis="client_provided",
            operator="Test Rights Reviewer",
            evidence="Synthetic asset used by the Cockpit test fixture.",
            projects_dir=projects_dir,
            db_path=db_path,
        )

    # 3. Facts
    facts_payload = {
        "project_id": project_id,
        "facts": [
            {
                "category": "pricing",
                "claim_key": "bath_price",
                "claim_value": "$45",
                "source_text_snippet": "Full bath starting at $45 for small dogs.",
                "source_url": "https://cockpitpaws.example/prices",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "discount",
                "claim_key": "first_time_offer",
                "claim_value": "10% off",
                "source_text_snippet": "Unverified promo claim",
                "source_url": "https://cockpitpaws.example/promo",
                "is_verified": False,
                "status": "needs_review",
            },
        ],
    }
    (project_root / "research/business-facts.json").write_text(
        json.dumps(facts_payload, indent=2), encoding="utf-8"
    )

    # 4. Sections & Website
    header_html = (
        '<header data-section="header">\n'
        '  <nav><a href="#services">Services</a> <a href="#contact">Contact</a></nav>\n'
        '</header>\n'
    )
    hero_html = (
        '<section id="hero" data-section="hero">\n'
        '  <h1>Cockpit Paws Spa</h1>\n'
        '  <img src="assets/optimized/dog.png" alt="Happy pup">\n'
        '</section>\n'
    )
    services_html = (
        '<section id="services" data-section="services">\n'
        '  <h2>Grooming Services</h2>\n'
        '  <p>Full bath: $45</p>\n'
        '</section>\n'
    )
    footer_html = (
        '<footer data-section="footer">\n'
        '  <p>&copy; 2026 Cockpit Paws</p>\n'
        '</footer>\n'
    )

    (project_root / "website/sections/header.html").write_text(header_html, encoding="utf-8")
    (project_root / "website/sections/hero.html").write_text(hero_html, encoding="utf-8")
    (project_root / "website/sections/services.html").write_text(services_html, encoding="utf-8")
    (project_root / "website/sections/footer.html").write_text(footer_html, encoding="utf-8")

    (project_root / "website/css/styles.css").write_text(
        ":root { --ev-primary: #1B365D; }\nbody { margin: 0; }\n", encoding="utf-8"
    )
    (project_root / "website/js/main.js").write_text("// main\n", encoding="utf-8")

    full_index = (
        "<!DOCTYPE html>\n<html><head><title>Cockpit Paws</title>"
        '<link rel="stylesheet" href="css/styles.css"></head><body>\n'
        f"{header_html}\n{hero_html}\n{services_html}\n{footer_html}\n"
        '<script src="js/main.js"></script></body></html>'
    )
    (project_root / "website/index.html").write_text(full_index, encoding="utf-8")

    # 5. Stage release candidate
    stage_release_candidate(project_root)
    cand_file = project_root / "release-candidate/standalone.html"
    cand_sha = hashlib.sha256(cand_file.read_bytes()).hexdigest()

    # 6. Viewport screenshots & QA report
    viewports = []
    for label, width in BREAKPOINTS:
        shot_path = project_root / "qa/screenshots" / f"{width}px.png"
        shot_path.write_bytes(_mock_png_bytes(width, int(width * 0.75)))
        viewports.append(
            ViewportQAResult(
                label=label,
                width=width,
                screenshot_path=shot_path.relative_to(project_root).as_posix(),
                horizontal_overflow=False,
                scroll_width=width,
                inner_width=width,
                broken_assets=[],
                console_errors=[],
                contrast_failures=[],
                passed=True,
            )
        )

    qa_report = QADefectReport(
        project_id=project_id,
        target_url=f"http://localhost/{project_id}/website/index.html",
        passed=True,
        candidate_sha256=cand_sha,
        viewports=viewports,
    )
    (project_root / "qa/qa-report.json").write_text(
        qa_report.model_dump_json(indent=2), encoding="utf-8"
    )

    # Visual critique
    critique = {
        "project_id": project_id,
        "status": "passed",
        "inspected_screenshots": [f"qa/screenshots/{w}px.png" for _, w in BREAKPOINTS],
        "defects": [],
    }
    (project_root / "qa/visual-critique.json").write_text(
        json.dumps(critique, indent=2), encoding="utf-8"
    )

    return project_root


def _approval_payload(
    project_root: Path,
    *,
    operator_name: str = "Senior Operator",
    notes: str = "Reviewed candidate, sources, assets, and responsive QA.",
) -> dict[str, object]:
    candidate = project_root / "release-candidate" / "standalone.html"
    return {
        "operator_name": operator_name,
        "notes": notes,
        "human_review": {
            "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "reviewed_preview": True,
            "reviewed_facts": True,
            "reviewed_assets": True,
            "reviewed_qa": True,
            "confirmed_release": True,
        },
    }


@contextmanager
def _ephemeral_cockpit_server(projects_dir: Path) -> Iterator[str]:
    app = create_cockpit_app(projects_dir=projects_dir)
    port = _get_free_port()
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Poll server readiness
    base_url = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(30):
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
        raise RuntimeError("Ephemeral cockpit server failed to start")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


def test_cockpit_dashboard_lists_projects(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    _scaffold_cockpit_project(projects_dir, "project-alpha", rights_confirmed=True)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        resp = requests.get(f"{base_url}/", timeout=3.0)
        assert resp.status_code == 200
        assert "Project workspace" in resp.text
        assert "project-alpha" in resp.text
        assert "Cockpit Paws Spa" in resp.text
        assert "awaiting approval" in resp.text
        assert 'id="project-search"' in resp.text
        assert "workspace-shell" in resp.text
        assert "data-project-card" in resp.text


def test_cockpit_review_page_serves_facts_and_viewports(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    _scaffold_cockpit_project(projects_dir, "review-demo", rights_confirmed=False)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        resp = requests.get(f"{base_url}/review/review-demo", timeout=3.0)
        assert resp.status_code == 200
        # Check fact verification display
        assert "Full bath starting at $45" in resp.text
        assert "verified" in resp.text
        assert "needs_review" in resp.text
        # Check 5-viewport screenshot links
        assert "1440px.png" in resp.text
        assert "390px.png" in resp.text
        # Check preview iframe
        assert 'src="/preview/review-demo/standalone.html"' in resp.text
        assert "review-summary" in resp.text
        assert "1 asset(s) still need evidence" in resp.text
        assert "Candidate preview" in resp.text
        assert "I reviewed the candidate preview" in resp.text
        assert "Exact candidate SHA-256" in resp.text


def test_cockpit_preview_serving_and_path_traversal_defense(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    _scaffold_cockpit_project(projects_dir, "secure-demo", rights_confirmed=True)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        # Legitimate candidate preview
        resp = requests.get(f"{base_url}/preview/secure-demo/standalone.html", timeout=3.0)
        assert resp.status_code == 200
        assert "Cockpit Paws Spa" in resp.text

        # Path traversal attack should be 400 or 403 or 404
        bad_resp = requests.get(
            f"{base_url}/preview/secure-demo/../../editvista.sqlite3",
            timeout=3.0,
        )
        assert bad_resp.status_code in (400, 403, 404)


def test_cockpit_confirm_asset_rights_and_approve_release_gate(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    project_root = _scaffold_cockpit_project(projects_dir, "gate-demo", rights_confirmed=False)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        review_page = requests.get(f"{base_url}/review/gate-demo", timeout=3.0)
        assert review_page.status_code == 200
        assert "Select rights basis" in review_page.text
        assert "Evidence for rights confirmation" in review_page.text

        # 1. Approval fails when rights are unconfirmed
        fail_appr = requests.post(
            f"{base_url}/api/projects/gate-demo/approve",
            json=_approval_payload(
                project_root,
                operator_name="Dev QA",
                notes="Premature attempt before rights confirmation.",
            ),
            timeout=3.0,
        )
        assert fail_appr.status_code == 400
        assert "rights_confirmed_for_preview is false" in fail_appr.json()["detail"]

        # 2. Confirm asset rights via Cockpit API
        confirm_resp = requests.post(
            f"{base_url}/api/projects/gate-demo/confirm-rights",
            json={
                "asset_file": "assets/optimized/dog.png",
                "basis": "client_provided",
                "operator": "Test Rights Reviewer",
                "evidence": "Synthetic asset used by the Cockpit confirmation test.",
            },
            timeout=3.0,
        )
        assert confirm_resp.status_code == 200
        assert confirm_resp.json()["rights_confirmed_for_preview"] is True

        # Verify persisted in asset-manifest.json
        manifest = json.loads(
            (project_root / "research/asset-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest[0]["rights_confirmed_for_preview"] is True

        # 3. Approve release now succeeds
        ok_appr = requests.post(
            f"{base_url}/api/projects/gate-demo/approve",
            json=_approval_payload(project_root),
            timeout=3.0,
        )
        assert ok_appr.status_code == 200
        res_data = ok_appr.json()
        assert res_data["status"] == "approved"
        assert len(res_data["manifest_sha256"]) == 64

        # Verify delivery/ was frozen
        assert (project_root / "delivery/release-manifest.json").is_file()
        assert (project_root / "versions/v1/release-manifest.json").is_file()
        manifest = json.loads(
            (project_root / "delivery/release-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["human_review"]["reviewed_by"] == "Senior Operator"
        assert manifest["human_review"]["reviewed_preview"] is True
        assert manifest["human_review"]["confirmed_release"] is True
        assert manifest["human_review"]["candidate_sha256"] == hashlib.sha256(
            (project_root / "release-candidate/standalone.html").read_bytes()
        ).hexdigest()


def test_cockpit_requires_complete_current_candidate_human_attestation(
    tmp_path: Path,
) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    project_root = _scaffold_cockpit_project(
        projects_dir, "attestation-demo", rights_confirmed=True
    )

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        missing_attestation = requests.post(
            f"{base_url}/api/projects/attestation-demo/approve",
            json={"operator_name": "Reviewer", "notes": "Reviewed the candidate carefully."},
            timeout=3.0,
        )
        assert missing_attestation.status_code == 400
        assert not (project_root / "delivery" / "release-manifest.json").exists()

        rejected_attestation = _approval_payload(project_root)
        review = rejected_attestation["human_review"]
        assert isinstance(review, dict)
        review["reviewed_facts"] = False
        rejected = requests.post(
            f"{base_url}/api/projects/attestation-demo/approve",
            json=rejected_attestation,
            timeout=3.0,
        )
        assert rejected.status_code == 422
        assert not (project_root / "delivery" / "release-manifest.json").exists()

        stale_attestation = _approval_payload(project_root)
        review = stale_attestation["human_review"]
        assert isinstance(review, dict)
        review["candidate_sha256"] = "0" * 64
        stale = requests.post(
            f"{base_url}/api/projects/attestation-demo/approve",
            json=stale_attestation,
            timeout=3.0,
        )
        assert stale.status_code == 409
        assert "candidate" in stale.json()["detail"].lower()
        assert not (project_root / "delivery" / "release-manifest.json").exists()


def test_cockpit_surgical_revision_api(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    project_root = _scaffold_cockpit_project(projects_dir, "revise-demo", rights_confirmed=True)

    from app.reviser import compute_section_hashes

    before_index = (project_root / "website/index.html").read_text(encoding="utf-8")
    before_hashes = compute_section_hashes(before_index, project_folder=project_root / "website")

    new_services_html = (
        '<section id="services" data-section="services">\n'
        '  <h2>Bespoke Luxury Spa Services</h2>\n'
        '  <p>Full bath: $45 (Verified)</p>\n'
        '</section>'
    )

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        rev_resp = requests.post(
            f"{base_url}/api/projects/revise-demo/revise-section",
            json={"section_name": "services", "new_html": new_services_html},
            timeout=3.0,
        )
        assert rev_resp.status_code == 200
        data = rev_resp.json()
        assert data["modified_section"] == "services"

        # Verify untouched sections remain identical
        after_index = (project_root / "website/index.html").read_text(encoding="utf-8")
        after_hashes = compute_section_hashes(after_index, project_folder=project_root / "website")

        assert after_hashes["services"] != before_hashes["services"]
        assert after_hashes["header"] == before_hashes["header"]
        assert after_hashes["hero"] == before_hashes["hero"]
        assert after_hashes["footer"] == before_hashes["footer"]


def test_cockpit_ghl_export_and_live_funnel_simulator(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)
    project_root = _scaffold_cockpit_project(projects_dir, "ghl-demo", rights_confirmed=True)

    with _ephemeral_cockpit_server(projects_dir) as base_url:
        # 1. Approve project first so delivery/ is frozen
        appr = requests.post(
            f"{base_url}/api/projects/ghl-demo/approve",
            json=_approval_payload(
                project_root,
                operator_name="GHL Lead",
                notes="Reviewed candidate and froze for GHL export.",
            ),
            timeout=3.0,
        )
        assert appr.status_code == 200
        manifest_before = (project_root / "delivery/release-manifest.json").read_bytes()

        # 2. Fetch scoped GHL export via Cockpit API
        exp_resp = requests.get(
            f"{base_url}/api/projects/ghl-demo/ghl-export",
            timeout=3.0,
        )
        assert exp_resp.status_code == 200
        exp_data = exp_resp.json()
        assert exp_data["project_id"] == "ghl-demo"
        assert exp_data["root_id"] == "ghl-ev-engine-root-ghl-demo"
        assert exp_data["simulation"]["is_valid"] is True
        assert exp_data["simulation"]["unscoped_css_count"] == 0
        assert exp_data["simulation"]["global_leak_detected"] is False
        assert exp_data["simulation"]["asset_urls_absolute"] is True
        assert f"{base_url}/assets/ghl-demo/assets/optimized/dog.png" in exp_data["ghl_html"]

        # Ensure immutable delivery/ manifest was not modified
        assert (project_root / "delivery/release-manifest.json").read_bytes() == manifest_before

        # 3. Fetch live GHL Funnel Simulator page
        sim_resp = requests.get(
            f"{base_url}/ghl-preview/ghl-demo",
            timeout=3.0,
        )
        assert sim_resp.status_code == 200
        assert "ghl-ev-engine-root-ghl-demo" in sim_resp.text
        assert "GoHighLevel Funnel Host Simulator" in sim_resp.text
        assert f"{base_url}/assets/ghl-demo/assets/optimized/dog.png" in sim_resp.text
