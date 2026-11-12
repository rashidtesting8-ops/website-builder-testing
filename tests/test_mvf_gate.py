"""Tests for Minimum Viable Facts (MVF) gate and contact CTA enforcement."""

from __future__ import annotations

import json
from pathlib import Path

from app.fact_extractor import extract_business_facts
from app.mvf_gate import check_mvf, enforce_mvf_gate
from app.pipeline import run_pipeline


def _setup_mock_project(
    tmp_path: Path,
    slug: str,
    *,
    html: str,
) -> Path:
    proj = tmp_path / slug
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    sha = f"sha-{slug}"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": f"https://{slug}.example.com",
            "sha256": sha,
            "title": f"Title {slug}",
            "text": html,
        }
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")
    return proj


def test_extract_classification_facts_animal_types_and_business_model(tmp_path: Path) -> None:
    """Requirement 1: Extract booking URL, animal types and business model."""
    html = """<!DOCTYPE html>
    <html>
      <head><title>Bark & Paws Mobile Dog Spa</title></head>
      <body>
        <h1>Bark & Paws Mobile Dog Spa</h1>
        <p>Luxury mobile dog grooming van at your doorstep across Surrey</p>
        <p><a href="https://booking.example.com/reserve">Book Appointment Online</a></p>
        <p>Phone: 020 7946 0192</p>
      </body>
    </html>"""
    proj = _setup_mock_project(tmp_path, "mobile-dog-spa", html=html)
    report = extract_business_facts(proj, extract_classification=True)

    facts_by_cat = {f.category: f for f in report.facts}

    # 1. Booking URL extracted
    assert "booking_url" in facts_by_cat
    assert "booking.example.com" in facts_by_cat["booking_url"].claim_value
    assert facts_by_cat["booking_url"].status == "verified"

    # 2. Animal types classified
    assert "animal_types" in facts_by_cat
    assert "dog" in facts_by_cat["animal_types"].claim_value
    assert facts_by_cat["animal_types"].status == "verified"

    # 3. Business model classified as mobile
    assert "business_model" in facts_by_cat
    assert "mobile" in facts_by_cat["business_model"].claim_value
    assert facts_by_cat["business_model"].status == "verified"


def test_classification_uncertain_becomes_needs_review(tmp_path: Path) -> None:
    """Requirement 1: If classification is uncertain or ambiguous, mark status as needs_review."""
    html = """<!DOCTYPE html>
    <html>
      <head><title>Generic Grooming Co</title></head>
      <body>
        <h1>Generic Grooming Co</h1>
        <p>Professional grooming services for all clients.</p>
        <p>Phone: 020 7946 0192</p>
      </body>
    </html>"""
    proj = _setup_mock_project(tmp_path, "generic-grooming", html=html)
    report = extract_business_facts(proj, extract_classification=True)

    facts_by_cat = {f.category: f for f in report.facts}
    assert "business_model" in facts_by_cat
    # When text gives no clear indication whether salon, mobile, or daycare
    assert facts_by_cat["business_model"].status == "needs_review"


def test_mvf_check_passes_when_all_criteria_met(tmp_path: Path) -> None:
    """Requirement 2: MVF = verified name + verified contact path + verified service type."""
    html = """<!DOCTYPE html>
    <html>
      <head><title>Highland Paws Dog Salon</title></head>
      <body>
        <h1>Highland Paws Dog Salon</h1>
        <p>Dedicated canine salon on 12 High Street.</p>
        <p>Phone: 020 7946 0192</p>
      </body>
    </html>"""
    proj = _setup_mock_project(tmp_path, "highland-paws", html=html)
    extract_business_facts(proj, extract_classification=True)

    mvf = check_mvf(proj)
    assert mvf.passed is True
    assert len(mvf.missing_items) == 0
    assert "business_name" in mvf.verified_items
    assert "contact_path" in mvf.verified_items
    assert "service_type" in mvf.verified_items


def test_page_title_suffix_does_not_break_verified_business_name_for_multi_branch_site(
    tmp_path: Path,
) -> None:
    """Business names should resolve to the same brand when titles include section names."""
    html = """<!DOCTYPE html>
    <html>
      <head><title>About us – Lily's Club Dog Grooming</title></head>
      <body>
        <h1>Lily's Club Dog Grooming</h1>
        <p>Dog grooming salon in Harrow and Wood Green.</p>
        <p><a href="https://www.lilys-club.co.uk/book-an-appointment-2">Book Appointment</a></p>
        <p>Harrow phone: +44 7521 298312</p>
        <p>Wood Green phone: +44 7895 015546</p>
      </body>
    </html>"""
    proj = tmp_path / "lilys-club"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    sha = "lilys-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [{
        "url": "https://www.lilys-club.co.uk/about-us-2",
        "sha256": sha,
        "title": "About us – Lily's Club Dog Grooming",
        "text": html,
    }]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")
    report = extract_business_facts(proj, extract_classification=True)

    names = [f for f in report.facts if f.category == "business_name"]
    assert names
    assert any(
        f.is_verified and f.status == "verified" and "Lily's Club Dog Grooming" in f.claim_value
        for f in names
    )
    assert any(f.category == "booking_url" and f.is_verified for f in report.facts)
    assert any(f.category == "phone" and f.is_verified for f in report.facts)

    mvf = check_mvf(proj)
    assert mvf.passed is True
    assert "business_name" in mvf.verified_items
    assert "contact_path" in mvf.verified_items
    assert "service_type" in mvf.verified_items


def test_mvf_check_fails_and_writes_report_when_contact_path_missing(tmp_path: Path) -> None:
    """Requirement 2 & 3: MVF fails if contact path missing; writes qa/mvf-report.json."""
    # HTML with business name and animal/model, but NO phone, booking URL, or email
    html = """<!DOCTYPE html>
    <html>
      <head><title>Silent Paws Dog Salon</title></head>
      <body>
        <h1>Silent Paws Dog Salon</h1>
        <p>Bespoke dog grooming boutique in Mayfair.</p>
      </body>
    </html>"""
    proj = _setup_mock_project(tmp_path, "silent-paws", html=html)
    extract_business_facts(proj, extract_classification=True)

    report = enforce_mvf_gate(proj, acknowledge_gaps=False)
    assert report.passed is False
    assert "contact_path" in report.missing_items
    assert "contact" in report.exact_human_action_needed.lower()

    # qa/mvf-report.json must be written
    report_file = proj / "qa" / "mvf-report.json"
    assert report_file.is_file()
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["passed"] is False
    assert "contact_path" in data["missing_items"]
    assert len(data["exact_human_action_needed"]) > 0


def test_pipeline_blocks_at_needs_review_without_acknowledge_gaps(tmp_path: Path) -> None:
    """Requirement 3: Without --acknowledge-gaps, project cannot reach awaiting_approval."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    # Site with no phone or contact path
    html = """<!DOCTYPE html>
    <html>
      <head><title>Unreachable Groomers</title></head>
      <body>
        <h1>Unreachable Groomers</h1>
        <p>Dog grooming salon without phone or email.</p>
      </body>
    </html>"""
    slug = "unreachable-groomers"
    proj = projects_dir / slug
    snaps = proj / "research" / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    sha = "unreachable-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://unreachable.example.com",
            "sha256": sha,
            "title": "Unreachable Groomers",
            "text": html,
        }
    ]
    (proj / "research" / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    db_path = tmp_path / "test.db"
    res = run_pipeline(
        url="https://unreachable.example.com",
        business_name="Unreachable Groomers",
        business_id=slug,
        projects_dir=projects_dir,
        db_path=db_path,
        resume=True,
        acknowledge_gaps=False,
    )
    assert (
        res.status == "needs_review"
    ), "Must NOT reach awaiting_approval when MVF fails without acknowledge_gaps"

    mvf_report_path = proj / "qa" / "mvf-report.json"
    assert mvf_report_path.is_file()
    mvf_data = json.loads(mvf_report_path.read_text(encoding="utf-8"))
    assert mvf_data["passed"] is False
    assert mvf_data["acknowledged"] is False


def test_pipeline_proceeds_to_awaiting_approval_with_acknowledge_gaps(tmp_path: Path) -> None:
    """Requirement 3: With --acknowledge-gaps, pipeline proceeds and records gaps."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    html = """<!DOCTYPE html>
    <html>
      <head><title>Gap Accepted Groomers</title></head>
      <body>
        <h1>Gap Accepted Groomers</h1>
        <p>Dog grooming salon.</p>
      </body>
    </html>"""
    slug = "gap-accepted-groomers"
    proj = projects_dir / slug
    snaps = proj / "research" / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    sha = "gap-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://gap.example.com",
            "sha256": sha,
            "title": "Gap Accepted Groomers",
            "text": html,
        }
    ]
    (proj / "research" / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    db_path = tmp_path / "test.db"
    res = run_pipeline(
        url="https://gap.example.com",
        business_name="Gap Accepted Groomers",
        business_id=slug,
        projects_dir=projects_dir,
        db_path=db_path,
        resume=True,
        acknowledge_gaps=True,
    )
    assert res.status == "awaiting_approval"

    # review-summary.json must record acknowledged gaps
    summary_path = proj / "qa" / "review-summary.json"
    assert summary_path.is_file()
    summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert "acknowledged_gaps" in summary_data or "mvf_report" in summary_data


def test_generator_cta_omitted_when_no_verified_contact_path_exists(tmp_path: Path) -> None:
    """Requirement 4: Generator must never emit CTA to #contact when no verified contact path."""
    proj = tmp_path / "no-contact-proj"
    website = proj / "website"
    website.mkdir(parents=True, exist_ok=True)

    # Simulate generated html that initially had CTA to #contact
    index_html = """<!DOCTYPE html>
    <html>
      <body>
        <header>
          <a class="ev-cta-button" href="#contact">Book Grooming Appointment</a>
        </header>
        <section id="hero">
          <a class="ev-hero-cta" href="#contact">Contact Us Today</a>
        </section>
      </body>
    </html>"""
    (website / "index.html").write_text(index_html, encoding="utf-8")

    # Facts with NO verified contact path
    facts = [
        {
            "category": "business_name",
            "claim_key": "business_name",
            "claim_value": "No Contact Salon",
            "status": "verified",
            "is_verified": True,
        }
    ]
    (proj / "research").mkdir(parents=True, exist_ok=True)
    (proj / "research" / "business-facts.json").write_text(
        json.dumps({"project_id": "no-contact-proj", "facts": facts}),
        encoding="utf-8",
    )

    report = enforce_mvf_gate(proj, acknowledge_gaps=True)
    assert report.cta_omitted is True

    updated_html = (website / "index.html").read_text(encoding="utf-8")
    assert (
        'href="#contact"' not in updated_html
    ), "Must NEVER emit CTA to #contact when no verified contact path exists"
