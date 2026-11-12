"""Milestone 5: Real-World 5-Business Acceptance Pilot Integration Tests.

Validates the EditVista Engine across 5 diverse business archetypes:
1. Classic Luxury Salon (Puppalace archetype: rich gallery, tiered pricing, dog only)
2. Mobile Grooming Van (K9 Advantedge archetype: mobile van, service radius, instant call CTA)
3. Mixed Species Daycare & Spa (Lily's Club archetype: dog & cat segregated pricing, daycare)
4. Minimalist Studio with Sparse Pricing (Dog Grooming Studio: no prices, zero fabrication)
5. Social-Heavy Business (Pets Grooming UK archetype: web + social intake, rights check)

Tests:
- Pilot runner executes all 5 businesses end-to-end.
- Verifies taxonomy classification for all 5 archetypes.
- Verifies zero-fabrication: unverified claims become needs_review.
- Confirms pilot candidates stop at human approval instead of self-approving.
- Verifies comprehensive pilot report generation with execution metrics and audit tables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.pilot import (
    PILOT_BUSINESS_SPECS,
    PilotAcceptanceReport,
    execute_acceptance_pilot,
    run_single_pilot_business,
)
from app.qa_engine import BREAKPOINTS, QADefectReport, ViewportQAResult
from PIL import Image


def _mock_qa_runner(project_folder: str | Path, **_kwargs) -> QADefectReport:
    import hashlib
    import io

    project_root = Path(project_folder).resolve()
    screenshots_dir = project_root / "qa" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (320, 200), color=(27, 54, 93))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    viewports = []
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
    elif (project_root / "website" / "index.html").is_file():
        index_bytes = (project_root / "website" / "index.html").read_bytes()
        cand_sha = hashlib.sha256(index_bytes).hexdigest()

    report = QADefectReport(
        project_id=project_root.name,
        target_url=f"http://localhost/{project_root.name}/website/index.html",
        passed=True,
        candidate_sha256=cand_sha,
        viewports=viewports,
    )
    (project_root / "qa" / "qa-report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def test_pilot_specs_contain_five_distinct_archetypes() -> None:
    assert len(PILOT_BUSINESS_SPECS) == 5
    slugs = [spec.slug for spec in PILOT_BUSINESS_SPECS]
    assert len(set(slugs)) == 5
    archetypes = [spec.archetype for spec in PILOT_BUSINESS_SPECS]
    expected_archetypes = {
        "classic_salon",
        "mobile_groomer",
        "mixed_daycare_spa",
        "minimalist_inquiry",
        "social_heavy",
    }
    assert set(archetypes) == expected_archetypes


def test_single_pilot_mobile_groomer_taxonomy_and_call_cta(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)

    spec = next(s for s in PILOT_BUSINESS_SPECS if s.archetype == "mobile_groomer")
    result = run_single_pilot_business(spec, projects_dir=projects_dir, qa_runner=_mock_qa_runner)

    assert result.status == "awaiting_approval"
    assert result.taxonomy.is_mobile_groomer is True
    assert result.taxonomy.is_salon is False
    assert result.primary_cta_target is not None
    assert result.primary_cta_target.startswith("tel:")

    # Candidate generation is complete, but only a human can freeze a release.
    project_root = projects_dir / spec.slug
    assert (project_root / "release-candidate/ghl-ready.html").is_file()
    assert not (project_root / "delivery/release-manifest.json").exists()


def test_single_pilot_minimalist_site_zero_fabrication(tmp_path: Path) -> None:
    """Minimalist site with no pricing listed must NOT fabricate prices."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)

    spec = next(s for s in PILOT_BUSINESS_SPECS if s.archetype == "minimalist_inquiry")
    result = run_single_pilot_business(spec, projects_dir=projects_dir, qa_runner=_mock_qa_runner)

    assert result.status == "awaiting_approval"
    # Pricing is not verified, so it must fall back to inquiry CTA or consultation
    brief_file = projects_dir / spec.slug / "design/creative-brief.json"
    brief_data = json.loads(brief_file.read_text(encoding="utf-8"))
    assert "Book" in brief_data["primary_cta_label"] or "Inquire" in brief_data["primary_cta_label"]

    # Verify business-facts.json contains no fabricated pricing
    facts_file = projects_dir / spec.slug / "research/business-facts.json"
    facts_data = json.loads(facts_file.read_text(encoding="utf-8"))
    for f in facts_data.get("facts", []):
        if f.get("category") == "pricing":
            assert f.get("is_verified") is False or f.get("status") == "needs_review"


def test_execute_acceptance_pilot_runs_all_five_and_generates_report(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True)

    pilot_report: PilotAcceptanceReport = execute_acceptance_pilot(
        projects_dir=projects_dir,
        qa_runner=_mock_qa_runner,
    )

    assert pilot_report.total_businesses == 5
    assert pilot_report.review_ready_businesses == 5
    assert pilot_report.failed_businesses == 0
    assert len(pilot_report.results) == 5

    # Check report files generated
    report_json = projects_dir / "_pilot/pilot-report.json"
    report_md = projects_dir / "_pilot/pilot-report.md"
    assert report_json.is_file()
    assert report_md.is_file()

    md_content = report_md.read_text(encoding="utf-8")
    assert "# EditVista Milestone 5 — Five-Business Acceptance Pilot Report" in md_content
    assert "Classic Luxury Salon" in md_content
    assert "Mobile Grooming Van" in md_content
    has_mixed_label = (
        "Mixed Species Daycare &amp; Spa" in md_content
        or "Mixed Species Daycare & Spa" in md_content
    )
    assert has_mixed_label is True
    assert "Minimalist Studio" in md_content
    assert "Social-Heavy" in md_content

    # Each candidate awaits a separate human review and immutable release decision.
    for res in pilot_report.results:
        p_dir = projects_dir / res.slug
        assert res.status == "awaiting_approval"
        assert not (p_dir / "delivery" / "release-manifest.json").exists()
        assert res.zero_fabrication_passed is True


def test_pilot_cli_accepts_live_flag_and_wires_live_qa_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI main() must accept --live as documented in README.md and wire the live QA runner."""
    import app.pilot as pilot_mod

    projects_dir = tmp_path / "projects_live"
    projects_dir.mkdir(parents=True)

    called_live = {"count": 0}

    def _spy_live_qa(project_folder: str | Path, **kwargs: object) -> QADefectReport:
        called_live["count"] += 1
        return _mock_qa_runner(project_folder, **kwargs)

    monkeypatch.setattr(pilot_mod, "run_qa_engine", _spy_live_qa, raising=False)

    exit_code = pilot_mod.main(
        [
            "--business",
            PILOT_BUSINESS_SPECS[0].slug,
            "--live",
            "--projects-dir",
            str(projects_dir),
        ]
    )
    assert exit_code == 0
    assert called_live["count"] >= 2
