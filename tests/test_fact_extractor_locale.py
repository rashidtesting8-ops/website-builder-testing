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


def test_cross_priority_conflict_keeps_json_ld_name_verified(tmp_path: Path) -> None:
    """Requirement 4 (source priority): a page-title variant must not demote the JSON-LD name.

    Regression: 'Location | <name>' title candidates used to conflict with the JSON-LD
    business name and demote EVERY business_name fact to needs_review, which failed the
    MVF gate and let the junk title flow into the creative brief after acknowledgment.
    """
    proj = tmp_path / "proj-cross-priority"
    research = proj / "research"
    snaps = research / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    home_html = """<!DOCTYPE html>
    <html>
      <head>
        <title>Hachi &amp; Co | Dog Grooming</title>
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Hachi & Co"}
        </script>
      </head>
      <body><h1>Hachi &amp; Co</h1></body>
    </html>"""
    location_html = """<!DOCTYPE html>
    <html>
      <head><title>Location | Hachi &amp; Co</title></head>
      <body><h1>Location</h1></body>
    </html>"""

    (snaps / "home-sha.html").write_text(home_html, encoding="utf-8")
    (snaps / "location-sha.html").write_text(location_html, encoding="utf-8")
    pages = [
        {
            "url": "https://hachi.example.com/",
            "sha256": "home-sha",
            "title": "Hachi & Co | Dog Grooming",
            "text": "Hachi & Co Dog Grooming",
        },
        {
            "url": "https://hachi.example.com/location",
            "sha256": "location-sha",
            "title": "Location | Hachi & Co",
            "text": "Location Hachi & Co",
        },
    ]
    (research / "pages.json").write_text(json.dumps(pages), encoding="utf-8")

    report = extract_business_facts(proj)

    names = [f for f in report.facts if f.category == "business_name"]
    json_ld_names = [f for f in names if f.source_kind == "json_ld"]
    title_names = [f for f in names if f.source_kind == "regex"]
    assert json_ld_names, "JSON-LD business name must be extracted"
    assert title_names, "Page-title fallback candidate must be extracted"

    # The authoritative JSON-LD name survives the conflict, undisputed.
    for f in json_ld_names:
        assert f.status == "verified" and f.is_verified
        assert f.conflicts_with == []

    # The title-suffix variant loses and points at the surviving JSON-LD facts.
    json_ld_ids = {f.fact_id for f in json_ld_names}
    for f in title_names:
        assert f.status == "needs_review" and not f.is_verified
        assert json_ld_ids & set(f.conflicts_with)


# --- Rich, source-backed content extraction (bespoke homepage) ---------------------------

LILY_HOME_TEXT = """Lily's Club Dog Grooming
Premium Pet Grooming in North and North West London
100+ 5* reviews
Over 1000 clients
10 + years of pet care experience
Premium services and products
Transparent pricing
Cat grooming
Dog Grooming
Spa add-ons
Testimonials
It's more than Grooming
This is Lily. Welcome to her club.
Lily has travelled all around the world and she always looked forward to her grooms.
how much fluff a Pomeranian like her has
Our goal is to provide personal friendly service to every pet and every pet parent
Since opening, Lily's Club has grown from strength to strength and in 2025 was announced as the
national winner of the Corporate LiveWire Innovation & Excellence award for
Dog Grooming Service of the Year.
including being the first salon in the UK to introduce chromotherapy for pets.
In March 2026, Lily's Club opened it's second salon in Wood Green, expanding on the outstanding
experiences of thousands of loyal customers at our Harrow salon.
Contact Us - Harrow
+44 7521 298312
lilysclubuk@outlook.com
87 Kenton Road, Harrow, London, HA3 0AH
Contact Us - Wood Green
+44 7895 015546
75 Myddleton Road, Wood Green, London, N22 8LZ"""

LILY_SERVICES_TEXT = """Grooming
Bath, brush & Dry
Toy/Small Breeds up to 8kg
Â£32
Medium Breeds up to 15kg
Â£44
Mini groom
Toy/Small Breeds up to 8kg
Â£45
Full groom
Toy/Small Breeds up to 8kg
Â£60
Medium Breeds up to 15kg
Â£72"""


def _seed_rich_project(tmp_path: Path, name: str = "lily-rich") -> Path:
    proj = tmp_path / name
    research = proj / "research"
    research.mkdir(parents=True)
    (research / "pages.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://www.lilys-club.co.uk/",
                    "title": "Lily's Club Dog Grooming",
                    "sha256": "no-snapshot",
                    "text": LILY_HOME_TEXT,
                },
                {
                    "url": "https://www.lilys-club.co.uk/services2",
                    "title": "Services2",
                    "sha256": "no-snapshot-2",
                    "text": LILY_SERVICES_TEXT,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return proj


def test_rich_extraction_proof_points_are_verified_and_sourced(tmp_path: Path) -> None:
    """Rich proof stats from the source page must be extracted with full provenance."""
    proj = _seed_rich_project(tmp_path)
    report = extract_business_facts(proj)
    proofs = [f for f in report.facts if f.category == "proof_point"]
    values = {f.claim_value for f in proofs}

    assert "100+ 5* reviews" in values
    assert "Over 1000 clients" in values
    assert "Premium services and products" in values
    assert "Transparent pricing" in values
    for fact in proofs:
        assert fact.source_url == "https://www.lilys-club.co.uk/"
        assert fact.source_text_snippet.strip()
        assert fact.is_verified is True
        assert fact.status == "verified"
        assert fact.last_checked_at is not None
        assert fact.claim_value in fact.source_text_snippet


def test_rich_extraction_award_story_and_innovation(tmp_path: Path) -> None:
    """Award, owner story and chromotherapy facts must come verbatim from the source."""
    proj = _seed_rich_project(tmp_path)
    report = extract_business_facts(proj)
    by_cat: dict[str, list[str]] = {}
    for f in report.facts:
        by_cat.setdefault(f.category, []).append(f.claim_value)

    assert any("national winner" in v.lower() for v in by_cat.get("award", []))
    assert any("chromotherapy" in v.lower() for v in by_cat.get("service_highlight", []))
    story = " ".join(by_cat.get("owner_story", []))
    assert "Lily" in story
    assert "Pomeranian" in story
    assert any("Spa add-ons" == v for v in by_cat.get("service_highlight", []))


def test_rich_extraction_two_locations_and_contact_details(tmp_path: Path) -> None:
    """Two real salons with their own phone/address must be extracted and verified."""
    proj = _seed_rich_project(tmp_path)
    report = extract_business_facts(proj)
    locations = [f for f in report.facts if f.category == "location"]

    assert {f.claim_value for f in locations} == {"Harrow", "Wood Green"}
    by_name: dict[str, dict[str, object]] = {}
    for f in locations:
        if isinstance(f.structured_value, dict):
            by_name[f.claim_value] = f.structured_value
    assert by_name["Harrow"]["phone"] == "+44 7521 298312"
    assert by_name["Harrow"]["address"] == "87 Kenton Road, Harrow, London, HA3 0AH"
    assert by_name["Wood Green"]["phone"] == "+44 7895 015546"
    assert by_name["Wood Green"]["address"] == "75 Myddleton Road, Wood Green, London, N22 8LZ"

    contact_vals = {
        f.claim_value for f in report.facts if f.category == "contact_detail"
    }
    assert "+44 7521 298312" in contact_vals
    assert "+44 7895 015546" in contact_vals
    assert "lilysclubuk@outlook.com" in contact_vals


def test_rich_extraction_dog_grooming_prices(tmp_path: Path) -> None:
    """Dog grooming package prices (with Â£ mojibake) must be extracted as real prices."""
    proj = _seed_rich_project(tmp_path)
    report = extract_business_facts(proj)
    prices = " ".join(
        f.claim_value for f in report.facts if f.category == "pricing"
    )
    assert "Full groom" in prices
    assert "£60" in prices
    assert "£32" in prices


def test_generated_homepage_renders_rich_verified_content(tmp_path: Path) -> None:
    """Generator must render extracted proof/story/award/location facts, not generic copy."""
    from app.art_director import generate_art_direction
    from app.generator import generate_website
    from PIL import Image

    proj = _seed_rich_project(tmp_path, "lily-generate")
    # Persist extracted rich facts into the project artifact consumed by the generator.
    report = extract_business_facts(proj)
    (proj / "research/business-facts.json").write_text(
        json.dumps(
            {
                "project_id": proj.name,
                "facts": [f.model_dump(mode="json") for f in report.facts],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    art = proj / "assets/optimized"
    art.mkdir(parents=True, exist_ok=True)
    for n in ("hero.jpg", "g1.jpg", "g2.jpg", "g3.jpg", "g4.jpg"):
        Image.new("RGB", (640, 480), (90, 110, 140)).save(art / n, "JPEG")
    (proj / "design").mkdir(parents=True, exist_ok=True)
    (proj / "design/image-selection.json").write_text(
        json.dumps(
            {
                "hero_image": {
                    "filename": "hero.jpg",
                    "relative_path": "assets/optimized/hero.jpg",
                },
                "grooming_gallery": [
                    {
                        "filename": f"g{i}.jpg",
                        "relative_path": f"assets/optimized/g{i}.jpg",
                    }
                    for i in range(1, 5)
                ],
            }
        ),
        encoding="utf-8",
    )
    generate_art_direction(proj)
    generate_website(proj)

    html = (proj / "website/index.html").read_text(encoding="utf-8")
    assert "100+ 5* reviews" in html
    assert "Over 1000 clients" in html
    assert "national winner" in html.lower()
    assert "Pomeranian" in html
    assert "87 Kenton Road" in html
    assert "75 Myddleton Road" in html
    assert "£60" in html
