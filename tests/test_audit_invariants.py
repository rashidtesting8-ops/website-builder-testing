"""Regression tests for the 7 November 2026 Senior Engineering Audit findings (P1 & P2).

Tests each cross-module invariant identified in the audit:
- P1-1: Fresh pipeline rerun (`resume=False`) never destroys existing `delivery/` or `versions/`.
- P1-2: `verify_release()` verifies `asset_signatures` and aggregate `version_hash`, and snapshots
  released assets into `versions/<v>/`.
- P1-3: Automatic (`apply_targeted_revisions`) and manual (`revise_section`) revisions share one
  canonical source of truth (`website/sections/*.html` + `website/css/*.css`).
- P1-4: Asset provenance records never collide across different projects even when short `asset_id`
  or image SHA-256 hashes match.
- P1-5: `creative-brief.json` controls both editorial copy and `<main>` section ordering.
- P1-6: `LLMAdapter.complete()` reads `provider.default_model`, supplies a concrete `max_tokens`
  default, and `generate_art_direction` filters out unverified facts from LLM prompts.
- P2-8/9/10: CI workflow installs `--extra browser` + Chromium, GHL JS scopes DOM queries per root
  container, Cockpit preview iframe uses `sandbox`, and `browser_gallery.py` intercepts subrequests
  via `safe_url`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from app.approval import (
    ReleaseVerificationError,
    approve_release,
    stage_release_candidate,
    verify_release,
)
from app.art_director import generate_art_direction
from app.db import (
    AssetProvenanceRecord,
    ProjectRecord,
    connect_db,
    init_db,
    list_assets_for_project,
    upsert_asset_provenance,
    upsert_project,
)
from app.generator import generate_website, revise_section
from app.pipeline import run_pipeline
from app.qa_engine import QADefectReport, ViewportQAResult
from app.reviser import apply_targeted_revisions
from app.rights import confirm_asset_rights
from PIL import Image

from engine.llm.adapter import BudgetConfig, LLMAdapter, LLMResponse


def _make_jpeg(path: Path, color: tuple[int, int, int] = (27, 54, 93)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (800, 600), color=color)
    img.save(path, format="JPEG", quality=85)
    return path.read_bytes()


def _mock_qa_runner(project_root: Path) -> QADefectReport:
    stage_release_candidate(project_root)
    cand = project_root / "release-candidate" / "standalone.html"
    cand_sha = hashlib.sha256(cand.read_bytes()).hexdigest() if cand.is_file() else ""
    shot_dir = project_root / "qa" / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot_file = shot_dir / "1440px.png"
    _make_jpeg(shot_file, (40, 70, 110))
    rep = QADefectReport(
        project_id=project_root.name,
        target_url="http://127.0.0.1/index.html",
        passed=True,
        candidate_sha256=cand_sha,
        viewports=[
            ViewportQAResult(
                label="Desktop",
                width=1440,
                screenshot_path="qa/screenshots/1440px.png",
                horizontal_overflow=False,
                scroll_width=1440,
                inner_width=1440,
                passed=True,
            )
        ],
    )
    (project_root / "qa" / "qa-report.json").write_text(
        rep.model_dump_json(indent=2), encoding="utf-8"
    )
    return rep


def test_p1_1_pipeline_rerun_preserves_approved_delivery_and_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-1: Ordinary rerun without --resume must not delete delivery/ or versions/v1/."""

    def _fake_collect(
        url: str,
        output_dir: str | Path,
        max_pages: int = 10,
        max_media: int = 40,
        *,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        out = Path(output_dir) / "paw-spa"
        (out / "research").mkdir(parents=True, exist_ok=True)
        (out / "assets" / "website").mkdir(parents=True, exist_ok=True)
        img_bytes = _make_jpeg(out / "assets" / "website" / "hero_dog.jpg")
        sha = hashlib.sha256(img_bytes).hexdigest()
        (out / "research" / "pages.json").write_text(
            json.dumps(
                [
                    {
                        "url": url,
                        "sha256": "a" * 64,
                        "title": "Paw Spa",
                        "description": "Paw Spa",
                        "text": "Welcome to Paw Spa. Call 512-555-0199.",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (out / "research" / "asset-manifest.json").write_text(
            json.dumps(
                [
                    {
                        "file": "assets/website/hero_dog.jpg",
                        "sha256": sha,
                        "url": f"{url}/hero_dog.jpg",
                        "status": "downloaded",
                        "rights_confirmed_for_preview": True,
                    }
                ]
            ),
            encoding="utf-8",
        )
        return {"project_folder": str(out)}

    monkeypatch.setattr("app.pipeline.collect", _fake_collect)

    # First run -> then operator approves v1
    res1 = run_pipeline(
        url="https://pawspa.example.com",
        business_name="Paw Spa",
        business_id="paw-spa",
        projects_dir=tmp_path,
        resume=False,
        qa_runner=_mock_qa_runner,
    )
    confirm_asset_rights(
        res1.project_folder,
        all_assets=True,
        basis="client_provided",
        operator="Test Rights Reviewer",
        evidence="Synthetic asset used by the pipeline invariant test.",
        projects_dir=tmp_path,
    )
    approve_release(res1.project_folder, version="v1", projects_dir=tmp_path)
    v1_html_path = res1.project_folder / "versions" / "v1" / "approved-homepage.html"
    assert v1_html_path.is_file()
    v1_bytes_before = v1_html_path.read_bytes()

    # Second run without resume=True (simulating fresh collection rerun)
    res2 = run_pipeline(
        url="https://pawspa.example.com",
        business_name="Paw Spa",
        business_id="paw-spa",
        projects_dir=tmp_path,
        resume=False,
        pause_for_approval=True,
        qa_runner=_mock_qa_runner,
        new_draft=True,
    )
    assert res2.project_folder == res1.project_folder
    # v1 release must still exist and remain byte-for-byte identical!
    assert v1_html_path.is_file()
    assert v1_html_path.read_bytes() == v1_bytes_before


def test_p1_2_verify_release_detects_tampered_asset_bytes(tmp_path: Path) -> None:
    """P1-2: verify_release() must fail when a released asset's bytes are modified."""
    proj = tmp_path / "salon-verify"
    hero_path = proj / "assets" / "optimized" / "hero_pup.jpg"
    hero_bytes = _make_jpeg(hero_path, (30, 60, 90))
    hero_sha = hashlib.sha256(hero_bytes).hexdigest()

    (proj / "research").mkdir(parents=True, exist_ok=True)
    (proj / "design").mkdir(parents=True, exist_ok=True)
    (proj / "research" / "asset-manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "assets/optimized/hero_pup.jpg",
                    "sha256": hero_sha,
                    "url": "https://salon.example.com/hero_pup.jpg",
                    "rights_confirmed_for_preview": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    (proj / "research" / "business-facts.json").write_text(
        json.dumps(
            {
                "project_id": "salon-verify",
                "facts": [
                    {
                        "category": "business_name",
                        "claim_key": "name",
                        "claim_value": "Salon Verify",
                        "is_verified": True,
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (proj / "design" / "image-selection.json").write_text(
        json.dumps(
            {
                "hero_image": {
                    "filename": "hero_pup.jpg",
                    "relative_path": "assets/optimized/hero_pup.jpg",
                    "object_position": "50% 30%",
                },
                "grooming_gallery": [],
            }
        ),
        encoding="utf-8",
    )
    conn = connect_db(tmp_path / "editvista.sqlite3")
    try:
        init_db(conn)
        upsert_project(
            conn,
            ProjectRecord(
                project_id="salon-verify",
                business_name="Salon Verify",
                primary_url="https://salon.example.com",
                status="awaiting_approval",
            ),
        )
    finally:
        conn.close()
    confirm_asset_rights(
        proj,
        all_assets=True,
        basis="client_provided",
        operator="Test Rights Reviewer",
        evidence="Synthetic asset used by the release verification test.",
        projects_dir=tmp_path,
    )

    generate_art_direction(proj)
    generate_website(proj)
    _mock_qa_runner(proj)
    approve_release(proj, version="v1", projects_dir=tmp_path)

    # Initial verification succeeds
    assert verify_release(proj) is True
    # Version snapshot must also contain the referenced asset
    assert (proj / "versions" / "v1" / "assets" / "optimized" / "hero_pup.jpg").is_file()

    # Tamper with the shared asset bytes on disk
    _make_jpeg(hero_path, (220, 20, 20))
    with pytest.raises(ReleaseVerificationError, match="asset"):
        verify_release(proj)


def test_p1_3_auto_revision_then_manual_revision_preserves_auto_fix(tmp_path: Path) -> None:
    """P1-3: Automatic hero revision followed by manual services revision must keep hero fix."""
    proj = tmp_path / "rev-sync"
    (proj / "research").mkdir(parents=True, exist_ok=True)
    (proj / "design").mkdir(parents=True, exist_ok=True)
    (proj / "qa").mkdir(parents=True, exist_ok=True)
    (proj / "research" / "business-facts.json").write_text(
        json.dumps(
            {
                "project_id": "rev-sync",
                "facts": [
                    {
                        "category": "business_name",
                        "claim_key": "name",
                        "claim_value": "Revision Sync Salon",
                        "is_verified": True,
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generate_art_direction(proj)
    generate_website(proj)

    # Write a critique targeting #hero spacing
    (proj / "qa" / "visual-critique.json").write_text(
        json.dumps(
            {
                "project_id": "rev-sync",
                "status": "defects_found",
                "inspected_screenshots": ["qa/screenshots/1440px.png"],
                "defects": [
                    {
                        "target_section_id": "hero",
                        "screenshot_path": "qa/screenshots/1440px.png",
                        "defect_type": "spacing_and_whitespace_rhythm",
                        "actionable_instruction": (
                            "Increase padding on #hero to clamp(1.25rem, 2.5vw, 2rem) and "
                            "restore vertical whitespace rhythm."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # 1. Run automatic revision on #hero
    patch_res = apply_targeted_revisions(proj)
    assert "hero" in patch_res.modified_section_ids
    hero_hash_after_auto = patch_res.after_hashes["hero"]

    # 2. Now manually revise #services via revise_section()
    new_services_html = (
        '<section id="services" class="ev-section" data-section="services">\n'
        '  <h2 class="ev-section-title">Updated Spa Services</h2>\n'
        "</section>"
    )
    hashes_after_manual = revise_section("services", new_services_html, project_folder=proj)

    # Hero's automatic revision must still be present in index.html and its hash preserved!
    rebuilt_html = (proj / "website" / "index.html").read_text(encoding="utf-8")
    assert 'data-ev-revised="spacing_and_whitespace_rhythm"' in rebuilt_html
    assert hashes_after_manual["hero"] == hero_hash_after_auto


def test_p1_4_asset_provenance_no_cross_project_collision(tmp_path: Path) -> None:
    """P1-4: Same short asset_id across two projects must not overwrite project A's row."""
    conn = connect_db(tmp_path / "test.sqlite3")
    try:
        init_db(conn)
        upsert_project(
            conn,
            ProjectRecord(
                project_id="biz-a",
                business_name="Biz A",
                primary_url="https://biza.example.com",
                status="running",
            ),
        )
        upsert_project(
            conn,
            ProjectRecord(
                project_id="biz-b",
                business_name="Biz B",
                primary_url="https://bizb.example.com",
                status="running",
            ),
        )

        upsert_asset_provenance(
            conn,
            AssetProvenanceRecord(
                asset_id="asset-0-deadbeef12",
                project_id="biz-a",
                sha256_hash="deadbeef" * 8,
                source_url="https://biza.example.com/dog.jpg",
                local_path="assets/website/dog_a.jpg",
                rights_confirmed_for_preview=True,
            ),
        )
        upsert_asset_provenance(
            conn,
            AssetProvenanceRecord(
                asset_id="asset-0-deadbeef12",
                project_id="biz-b",
                sha256_hash="deadbeef" * 8,
                source_url="https://bizb.example.com/dog.jpg",
                local_path="assets/website/dog_b.jpg",
                rights_confirmed_for_preview=False,
            ),
        )

        a_assets = list_assets_for_project(conn, "biz-a")
        b_assets = list_assets_for_project(conn, "biz-b")
        assert len(a_assets) == 1
        assert len(b_assets) == 1
        assert a_assets[0].local_path == "assets/website/dog_a.jpg"
        assert a_assets[0].rights_confirmed_for_preview is True
        assert b_assets[0].local_path == "assets/website/dog_b.jpg"
        assert b_assets[0].rights_confirmed_for_preview is False
    finally:
        conn.close()


def test_p1_5_creative_brief_controls_section_order(tmp_path: Path) -> None:
    """P1-5: Custom suggested_sections order in creative-brief.json controls <main> order."""
    proj = tmp_path / "order-test"
    _make_jpeg(proj / "assets" / "optimized" / "gal1.jpg")
    (proj / "research").mkdir(parents=True, exist_ok=True)
    (proj / "design").mkdir(parents=True, exist_ok=True)
    (proj / "research" / "business-facts.json").write_text(
        json.dumps(
            {
                "project_id": "order-test",
                "facts": [
                    {
                        "category": "business_name",
                        "claim_key": "name",
                        "claim_value": "Atelier Order",
                        "is_verified": True,
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (proj / "design" / "image-selection.json").write_text(
        json.dumps(
            {
                "grooming_gallery": [
                    {
                        "filename": "gal1.jpg",
                        "relative_path": "assets/optimized/gal1.jpg",
                        "object_position": "50% 35%",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    generate_art_direction(proj)
    brief_path = proj / "design" / "creative-brief.json"
    brief_data = json.loads(brief_path.read_text(encoding="utf-8"))
    brief_data["editorial_headline"] = "Atelier Order — Custom Bespoke Headline"
    brief_data["suggested_sections"] = ["hero", "gallery", "services", "contact"]
    brief_path.write_text(json.dumps(brief_data, indent=2), encoding="utf-8")

    bundle = generate_website(proj)
    html_doc = bundle.index_html_path.read_text(encoding="utf-8")
    assert "Atelier Order — Custom Bespoke Headline" in html_doc
    assert html_doc.index('id="gallery"') < html_doc.index('id="services"')


def test_p1_6_llm_adapter_reads_default_model_and_filters_unverified_facts(
    tmp_path: Path,
) -> None:
    """P1-6: LLMAdapter uses provider.default_model and max_tokens; prompts filter facts."""
    captured_calls: list[dict[str, Any]] = []

    class _FakeRealProvider:
        default_model = "claude-3-7-sonnet-20250219"

        def complete(
            self,
            prompt: str,
            images: Sequence[bytes | Path] = (),
            model: str | None = None,
            **kwargs: Any,
        ) -> LLMResponse:
            captured_calls.append(
                {
                    "prompt": prompt,
                    "model": model,
                    "max_tokens": kwargs.get("max_tokens"),
                }
            )
            return LLMResponse(
                content=json.dumps(
                    {
                        "headline": "Verified Luxury Headline",
                        "tagline": "Verified Tagline",
                        "value_proposition": "Verified Value Prop",
                        "suggested_section_order": ["hero", "services", "contact"],
                    }
                ),
                prompt_tokens=20,
                completion_tokens=15,
                total_tokens=35,
                model=model or self.default_model,
            )

    adapter = LLMAdapter(
        provider=_FakeRealProvider(),
        config=BudgetConfig(
            cache_dir=tmp_path / "cache",
            db_path=tmp_path / "llm.sqlite3",
        ),
    )

    proj = tmp_path / "llm-filter-proj"
    (proj / "research").mkdir(parents=True, exist_ok=True)
    (proj / "research" / "business-facts.json").write_text(
        json.dumps(
            {
                "project_id": "llm-filter-proj",
                "facts": [
                    {
                        "category": "business_name",
                        "claim_key": "name",
                        "claim_value": "Verified Bark Spa",
                        "is_verified": True,
                        "status": "verified",
                    },
                    {
                        "category": "pricing",
                        "claim_key": "unverified_ rumor_discount",
                        "claim_value": "$5 Secret Unverified Discount",
                        "is_verified": False,
                        "status": "needs_review",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    generate_art_direction(proj, llm_adapter=adapter)
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["model"] == "claude-3-7-sonnet-20250219"
    assert isinstance(call["max_tokens"], int) and call["max_tokens"] > 0
    assert "$5 Secret Unverified Discount" not in call["prompt"]


def test_p2_ci_ghl_root_scope_and_browser_security_guards(tmp_path: Path) -> None:
    """P2-8/9/10: Verify CI browser extra, root-scoped JS, iframe sandbox, and SSRF guard."""
    repo_root = Path(__file__).resolve().parent.parent

    # P2-8: CI workflow installs browser extra
    ci_text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-24.04" in ci_text
    assert "--extra browser" in ci_text
    assert "playwright install" in ci_text

    # P2-9: Generated JS scopes DOM queries to root container
    proj = tmp_path / "js-scope"
    generate_website(proj)
    js_text = (proj / "website" / "js" / "main.js").read_text(encoding="utf-8")
    assert "querySelectorAll('.ev-ghl-scope')" in js_text or "root.querySelector" in js_text

    # P2-10a: Cockpit preview iframe is sandboxed
    cockpit_tpl = (repo_root / "app" / "templates" / "review_cockpit.html").read_text(
        encoding="utf-8"
    )
    assert 'sandbox="' in cockpit_tpl

    # P2-10b: browser_gallery.py intercepts subresource/redirect requests via page.route + safe_url
    bg_text = (repo_root / "src" / "engine" / "browser_gallery.py").read_text(encoding="utf-8")
    assert "page.route(" in bg_text
