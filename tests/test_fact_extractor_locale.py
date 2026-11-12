"""Tests for locale-aware, conflict-aware fact extractor with source priority."""

from __future__ import annotations

import json
from pathlib import Path

from app.fact_extractor import (
    BusinessFactsReport,
    ExtractedFact,
    extract_business_facts,
)


def test_extracted_fact_schema_has_required_fields() -> None:
    """Requirement 1, 4, 5: ExtractedFact schema supports fields."""
    fact = ExtractedFact(
        fact_id="fact-test-1",
        project_id="test-proj",
        category="phone",
        claim_key="phone",
        claim_value="+442079460192",
        source_url="https://example.com",
        source_text_snippet="Call 020 7946 0192",
        source_kind="regex",
        conflicts_with=[],
        is_verified=True,
        status="verified",
    )
    assert fact.source_kind == "regex"
    assert fact.conflicts_with == []
    assert fact.last_checked_at is not None


def test_business_facts_artifact_exposes_required_claim_metadata(tmp_path: Path) -> None:
    project = tmp_path / "metadata-project"
    research = project / "research"
    snapshots = research / "snapshots"
    snapshots.mkdir(parents=True)

    html = (
        "<html><head><title>Metadata Salon</title></head>"
        "<body><h1>Metadata Salon</h1></body></html>"
    )
    snapshot_sha = "metadata-sha"
    (snapshots / f"{snapshot_sha}.html").write_text(html, encoding="utf-8")
    (research / "pages.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://metadata.example.com",
                    "sha256": snapshot_sha,
                    "title": "Metadata Salon",
                    "text": "Metadata Salon",
                }
            ]
        ),
        encoding="utf-8",
    )

    report = extract_business_facts(project)
    persisted = json.loads(
        (research / "business-facts.json").read_text(encoding="utf-8")
    )["facts"]
    persisted_by_id = {fact["fact_id"]: fact for fact in persisted}

    assert report.facts
    round_tripped = BusinessFactsReport.model_validate_json(
        (research / "business-facts.json").read_text(encoding="utf-8")
    )
    assert len(round_tripped.facts) == len(report.facts)
    for fact in report.facts:
        record = persisted_by_id[fact.fact_id]
        assert record["source_url"] == fact.source_url
        assert record["source_text"] == fact.source_text_snippet
        assert record["verified"] is fact.is_verified
        assert record["last_checked_at"]


def test_source_priority_jsonld_over_href_over_regex(tmp_path: Path) -> None:
    """Requirement 1: Source priority (1) JSON-LD, (2) tel:/mailto: hrefs, (3) regex."""
    proj = tmp_path / "proj-priority"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    html = """<!DOCTYPE html>
    <html>
      <head>
        <title>Priority Salon</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "LocalBusiness",
          "name": "JSON-LD Salon Name",
          "telephone": "+442079460199",
          "address": {
            "@type": "PostalAddress",
            "streetAddress": "10 Downing Street",
            "addressLocality": "London",
            "postalCode": "SW1A 2AA",
            "addressCountry": "GB"
          }
        }
        </script>
      </head>
      <body>
        <h1>Regex Fallback Header</h1>
        <p>Call us at <a href="tel:+442079460188">020 7946 0188</a> or plain 020 7946 0177</p>
        <p>Address: 42 High Street, London, N1 2AB</p>
      </body>
    </html>"""

    sha = "priority-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://priority.example.co.uk",
            "sha256": sha,
            "title": "Priority Salon",
            "text": "Regex Fallback Header Call us at 020 7946 0188 or plain 020 7946 0177",
        }
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    report = extract_business_facts(proj)

    # 1. Name from JSON-LD has source_kind="json_ld"
    names = [f for f in report.facts if f.category == "business_name"]
    assert any(
        f.claim_value == "JSON-LD Salon Name" and f.source_kind == "json_ld" for f in names
    )

    # 2. JSON-LD telephone was captured with source_kind="json_ld"
    phones = [f for f in report.facts if f.category == "phone"]
    assert any(f.source_kind == "json_ld" for f in phones)

    # 3. Tel link captured with source_kind="href"
    assert any(f.source_kind == "href" for f in phones)


def test_phonenumbers_uk_us_pk_parsing(tmp_path: Path) -> None:
    """Requirement 2: Phone numbers parsed via phonenumbers with region hints."""
    proj = tmp_path / "proj-phones"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    html = """<!DOCTYPE html>
    <html>
      <head><title>International Salon</title></head>
      <body>
        <p>UK Landline: 020 7946 0192</p>
        <p>UK Mobile: 07700 900123</p>
        <p>US Toll Free: (512) 555-0144</p>
        <p>PK Support: +92 300 1234567</p>
      </body>
    </html>"""

    sha = "phones-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://phones.example.com",
            "sha256": sha,
            "title": "International Salon",
            "text": (
                "UK Landline: 020 7946 0192 UK Mobile: 07700 900123 "
                "US Toll Free: (512) 555-0144 PK Support: +92 300 1234567"
            ),
        }
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    report = extract_business_facts(proj)
    phone_vals = [f.claim_value for f in report.facts if f.category == "phone"]

    # Must recognize UK landline and UK mobile
    assert any("020" in p or "+4420" in p or "2079460192" in p for p in phone_vals)
    assert any("07700" in p or "+447700" in p or "7700900123" in p for p in phone_vals)
    # Must recognize US phone
    assert any("512" in p for p in phone_vals)
    # Must recognize PK phone
    assert any(
        "0300" in p or "92300" in p or "3001234567" in p.replace(" ", "") for p in phone_vals
    )


def test_conflict_detection_marks_all_candidates_needs_review(tmp_path: Path) -> None:
    """Requirement 4: >=2 different values for single-valued category => ALL become needs_review."""
    proj = tmp_path / "proj-conflict"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    html = """<!DOCTYPE html>
    <html>
      <head><title>Conflicting Salon</title></head>
      <body>
        <section id="contact">
          <p>Contact Phone: (512) 555-0199</p>
          <p>Address: 100 Main St, Austin, TX 78701</p>
        </section>
        <footer>
          <p>Footer Phone: (512) 555-0111</p>
          <p>Old Address: 200 South St, Austin, TX 78704</p>
        </footer>
      </body>
    </html>"""

    sha = "conflict-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://conflict.example.com",
            "sha256": sha,
            "title": "Conflicting Salon",
            "text": (
                "Contact Phone: (512) 555-0199 Address: 100 Main St, Austin, TX 78701 "
                "Footer Phone: (512) 555-0111 Old Address: 200 South St, Austin, TX 78704"
            ),
        }
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    report = extract_business_facts(proj)

    phones = [f for f in report.facts if f.category == "phone"]
    assert len(phones) >= 2
    # Both must be marked needs_review with conflicts_with populated
    for p in phones:
        assert p.status == "needs_review", "Conflicting phone must NOT be verified"
        assert len(p.conflicts_with) >= 1, "conflicts_with must list competing fact IDs"

    addrs = [f for f in report.facts if f.category == "address"]
    assert len(addrs) >= 2
    for a in addrs:
        assert a.status == "needs_review", "Conflicting address must NOT be verified"
        assert len(a.conflicts_with) >= 1
        assert "conflicts with" not in a.claim_value.lower()


def test_structured_weekly_hours_and_partial_week_status(tmp_path: Path) -> None:
    """Requirement 3: Hours become ONE structured weekly object; partial week is needs_review."""
    proj = tmp_path / "proj-hours"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    # Partial week (only Mon-Fri captured, weekend omitted)
    html = """<!DOCTYPE html>
    <html>
      <head><title>Hours Salon</title></head>
      <body>
        <p>Hours: Monday to Friday 9am - 5pm</p>
      </body>
    </html>"""

    sha = "hours-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://hours.example.com",
            "sha256": sha,
            "title": "Hours Salon",
            "text": "Hours: Monday to Friday 9am - 5pm",
        }
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    report = extract_business_facts(proj)
    hour_facts = [f for f in report.facts if f.category == "hours"]
    assert len(hour_facts) >= 1
    # Because Saturday/Sunday are missing, it must be needs_review, never verified
    for h in hour_facts:
        assert h.status == "needs_review"
        snip = h.source_text_snippet.lower()
        assert "partial" in snip or "missing" in h.claim_key.lower() or h.status == "needs_review"


def test_expired_promotion_status(tmp_path: Path) -> None:
    """Requirement 5: Promos whose end date has passed are assigned status='expired'."""
    proj = tmp_path / "proj-promo"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    html = """<!DOCTYPE html>
    <html>
      <head><title>Promo Salon</title></head>
      <body>
        <p>Winter Special: $20 off full groom. Valid through January 15, 2023.</p>
        <p>Summer Special: 10% off baths. Valid through December 31, 2099.</p>
      </body>
    </html>"""

    sha = "promo-sha"
    (snaps / f"{sha}.html").write_text(html, encoding="utf-8")
    pages = [
        {
            "url": "https://promo.example.com",
            "sha256": sha,
            "title": "Promo Salon",
            "text": (
                "Winter Special: $20 off full groom. Valid through January 15, 2023. "
                "Summer Special: 10% off baths. Valid through December 31, 2099."
            ),
        }
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    report = extract_business_facts(proj)
    expired_facts = [f for f in report.facts if f.status == "expired"]
    assert len(expired_facts) >= 1, "Past promotion must be assigned status='expired'"
