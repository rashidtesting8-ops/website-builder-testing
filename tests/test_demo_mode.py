from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.approval import (
    ReleaseApprovalGateError,
    approve_release,
    export_approved_deliverables,
    stage_release_candidate,
    verify_release,
)
from app.db import (
    ProjectRecord,
    connect_db,
    init_db,
    list_assets_for_project,
    upsert_project,
)
from app.generator import rebuild_index_from_sections, revise_section
from app.ghl_exporter import export_ghl_snippet
from app.pilot import PILOT_BUSINESS_SPECS, execute_acceptance_pilot
from app.pipeline import (
    _enforce_third_party_demo_rights,
    _stage_downloaded_assets_for_curator,
    run_pipeline,
)
from app.pipeline import main as pipeline_cli_main
from app.qa_engine import QADefectReport
from app.rights import confirm_asset_rights

from engine.exporter import export_release

DEMO_BANNER = "INTERNAL DEMO - NOT A VERIFIED BUSINESS"


def _write_input(project_root: Path, *, kind: str, reason: str = "Internal test fixture.") -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "input.json").write_text(
        json.dumps(
            {
                "demo": {
                    "is_fixture": True,
                    "kind": kind,
                    "reason": reason,
                }
            }
        ),
        encoding="utf-8",
    )


def _write_sections(project_root: Path) -> None:
    sections = project_root / "website" / "sections"
    sections.mkdir(parents=True, exist_ok=True)
    (sections / "header.html").write_text(
        '<header data-section="header">Header</header>', encoding="utf-8"
    )
    (sections / "hero.html").write_text(
        '<section data-section="hero"><h1>Preview</h1></section>', encoding="utf-8"
    )
    (sections / "footer.html").write_text(
        '<footer data-section="footer">Footer</footer>', encoding="utf-8"
    )


def _write_candidate_and_qa(project_root: Path) -> str:
    (project_root / "website").mkdir(parents=True, exist_ok=True)
    (project_root / "website" / "index.html").write_text(
        (
            "<!DOCTYPE html><html><head><title>Fixture</title></head><body>"
            f'<div data-editvista-demo="true">{DEMO_BANNER}</div>'
            "<main>Fixture page</main></body></html>"
        ),
        encoding="utf-8",
    )
    candidate, _ = stage_release_candidate(project_root)
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    qa_dir = project_root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    (qa_dir / "qa-report.json").write_text(
        json.dumps({"passed": True, "candidate_sha256": candidate_sha, "critical_defects": []}),
        encoding="utf-8",
    )
    (project_root / "research").mkdir(parents=True, exist_ok=True)
    (project_root / "research" / "asset-manifest.json").write_text("[]", encoding="utf-8")
    return candidate_sha


def test_fixture_banner_survives_generation_and_section_revision(tmp_path: Path) -> None:
    project_root = tmp_path / "fixture"
    _write_input(project_root, kind="fictional")
    _write_sections(project_root)

    html = rebuild_index_from_sections(project_root / "website")
    assert DEMO_BANNER in html
    assert 'data-editvista-demo="true"' in html

    revise_section(
        "hero",
        '<section data-section="hero"><h1>Revised preview</h1></section>',
        project_root,
    )
    revised = (project_root / "website" / "index.html").read_text(encoding="utf-8")
    assert DEMO_BANNER in revised


def test_approval_rejects_fixture_candidate_with_removed_banner(tmp_path: Path) -> None:
    project_root = tmp_path / "stripped-banner"
    _write_input(project_root, kind="fictional")
    _write_candidate_and_qa(project_root)
    candidate = project_root / "release-candidate" / "standalone.html"
    candidate.write_text(
        candidate.read_text(encoding="utf-8")
        .replace('data-editvista-demo="true"', "")
        .replace(DEMO_BANNER, ""),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseApprovalGateError, match="banner"):
        approve_release(
            project_root,
            operator_name="Human Reviewer",
            notes="Attempted approval without the fixture banner.",
        )


def test_invalid_demo_input_fails_before_pipeline_mutation_or_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "invalid-fixture"
    project_root.mkdir(parents=True)
    (project_root / "input.json").write_text(
        '{"demo":{"is_fixture":true,"kind":"unrecognized","reason":"test"}}',
        encoding="utf-8",
    )
    collect_calls = 0

    def fail_if_collected(*_args: object, **_kwargs: object) -> None:
        nonlocal collect_calls
        collect_calls += 1
        raise AssertionError("collection must not run for invalid demo metadata")

    monkeypatch.setattr("app.pipeline.collect", fail_if_collected)
    with pytest.raises(ValueError, match="demo"):
        run_pipeline(
            url="https://example.test/",
            business_name="Invalid Fixture",
            business_id="invalid-fixture",
            projects_dir=projects_dir,
        )

    assert collect_calls == 0
    assert not (projects_dir / "editvista.sqlite3").exists()


def test_third_party_demo_rights_confirmation_is_refused(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "third-party"
    _write_input(project_root, kind="third_party_internal_demo")
    asset_path = project_root / "assets" / "photo.jpg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"photo bytes")
    manifest_path = project_root / "research" / "asset-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            [
                {
                    "file": "assets/photo.jpg",
                    "sha256": hashlib.sha256(b"photo bytes").hexdigest(),
                    "url": "https://source.example/photo.jpg",
                    "rights_confirmed_for_preview": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="third-party"):
        confirm_asset_rights(
            project_root,
            asset="photo.jpg",
            basis="owner_permission",
            operator="Reviewer",
            evidence="Internal demo only",
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))[0][
        "rights_confirmed_for_preview"
    ] is False
    assert not (project_root / "research" / "rights-audit.jsonl").exists()

    with pytest.raises(ValueError, match="third-party"):
        pipeline_cli_main(
            [
                "confirm-rights",
                "--project-id",
                project_root.name,
                "--asset",
                "photo.jpg",
                "--basis",
                "owner_permission",
                "--operator",
                "Reviewer",
                "--evidence",
                "Internal demo only",
                "--projects-dir",
                str(project_root.parent),
            ]
        )


def test_pipeline_keeps_third_party_asset_rights_unconfirmed(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "third-party-pipeline"
    _write_input(project_root, kind="third_party_internal_demo")
    asset = project_root / "assets" / "website" / "photo.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"collected third-party bytes")
    manifest_path = project_root / "research" / "asset-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            [
                {
                    "file": "assets/website/photo.jpg",
                    "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    "url": "https://source.example/photo.jpg",
                    "status": "downloaded",
                    "rights_confirmed_for_preview": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    social_manifest_path = project_root / "research" / "social-media-manifest.json"
    social_manifest_path.write_text(
        json.dumps(
            [
                {
                    "file": "assets/website/photo.jpg",
                    "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    "rights_confirmed_for_preview": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    conn = connect_db(tmp_path / "projects" / "editvista.sqlite3")
    init_db(conn)
    try:
        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_root.name,
                business_name="Third Party Fixture",
                primary_url="https://source.example/",
                status="running",
            ),
        )
        _enforce_third_party_demo_rights(conn, project_root, project_root.name)
        _stage_downloaded_assets_for_curator(conn, project_root, project_root.name)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        social_manifest = json.loads(social_manifest_path.read_text(encoding="utf-8"))
        records = list_assets_for_project(conn, project_root.name)
    finally:
        conn.close()

    assert manifest[0]["rights_confirmed_for_preview"] is False
    assert social_manifest[0]["rights_confirmed_for_preview"] is False
    assert len(records) == 1
    assert records[0].rights_confirmed_for_preview is False


def test_third_party_demo_refuses_approval_and_all_export_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "third-party"
    _write_input(project_root, kind="third_party_internal_demo")
    _write_candidate_and_qa(project_root)
    assert not (project_root / "release-candidate" / "ghl-ready.html").exists()
    standalone = project_root / "delivery" / "standalone.html"
    standalone.parent.mkdir(parents=True, exist_ok=True)
    standalone.write_text(
        f'<div data-editvista-demo="true">{DEMO_BANNER}</div>', encoding="utf-8"
    )

    with pytest.raises(ReleaseApprovalGateError, match="third-party"):
        approve_release(project_root, operator_name="Reviewer", notes="No release")
    with pytest.raises(ReleaseApprovalGateError, match="third-party"):
        export_approved_deliverables(project_root, is_approved=True)

    result = export_release(project_root, source_html_path=standalone)
    assert result["status"] == "blocked_demo_fixture"
    assert not (project_root / "delivery" / "approved-homepage.html").exists()

    with pytest.raises(PermissionError, match="third-party"):
        export_ghl_snippet(project_root)


def test_fictional_demo_release_and_standalone_export_record_demo_metadata(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "fictional"
    _write_input(project_root, kind="fictional", reason="Synthetic internal test data.")
    candidate_sha = _write_candidate_and_qa(project_root)

    approve_release(
        project_root,
        operator_name="Human Reviewer",
        notes="Approved fictional demo for internal use.",
        review_attestation={
            "candidate_sha256": candidate_sha,
            "reviewed_preview": True,
            "reviewed_facts": True,
            "reviewed_assets": True,
            "reviewed_qa": True,
            "confirmed_release": True,
        },
    )
    manifest = json.loads(
        (project_root / "delivery" / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["demo"] is True
    assert manifest["kind"] == "fictional"
    assert manifest["reason"] == "Synthetic internal test data."
    for name in ("standalone.html", "approved-homepage.html", "ghl-ready.html"):
        html = (project_root / "delivery" / name).read_text(encoding="utf-8")
        assert DEMO_BANNER in html
        assert 'data-editvista-demo="true"' in html
    assert verify_release(project_root)

    direct_project = tmp_path / "fictional-direct-export"
    _write_input(direct_project, kind="fictional")
    standalone = direct_project / "delivery" / "standalone.html"
    standalone.parent.mkdir(parents=True)
    standalone.write_text(
        f'<div data-editvista-demo="true">{DEMO_BANNER}</div>', encoding="utf-8"
    )
    result = export_release(direct_project, source_html_path=standalone)
    assert result["status"] == "exported"
    delivery_manifest = json.loads(
        (direct_project / "delivery" / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert delivery_manifest["demo"] is True
    assert delivery_manifest["kind"] == "fictional"
    retained = json.loads(
        (direct_project / "delivery" / "retained-assets.json").read_text(encoding="utf-8")
    )
    assert retained["demo"] is True


def test_projects_sharing_source_url_keep_separate_demo_classification(tmp_path: Path) -> None:
    roots = (tmp_path / "fictional", tmp_path / "real-business")
    outputs = []
    for index, root in enumerate(roots):
        root.mkdir(parents=True, exist_ok=True)
        _write_sections(root)
        payload: dict[str, object] = {"primary_url": "https://same.example/"}
        if index == 0:
            payload["demo"] = {
                "is_fixture": True,
                "kind": "fictional",
                "reason": "Same-source isolation test.",
            }
        (root / "input.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        outputs.append(rebuild_index_from_sections(root / "website"))

    assert DEMO_BANNER in outputs[0]
    assert 'data-editvista-demo="true"' in outputs[0]
    assert DEMO_BANNER not in outputs[1]
    assert "data-editvista-demo" not in outputs[1]
    assert json.loads((roots[0] / "input.json").read_text(encoding="utf-8"))["demo"]["kind"] == (
        "fictional"
    )
    assert "demo" not in json.loads((roots[1] / "input.json").read_text(encoding="utf-8"))


def test_fixture_kind_cannot_be_changed_after_generation(tmp_path: Path) -> None:
    project_root = tmp_path / "relabelled"
    _write_input(project_root, kind="fictional")
    _write_sections(project_root)
    rebuild_index_from_sections(project_root / "website")

    _write_input(project_root, kind="third_party_internal_demo")
    with pytest.raises(ValueError, match="classification changed"):
        rebuild_index_from_sections(project_root / "website")


def test_absent_demo_preserves_real_business_output(tmp_path: Path) -> None:
    project_root = tmp_path / "real-business"
    _write_sections(project_root)
    html = rebuild_index_from_sections(project_root / "website")
    assert DEMO_BANNER not in html
    assert "data-editvista-demo" not in html

    standalone = project_root / "delivery" / "standalone.html"
    standalone.parent.mkdir(parents=True)
    standalone.write_text("<html><body>Real business</body></html>", encoding="utf-8")
    result = export_release(project_root, source_html_path=standalone)
    assert result["status"] == "exported"
    manifest = json.loads(
        (project_root / "delivery" / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert "demo" not in manifest


def test_builtin_pilot_projects_are_excluded_from_real_business_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.pilot as pilot_module

    monkeypatch.setattr(pilot_module, "PILOT_BUSINESS_SPECS", PILOT_BUSINESS_SPECS[:1])

    def qa_runner(project_root: Path, **_kwargs: object) -> QADefectReport:
        candidate = project_root / "release-candidate" / "standalone.html"
        return QADefectReport(
            project_id=project_root.name,
            target_url=f"http://localhost/{project_root.name}/",
            passed=True,
            candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
            viewports=[],
        )

    report = execute_acceptance_pilot(
        projects_dir=tmp_path / "projects",
        qa_runner=qa_runner,
        live=True,
    )

    assert report.fixture_businesses == 1
    assert report.eligible_real_businesses == 0
    project_input = (
        tmp_path / "projects" / report.results[0].slug / "input.json"
    )
    demo = json.loads(project_input.read_text(encoding="utf-8"))["demo"]
    assert demo["is_fixture"] is True
    assert report.results[0].demo is True
    candidate = (
        tmp_path
        / "projects"
        / report.results[0].slug
        / "release-candidate"
        / "standalone.html"
    )
    assert DEMO_BANNER in candidate.read_text(encoding="utf-8")
