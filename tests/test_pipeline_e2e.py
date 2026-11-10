"""End-to-end integration tests for app/generator.py and app/pipeline.py."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from app.db import connect_db, get_project
from app.generator import generate_website
from app.pipeline import main as pipeline_cli_main
from app.pipeline import run_pipeline
from app.qa_engine import BREAKPOINTS, QADefectReport, ViewportQAResult
from PIL import Image, ImageDraw


def _build_jpeg_bytes(
    *,
    width: int = 1280,
    height: int = 800,
    bg_color: tuple[int, int, int] = (27, 54, 93),
    box_color: tuple[int, int, int] = (217, 138, 43),
) -> bytes:
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = width // 4, height // 5, 3 * width // 4, 3 * height // 4
    draw.rectangle((x0, y0, x1, y1), fill=box_color, outline=(255, 255, 255), width=4)
    for x in range(x0, x1, 14):
        draw.line([(x, y0), (x, y1)], fill=(20, 20, 20), width=2)
    for y in range(y0, y1, 14):
        draw.line([(x0, y), (x1, y)], fill=(245, 245, 245), width=2)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


SAMPLE_SALON_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Bark &amp; Bliss Pet Grooming Salon</title>
  <meta name="description" content="Trusted dog and cat grooming salon in Austin, TX.">
</head>
<body>
  <header>
    <h1>Bark &amp; Bliss Pet Grooming Salon</h1>
    <img src="/media/logo.jpg" alt="Bark and Bliss Logo">
  </header>
  <main>
    <section>
      <p>Phone: (512) 555-0144</p>
      <p>Address: 742 Evergreen Terrace, Austin, TX 78701</p>
      <p>Hours: Mon - Sat 8:00 AM - 6:00 PM</p>
    </section>
    <section>
      <h2>Grooming Services &amp; Pricing</h2>
      <p>Full Dog Groom &amp; Styling - $85.00</p>
      <p>Luxury Bath &amp; Deshedding - $55.00</p>
      <p>Feline Lion Cut &amp; Cat Bath - $95.00</p>
      <p>Puppy Intro Grooming Package - $45.00</p>
    </section>
    <section>
      <h2>Gallery</h2>
      <img src="/media/hero_golden_retriever.jpg" alt="Hero Golden Retriever Portrait">
      <img src="/media/groom_poodle_1.jpg" alt="Standard Poodle Groom">
      <img src="/media/groom_terrier_2.jpg" alt="Terrier Scissor Trim">
      <img src="/media/groom_cat_3.jpg" alt="Persian Cat Brush Out">
      <img src="/media/groom_spaniel_4.jpg" alt="Cocker Spaniel Bath">
      <img src="/media/salon_interior.jpg" alt="Clean Grooming Salon Facility">
      <img src="/media/before_matted_dog.jpg" alt="Before Grooming Session">
      <img src="/media/after_fluffy_dog.jpg" alt="After Grooming Session">
    </section>
    <section>
      <p>Cancellation Policy: Please provide 24 hours notice for any cancellation.</p>
    </section>
  </main>
</body>
</html>
"""


@contextmanager
def _ephemeral_grooming_salon_server() -> Iterator[str]:
    """Spin up a local ephemeral HTTP server serving sample grooming salon HTML and mock JPEGs."""
    images: dict[str, bytes] = {
        "/media/logo.jpg": _build_jpeg_bytes(
            width=840, height=600, bg_color=(27, 54, 93), box_color=(217, 138, 43)
        ),
        "/media/hero_golden_retriever.jpg": _build_jpeg_bytes(
            width=1440, height=900, bg_color=(35, 70, 110), box_color=(230, 170, 80)
        ),
        "/media/groom_poodle_1.jpg": _build_jpeg_bytes(
            width=1200, height=800, bg_color=(45, 85, 115), box_color=(210, 160, 95)
        ),
        "/media/groom_terrier_2.jpg": _build_jpeg_bytes(
            width=1200, height=800, bg_color=(55, 95, 120), box_color=(200, 150, 90)
        ),
        "/media/groom_cat_3.jpg": _build_jpeg_bytes(
            width=1200, height=800, bg_color=(65, 105, 125), box_color=(190, 140, 85)
        ),
        "/media/groom_spaniel_4.jpg": _build_jpeg_bytes(
            width=1200, height=800, bg_color=(75, 115, 130), box_color=(180, 130, 80)
        ),
        "/media/salon_interior.jpg": _build_jpeg_bytes(
            width=1280, height=800, bg_color=(80, 105, 90), box_color=(220, 210, 190)
        ),
        "/media/before_matted_dog.jpg": _build_jpeg_bytes(
            width=1100, height=800, bg_color=(95, 85, 75), box_color=(170, 155, 140)
        ),
        "/media/after_fluffy_dog.jpg": _build_jpeg_bytes(
            width=1100, height=800, bg_color=(40, 95, 115), box_color=(240, 225, 200)
        ),
    }

    class _SalonHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                body = SAMPLE_SALON_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/robots.txt":
                robots = b"User-agent: *\nAllow: /\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(robots)))
                self.end_headers()
                self.wfile.write(robots)
            elif self.path in images:
                payload = images[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SalonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        server.shutdown()
        server.server_close()


def _deterministic_qa_runner(project_folder: str | Path, **_kwargs: Any) -> QADefectReport:
    """Fast, deterministic 5-viewport QA runner validating generated HTML/CSS/JS and images."""
    project_root = Path(project_folder).resolve()
    index_file = project_root / "website" / "index.html"
    assert index_file.is_file()
    screenshots_dir = project_root / "qa" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    viewports: list[ViewportQAResult] = []
    for label, width in BREAKPOINTS:
        shot_path = screenshots_dir / f"{width}px.png"
        shot_path.write_bytes(_build_jpeg_bytes(width=320, height=200))
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

    report = QADefectReport(
        project_id=project_root.name,
        target_url=f"http://localhost/{project_root.name}/website/index.html",
        passed=True,
        viewports=viewports,
    )
    report_path = project_root / "qa" / "qa-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def test_pipeline_e2e_creates_website_ghl_delivery_and_completes_all_db_jobs(
    tmp_path: Path,
) -> None:
    """Run full pipeline against ephemeral HTTP server and verify HTML, GHL, and SQLite jobs."""
    projects_dir = tmp_path / "projects"
    db_path = tmp_path / "editvista.sqlite3"
    test_id = "bark-and-bliss-e2e"

    with _ephemeral_grooming_salon_server() as target_url:
        exit_code = pipeline_cli_main(
            [
                "--url",
                target_url,
                "--name",
                "Bark & Bliss Pet Grooming Salon",
                "--business-id",
                test_id,
                "--projects-dir",
                str(projects_dir),
                "--db-path",
                str(db_path),
                "--allow-private",
            ],
            qa_runner=_deterministic_qa_runner,
        )
        assert exit_code == 0

    project_root = projects_dir / test_id
    index_html_path = project_root / "website" / "index.html"
    css_path = project_root / "website" / "css" / "styles.css"
    js_path = project_root / "website" / "js" / "main.js"
    rc_standalone_path = project_root / "release-candidate" / "standalone.html"
    rc_approved_path = project_root / "release-candidate" / "approved-homepage.html"
    rc_ghl_path = project_root / "release-candidate" / "ghl-ready.html"

    # 1. Pipeline NEVER writes delivery/ automatically; writes only to release-candidate/
    assert not (project_root / "delivery").exists()
    assert index_html_path.is_file()
    assert css_path.is_file()
    assert js_path.is_file()
    assert rc_standalone_path.is_file()
    assert rc_approved_path.is_file()

    html_text = index_html_path.read_text(encoding="utf-8")
    css_text = css_path.read_text(encoding="utf-8")
    js_text = js_path.read_text(encoding="utf-8")

    # Semantic HTML5 structure checks
    for tag in ("<header", "<nav", "<main", "<section", "<footer"):
        assert tag in html_text

    # Zero external heavy dependencies (no jQuery, Bootstrap, Tailwind CDN)
    lower_html = html_text.lower()
    for banned in ("jquery", "bootstrap", "tailwindcss", "cdn.jsdelivr", "unpkg.com"):
        assert banned not in lower_html

    # Verified business facts injected
    assert "(512) 555-0144" in html_text
    assert "742 Evergreen Terrace, Austin, TX 78701" in html_text
    assert "Mon - Sat 8:00 AM - 6:00 PM" in html_text
    assert "$85.00" in html_text

    # Responsive CSS custom properties and clamp() typography
    assert "--ev-primary" in css_text
    assert "clamp(" in css_text
    assert "grid-template-columns" in css_text

    # Accessible mobile drawer and before/after slider in JS
    assert "aria-expanded" in js_text

    # 2. Assert release-candidate/ghl-ready.html contains scoped #ghl-ev-engine-root-... wrapper
    assert rc_ghl_path.is_file()
    ghl_text = rc_ghl_path.read_text(encoding="utf-8")
    expected_root_id = f"ghl-ev-engine-root-{test_id}"
    assert f'<div id="{expected_root_id}">' in ghl_text
    assert f"#{expected_root_id}" in ghl_text
    assert "<!DOCTYPE" not in ghl_text
    assert "<html" not in ghl_text.lower()

    # 3. Assert all database job states are completed and project is awaiting_approval
    conn = connect_db(db_path)
    try:
        proj = get_project(conn, test_id)
        assert proj is not None
        assert proj.status == "awaiting_approval"

        job_rows = conn.execute(
            "SELECT module_name, status, error_message FROM job_runs WHERE project_id = ?",
            (test_id,),
        ).fetchall()
        assert len(job_rows) >= 7
        for row in job_rows:
            assert str(row["status"]) == "completed", (
                f"Module {row['module_name']} had status {row['status']}: {row['error_message']}"
            )
    finally:
        conn.close()


def test_pipeline_blocks_approval_and_records_failed_qa_when_browser_qa_raises(
    tmp_path: Path,
) -> None:
    """A browser QA failure must block approval and leave a failed QA stage, not a stale run."""
    projects_dir = tmp_path / "projects"
    db_path = tmp_path / "editvista.sqlite3"
    test_id = "bark-and-bliss-qa-failure"

    def _failed_qa(_project_folder: str | Path, **_kwargs: Any) -> QADefectReport:
        raise RuntimeError("Playwright browser launch failed")

    with _ephemeral_grooming_salon_server() as target_url:
        with pytest.raises(RuntimeError, match="Playwright browser launch failed"):
            run_pipeline(
                url=target_url,
                business_name="Bark & Bliss Pet Grooming Salon",
                business_id=test_id,
                projects_dir=projects_dir,
                db_path=db_path,
                allow_private=True,
                pause_for_approval=True,
                qa_runner=_failed_qa,
            )

    project_root = projects_dir / test_id
    assert not (project_root / "delivery").exists()
    conn = connect_db(db_path)
    try:
        project = get_project(conn, test_id)
        assert project is not None
        assert project.status == "failed"
        qa_run = conn.execute(
            "SELECT status, error_message FROM job_runs "
            "WHERE project_id = ? AND module_name = 'qa'",
            (test_id,),
        ).fetchone()
        assert qa_run is not None
        assert qa_run["status"] == "failed"
        assert "Playwright browser launch failed" in qa_run["error_message"]
    finally:
        conn.close()


def test_generator_renders_inquiry_cta_when_pricing_needs_review(tmp_path: Path) -> None:
    """If pricing is needs_review (is_verified=False), render an inquiry CTA instead of prices."""
    project_root = tmp_path / "projects" / "unverified-pricing-salon"
    (project_root / "research").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)

    facts_payload = {
        "project_id": "unverified-pricing-salon",
        "extracted_at": "2026-11-05T12:00:00Z",
        "prompt_injection_warnings": [],
        "facts": [
            {
                "fact_id": "fact-name",
                "project_id": "unverified-pricing-salon",
                "category": "business_name",
                "claim_key": "business_name",
                "claim_value": "Gentle Paws Grooming Studio",
                "source_url": "https://gentlepaws.example/",
                "source_text_snippet": "Gentle Paws Grooming Studio",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
            {
                "fact_id": "fact-unverified-price",
                "project_id": "unverified-pricing-salon",
                "category": "pricing",
                "claim_key": "price_full_groom",
                "claim_value": "$999.00",
                "source_url": "https://gentlepaws.example/",
                "source_text_snippet": "Unmapped claim",
                "is_verified": False,
                "status": "needs_review",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
        ],
    }
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps(facts_payload),
        encoding="utf-8",
    )

    result = generate_website(project_root)
    html_out = result.index_html_path.read_text(encoding="utf-8")

    # Unverified price ($999.00) must NOT appear in rendered copy; inquiry/booking CTA must appear
    assert "$999.00" not in html_out
    assert "Request Custom Quote" in html_out or "Inquire for Pricing" in html_out
    assert result.pricing_needs_review is True
    assert run_pipeline is not None
