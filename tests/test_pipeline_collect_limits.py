"""Regression tests for STEP 1 pipeline collection limits (25 pages / 80 media)."""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from app.pipeline import run_pipeline
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
LILYS_SNAPSHOT_DIR = REPO_ROOT / "projects" / "lilys-club-co-uk" / "research"


def _unique_png_for_path(path: str) -> bytes:
    digest = hashlib.sha256(path.encode("utf-8")).digest()
    color = (digest[0], digest[1], digest[2])
    img = Image.new("RGB", (64, 64), color=color)
    # Vary one pixel per hash byte pair so identical top colors never collide.
    img.putpixel((digest[3] % 64, digest[4] % 64), (digest[5], digest[6], digest[7]))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@contextmanager
def _serve_html_and_media(
    html_routes: dict[str, str],
) -> Iterator[str]:
    png_cache: dict[str, bytes] = {}
    lock = threading.Lock()
    server_holder: list[ThreadingHTTPServer] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = unquote(parsed.path) or "/"
            if route == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route in html_routes:
                port = server_holder[0].server_address[1]
                base = f"http://127.0.0.1:{port}"
                html = html_routes[route]
                for origin in (
                    "https://www.lilys-club.co.uk",
                    "http://www.lilys-club.co.uk",
                    "https://lilys-club.co.uk",
                    "http://lilys-club.co.uk",
                ):
                    html = html.replace(origin, base)
                html = re.sub(
                    r"https?://(?:www\.)?facebook\.com/tr\?[^\s\"'<>]*",
                    "",
                    html,
                )
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            ext = Path(route.lower()).suffix
            if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico"} or "/wp-content/" in route:
                with lock:
                    payload = png_cache.get(route)
                    if payload is None:
                        payload = _unique_png_for_path(self.path)
                        png_cache[route] = payload
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_holder.append(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_pipeline_collect_respects_25_page_and_80_media_budget(tmp_path: Path) -> None:
    """Pipeline collect stage must crawl beyond 10 pages / 40 media up to 25 / 80."""
    # Ensure /prices, /testimonials, and /the-team sort after 10+ filler pages
    # without PAGE_HINTS priority boosting them ahead of early alphabetical URLs,
    # plus test both hinted and non-hinted ordering (> 10 pages total).
    html_routes: dict[str, str] = {}
    nav_links = [
        *[f'<a href="/about-branch-{idx:02d}">About {idx}</a>' for idx in range(1, 10)],
        '<a href="/prices">Prices</a>',
        '<a href="/testimonials">Testimonials</a>',
        '<a href="/the-team">The Team</a>',
        *[f'<a href="/services-extra-{idx:02d}">Service {idx}</a>' for idx in range(1, 5)],
    ]
    home_imgs = "".join(
        f'<img src="/media/grooming-{idx:02d}.png" alt="Groom {idx}">'
        for idx in range(1, 86)
    )
    html_routes["/"] = (
        "<html><head><title>Lily's Club Dog Grooming</title></head><body>"
        + "".join(nav_links)
        + home_imgs
        + "</body></html>"
    )
    for idx in range(1, 10):
        html_routes[f"/about-branch-{idx:02d}"] = (
            f"<html><head><title>About {idx}</title></head><body><p>Branch {idx}</p></body></html>"
        )
    for route, title in (
        ("/prices", "Prices"),
        ("/testimonials", "Testimonials"),
        ("/the-team", "The Team"),
    ):
        html_routes[route] = (
            f"<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>"
        )
    for idx in range(1, 5):
        html_routes[f"/services-extra-{idx:02d}"] = (
            f"<html><head><title>Extra {idx}</title></head><body><p>Service {idx}</p></body></html>"
        )

    with _serve_html_and_media(html_routes) as base_url:
        result = run_pipeline(
            url=f"{base_url}/",
            business_name="Lilys Limit Test",
            business_id="lilys-limit-test",
            projects_dir=tmp_path / "projects",
            db_path=tmp_path / "editvista.sqlite3",
            allow_private=True,
            stop_after="collect",
        )

    pages = json.loads(
        (result.project_folder / "research" / "pages.json").read_text(encoding="utf-8")
    )
    crawled_paths = {urlparse(page["url"]).path or "/" for page in pages}
    assert "/prices" in crawled_paths
    assert "/testimonials" in crawled_paths
    assert "/the-team" in crawled_paths

    saved_assets = [
        p for p in (result.project_folder / "assets" / "website").iterdir() if p.is_file()
    ]
    assert len(saved_assets) >= 70


@pytest.mark.skipif(
    not (LILYS_SNAPSHOT_DIR / "pages.json").is_file(),
    reason="Saved lilys-club snapshots are Git-ignored and unavailable in clean checkouts.",
)
def test_lilys_club_snapshots_crawl_prices_testimonials_team_and_70_assets(
    tmp_path: Path,
) -> None:
    """Replay saved lilys-club HTML snapshots through pipeline collect stage."""
    saved_pages = json.loads((LILYS_SNAPSHOT_DIR / "pages.json").read_text(encoding="utf-8"))
    html_routes: dict[str, str] = {}
    for page in saved_pages:
        snap_file = LILYS_SNAPSHOT_DIR / "snapshots" / f"{page['sha256']}.html"
        if not snap_file.is_file():
            continue
        path = urlparse(page["url"]).path or "/"
        html_routes[path] = snap_file.read_text(encoding="utf-8", errors="ignore")

    with _serve_html_and_media(html_routes) as base_url:
        result = run_pipeline(
            url=f"{base_url}/",
            business_name="Lily's Club Dog Grooming",
            business_id="lilys-club-snapshot-test",
            projects_dir=tmp_path / "projects",
            db_path=tmp_path / "editvista.sqlite3",
            allow_private=True,
            stop_after="collect",
        )

    pages = json.loads(
        (result.project_folder / "research" / "pages.json").read_text(encoding="utf-8")
    )
    crawled_paths = {urlparse(page["url"]).path or "/" for page in pages}
    assert "/prices" in crawled_paths
    assert "/testimonials" in crawled_paths
    assert "/the-team" in crawled_paths

    saved_assets = [
        p for p in (result.project_folder / "assets" / "website").iterdir() if p.is_file()
    ]
    assert len(saved_assets) >= 70
