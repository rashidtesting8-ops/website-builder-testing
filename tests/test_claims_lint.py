"""Tests for claims linter, UI microcopy allowlist, and zero-fabrication gating."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from app.claims_lint import ClaimsLintResult, lint_html_claims
from app.pipeline import run_pipeline
from tests.test_pipeline_e2e import (
    _deterministic_qa_runner,
    _ephemeral_grooming_salon_server,
)

EVIDENCE_STRINGS = [
    "hydro-massage baths",
    "Species-segregated, tranquil spa suites",
    "Dedicated feline quiet hours",
    "verified coat-safe botanicals",
    "cared for by our stylists",
    "Verified Client Portrait #1",
    "Verified by EditVista",
    "cage-free",
    "Zero kennel wait times",
]


def test_claims_linter_flags_all_unverified_evidence_strings(tmp_path: Path) -> None:
    """Requirement 1: The linter must flag every string in the evidence list."""
    html = f"""<!DOCTYPE html>
    <html>
      <head><title>Test Salon</title></head>
      <body>
        <h1>Test Salon</h1>
        <p>{EVIDENCE_STRINGS[0]}</p>
        <p>{EVIDENCE_STRINGS[1]}</p>
        <p>{EVIDENCE_STRINGS[2]}</p>
        <p>{EVIDENCE_STRINGS[3]}</p>
        <p>{EVIDENCE_STRINGS[4]}</p>
        <figure>
          <img src="photo1.jpg" alt="Pet photo">
          <figcaption>{EVIDENCE_STRINGS[5]}</figcaption>
        </figure>
        <p>{EVIDENCE_STRINGS[7]}</p>
        <p>{EVIDENCE_STRINGS[8]}</p>
        <footer>{EVIDENCE_STRINGS[6]}</footer>
      </body>
    </html>"""

    # Facts do NOT contain any of the fabricated claims
    verified_facts = [
        {
            "category": "business_name",
            "claim_value": "Test Salon",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "phone",
            "claim_value": "020 7946 0192",
            "is_verified": True,
            "status": "verified",
        },
    ]

    result: ClaimsLintResult = lint_html_claims(
        html_content=html,
        verified_facts=verified_facts,
    )

    assert result.passed is False
    assert len(result.violations) >= len(EVIDENCE_STRINGS)

    flagged_texts = [v["text"] for v in result.violations]
    for evidence in EVIDENCE_STRINGS:
        assert any(
            evidence.lower() in t.lower() for t in flagged_texts
        ), f"Linter failed to flag evidence string: '{evidence}'"


def test_claims_linter_banned_words_unless_fact_backed(tmp_path: Path) -> None:
    """Requirement 2: Banned words (verified, authentic, luxury, bespoke, premium, certified,

    cage-free, botanical, organic, award-winning) must be flagged unless in verified facts.
    """
    html = """<!DOCTYPE html>
    <html>
      <body>
        <h1>Luxury Paws Bespoke Spa</h1>
        <p>Our authentic, certified organic, award-winning botanical wash is premium.</p>
      </body>
    </html>"""

    # Only "Luxury" is fact-backed in business name; others are unbacked
    facts = [
        {
            "category": "business_name",
            "claim_value": "Luxury Paws",
            "is_verified": True,
            "status": "verified",
        },
    ]

    result = lint_html_claims(html_content=html, verified_facts=facts)
    assert result.passed is False
    flagged = " ".join(v["text"].lower() for v in result.violations)

    banned_words = (
        "authentic",
        "certified",
        "organic",
        "award-winning",
        "botanical",
        "premium",
        "bespoke",
    )
    for word in banned_words:
        assert word in flagged, f"Expected banned word '{word}' to be flagged"


def test_claims_linter_digits_and_currency_must_match_fact_exactly() -> None:
    """Requirement 2: Anything with digits/currency must match a verified fact exactly."""
    html = """<!DOCTYPE html>
    <html>
      <body>
        <p>Call (512) 555-0144</p>
        <p>Full Groom: $85.00</p>
        <p>Over 500 happy dogs groomed!</p>
        <p>Save £20 this week!</p>
      </body>
    </html>"""

    facts = [
        {
            "category": "phone",
            "claim_value": "(512) 555-0144",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "pricing",
            "claim_value": "Full Groom: $85.00",
            "is_verified": True,
            "status": "verified",
        },
    ]

    result = lint_html_claims(html_content=html, verified_facts=facts)
    assert result.passed is False

    violation_texts = [v["text"] for v in result.violations]
    # "Over 500 happy dogs groomed!" contains digit 500 not in facts
    assert any("500" in t for t in violation_texts)
    # "Save £20 this week!" contains £20 not in facts
    assert any("20" in t for t in violation_texts)
    # Verified phone and price must NOT be flagged as violations
    assert not any("(512) 555-0144" == t for t in violation_texts)
    assert not any("Full Groom: $85.00" == t for t in violation_texts)


def test_claims_linter_passes_on_fact_backed_and_allowlisted_content() -> None:
    """Requirement 2 & 3: Clean, fact-backed HTML with neutral alts passes linter."""
    html = """<!DOCTYPE html>
    <html>
      <head><title>Bark &amp; Bliss Pet Grooming Salon</title></head>
      <body>
        <header>
          <a href="#top">Bark &amp; Bliss Pet Grooming Salon</a>
          <button type="button">Menu</button>
          <nav>
            <a href="#services">Services</a>
            <a href="#gallery">Gallery</a>
            <a href="#contact">Contact</a>
          </nav>
        </header>
        <main>
          <section id="hero">
            <span>Dog &amp; Cat Grooming</span>
            <h1>Bark &amp; Bliss Pet Grooming Salon</h1>
            <p>Grooming services for dogs and cats.</p>
            <a href="tel:5125550144">Call (512) 555-0144</a>
            <a href="#services">Services</a>
            <img src="hero.jpg" alt="Bark &amp; Bliss Pet Grooming Salon photo 1">
          </section>
          <section id="services">
            <h2>Grooming Services &amp; Pricing</h2>
            <div>
              <h3>Full Dog Groom &amp; Styling</h3>
              <span>$85.00</span>
            </div>
            <div>
              <h3>Cat Bath</h3>
              <span>Custom Quote</span>
            </div>
          </section>
          <section id="gallery">
            <h2>Photo Gallery</h2>
            <figure>
              <img src="p1.jpg" alt="Bark &amp; Bliss Pet Grooming Salon photo 2">
            </figure>
          </section>
          <section id="contact">
            <h2>Location, Hours &amp; Policies</h2>
            <p>(512) 555-0144</p>
            <p>742 Evergreen Terrace, Austin, TX 78701</p>
            <p>Mon - Sat 8:00 AM - 6:00 PM</p>
            <p>Please provide 24 hours notice for any cancellation.</p>
          </section>
        </main>
        <footer>
          <p>&copy; Bark &amp; Bliss Pet Grooming Salon</p>
        </footer>
      </body>
    </html>"""

    facts = [
        {
            "category": "business_name",
            "claim_value": "Bark & Bliss Pet Grooming Salon",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "phone",
            "claim_value": "(512) 555-0144",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "address",
            "claim_value": "742 Evergreen Terrace, Austin, TX 78701",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "hours",
            "claim_value": "Mon - Sat 8:00 AM - 6:00 PM",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "cancellation_policy",
            "claim_value": "Please provide 24 hours notice for any cancellation.",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "services",
            "claim_value": "Full Dog Groom & Styling",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "pricing",
            "claim_value": "$85.00",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "services",
            "claim_value": "Cat Bath",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "animal_types",
            "claim_value": "dog, cat",
            "is_verified": True,
            "status": "verified",
        },
        {
            "category": "business_model",
            "claim_value": "salon",
            "is_verified": True,
            "status": "verified",
        },
    ]

    result = lint_html_claims(html_content=html, verified_facts=facts)
    assert result.passed is True
    assert len(result.violations) == 0


def test_pipeline_zero_pages_check_fails_early(tmp_path: Path) -> None:
    """Requirement 5: If collect returns pages_crawled == 0, stage collect fails immediately."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"

    # A project folder with empty pages.json
    proj = projects_dir / "zero-pages-salon"
    (proj / "research").mkdir(parents=True, exist_ok=True)
    (proj / "research" / "pages.json").write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        run_pipeline(
            url="https://empty-example.com",
            business_name="Zero Pages Salon",
            business_id="zero-pages-salon",
            projects_dir=projects_dir,
            db_path=db_path,
            resume=False,
        )
    assert "0 pages" in str(exc_info.value).lower() or "collection" in str(exc_info.value).lower()


class _NoContactHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        html = """<!DOCTYPE html>
        <html><head><title>No Contact Grooming Salon</title></head>
        <body>
          <h1>No Contact Grooming Salon</h1>
          <p>We provide dog and cat grooming.</p>
        </body></html>"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _ephemeral_no_contact_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoContactHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        server.shutdown()
        server.server_close()


def test_pipeline_mvf_check_stops_before_curate_and_generate(tmp_path: Path) -> None:
    """Requirement 5: MVF check runs right after extraction, stopping before curate/generate."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"

    with _ephemeral_no_contact_server() as target_url:
        res = run_pipeline(
            url=target_url,
            business_name="No Contact Grooming Salon",
            business_id="no-contact-salon",
            projects_dir=projects_dir,
            db_path=db_path,
            allow_private=True,
            resume=False,
            acknowledge_gaps=False,
        )

    proj = projects_dir / "no-contact-salon"
    assert res.status == "needs_review"
    assert any("MVF gate not met" in err for err in res.errors)
    # Curate, art_director, and generate should NOT have run!
    assert not (proj / "website" / "index.html").exists(), (
        "generate must not run on MVF-deficient project"
    )
    assert not (proj / "design" / "creative-brief.json").exists(), (
        "art_director must not run on MVF-deficient project"
    )


def test_pipeline_on_salon_fixture_passes_claims_lint(tmp_path: Path) -> None:
    """Requirements 1 & 4: Pipeline on salon fixture passes claims lint with qa/claims-lint.json."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test.db"

    with _ephemeral_grooming_salon_server() as target_url:
        res = run_pipeline(
            url=target_url,
            business_name="Bark & Bliss Pet Grooming Salon",
            business_id="bark-bliss-salon",
            projects_dir=projects_dir,
            db_path=db_path,
            allow_private=True,
            resume=False,
            qa_runner=_deterministic_qa_runner,
        )

        assert res.status == "awaiting_approval"
        proj = projects_dir / "bark-bliss-salon"

        # qa/claims-lint.json must exist and report passed: true
        lint_file = proj / "qa" / "claims-lint.json"
        assert lint_file.is_file(), "qa/claims-lint.json must be written"

        lint_data = json.loads(lint_file.read_text(encoding="utf-8"))
        assert lint_data["passed"] is True, f"Claims lint failed: {lint_data.get('violations')}"
        assert lint_data["violations_count"] == 0

        # Standalone HTML must be free of all evidence strings
        standalone = (proj / "release-candidate" / "standalone.html").read_text(encoding="utf-8")
        for ev in EVIDENCE_STRINGS:
            assert ev.lower() not in standalone.lower(), (
                f"Found unverified string in standalone.html: '{ev}'"
            )
