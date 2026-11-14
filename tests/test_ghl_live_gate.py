from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from bs4 import Tag
from PIL import Image
from scripts.ghl_live_gate import WIDTHS, main, run_gate

PROJECT_ID = "fixture"
ROOT_ID = f"ghl-ev-engine-root-{PROJECT_ID}"


def _snippet(asset_url: str) -> str:
    return f"""<div id="{ROOT_ID}">
<style>
#{ROOT_ID} .ev-ghl-scope {{ color: #123456; font-family: Arial, sans-serif; }}
#{ROOT_ID} h1 {{ color: #123456; font-size: 32px; font-weight: 700; margin: 0; }}
#{ROOT_ID} h2 {{ color: #123456; font-size: 24px; }}
#{ROOT_ID} p {{ color: #123456; font-size: 16px; }}
#{ROOT_ID} a {{ color: #123456; font-size: 16px; }}
#{ROOT_ID} button {{ color: #123456; font-size: 16px; }}
#{ROOT_ID} img {{ width: 20px; height: 20px; }}
#{ROOT_ID} [data-ev-nav-drawer][data-collapsed="true"] {{ display: none; }}
</style>
<div class="ev-ghl-scope">
<header><nav aria-label="Primary">
<button type="button" aria-expanded="false" data-ev-nav-toggle="true">Menu</button>
<ul data-ev-nav-drawer="true" data-collapsed="true"><li><a href="#services">Services</a></li></ul>
</nav></header>
<main><section id="hero"><h1>Fictional grooming studio</h1>
<h2>Welcome</h2><p>Fixture content.</p>
<a href="#services">Explore</a><img src="{asset_url}" alt="Fixture image">
<button type="button">Action</button></section>
<input type="range" min="0" max="100" value="50" data-ev-ba-range="true">
<div data-ev-ba-after="true" data-split="50"></div>
<footer>Fixture footer</footer>
</main></div>
<script>
(function () {{
  var root = document.getElementById('{ROOT_ID}');
  var scope = root && root.querySelector('.ev-ghl-scope');
  var toggle = scope && scope.querySelector('[data-ev-nav-toggle]');
  var drawer = scope && scope.querySelector('[data-ev-nav-drawer]');
  if (toggle && drawer) {{
    toggle.addEventListener('click', function () {{
      var expanded = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!expanded));
      drawer.setAttribute('data-collapsed', String(expanded));
    }});
  }}
  var range = scope && scope.querySelector('[data-ev-ba-range]');
  var after = scope && scope.querySelector('[data-ev-ba-after]');
  if (range && after) {{
    range.addEventListener('input', function () {{
      after.setAttribute('data-split', range.value);
    }});
  }}
}})();
</script>
</div>"""


def _approved_html() -> str:
    return """<!doctype html><html><head><style>
.ev-ghl-scope { color: #123456; font-family: Arial, sans-serif; }
.ev-ghl-scope h1 { color: #123456; font-size: 32px; font-weight: 700; margin: 0; }
.ev-ghl-scope h2 { color: #123456; font-size: 24px; }
.ev-ghl-scope p, .ev-ghl-scope a, .ev-ghl-scope button { color: #123456; font-size: 16px; }
.ev-ghl-scope img { width: 20px; height: 20px; }
</style></head><body><div class="ev-ghl-scope">
<header><nav><button>Menu</button></nav></header>
<main><section><h1>Fictional grooming studio</h1><h2>Welcome</h2>
<p>Fixture content.</p><a>Explore</a>
<img src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=">
<button>Action</button></section><footer>Fixture footer</footer></main>
</div></body></html>"""


def _write_project(project_dir: Path, snippet: str) -> None:
    delivery = project_dir / "delivery"
    delivery.mkdir(parents=True)
    snippet_path = delivery / "ghl-ready.html"
    snippet_path.write_text(snippet, encoding="utf-8")
    (delivery / "approved-homepage.html").write_text(_approved_html(), encoding="utf-8")
    digest = hashlib.sha256(snippet_path.read_bytes()).hexdigest()
    (delivery / "release-manifest.json").write_text(
        json.dumps(
            {
                "project_id": PROJECT_ID,
                "version_hash": "release-fixture-hash",
                "checksum_tree": {"delivery/ghl-ready.html": digest},
            }
        ),
        encoding="utf-8",
    )


def _image_bytes() -> bytes:
    image = Image.new("RGB", (2, 2), color=(18, 52, 86))
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


@contextmanager
def _simulated_host_page(snippet: str) -> Generator[str, None, None]:
    image = _image_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                body = f"""<!doctype html><html><head><style>
body {{ font-family: Arial, sans-serif; color: #222; }}
#host-header, #host-footer {{ color: #222; font-size: 14px; }}
</style></head><body>
<header id="host-header">Host header</header>
<div class="custom-html-element">{snippet}</div>
<footer id="host-footer">Host footer</footer>
</body></html>""".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/assets/ok.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(image)))
                self.end_headers()
                self.wfile.write(image)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            _ = format, args
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_gate(url: str, project_dir: Path, output_dir: Path) -> dict[str, Any]:
    return run_gate(
        url,
        project_dir,
        out=output_dir,
        _allow_local_http=True,
    )


def test_clean_simulated_host_passes_and_writes_all_viewport_screenshots(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / PROJECT_ID
    _write_project(project_dir, _snippet("/assets/ok.png"))

    with _simulated_host_page(_snippet("/assets/ok.png")) as url:
        report = _run_gate(url, project_dir, tmp_path / "clean-output")

    assert report["overall_pass"] is True
    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks["assets"]["passed"] is True
    assert checks["interactivity"]["passed"] is True
    assert checks["script_style_survival"]["passed"] is True
    assert set(checks["computed_styles"]["details"]["by_viewport"]) == {
        str(width) for width in WIDTHS
    }
    assert set(checks["host_elements_unchanged"]["details"]["by_viewport"]) == {
        str(width) for width in WIDTHS
    }
    assert report["release_hash"] == "release-fixture-hash"
    screenshots = report["evidence"]["screenshots"]
    assert len(screenshots) == len(WIDTHS)
    assert all(Path(path).is_file() for path in screenshots.values())
    assert (tmp_path / "clean-output" / "ghl-live-report.json").is_file()


def test_hostile_host_stylesheet_fails_computed_style_isolation(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / PROJECT_ID
    _write_project(project_dir, _snippet("/assets/ok.png"))
    hostile = """<style>
#ghl-ev-engine-root-fixture h1 { color: rgb(255, 0, 0) !important; font-size: 60px !important; }
</style>"""

    with _simulated_host_page(
        hostile + _snippet("/assets/ok.png")
    ) as url:
        report = _run_gate(url, project_dir, tmp_path / "hostile-output")

    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks["computed_styles"]["passed"] is False
    assert report["overall_pass"] is False


def test_mobile_host_style_bleed_fails_outside_element_isolation(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / PROJECT_ID
    _write_project(project_dir, _snippet("/assets/ok.png"))
    hostile = """<style>
@media (max-width: 800px) {
  #host-header { outline: 5px solid rgb(255, 0, 0) !important; }
}
</style>"""

    snippet = _snippet("/assets/ok.png").replace(
        '<div class="ev-ghl-scope">',
        hostile + '<div class="ev-ghl-scope">',
    )

    with _simulated_host_page(snippet) as url:
        report = _run_gate(url, project_dir, tmp_path / "host-bleed-output")

    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks["host_elements_unchanged"]["passed"] is False
    assert checks["host_elements_unchanged"]["details"]["by_viewport"]["390"]
    assert report["overall_pass"] is False


def test_stripped_script_fails_script_survival_and_interactivity(
    tmp_path: Path,
) -> None:
    from bs4 import BeautifulSoup

    project_dir = tmp_path / PROJECT_ID
    full_snippet = _snippet("/assets/ok.png")
    _write_project(project_dir, full_snippet)
    soup = BeautifulSoup(full_snippet, "html.parser")
    script = soup.find("script")
    assert isinstance(script, Tag)
    script.decompose()

    with _simulated_host_page(str(soup)) as url:
        report = _run_gate(url, project_dir, tmp_path / "stripped-output")

    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks["script_style_survival"]["passed"] is False
    assert checks["interactivity"]["passed"] is False
    assert report["overall_pass"] is False


def test_404_asset_fails_asset_integrity_check(tmp_path: Path) -> None:
    project_dir = tmp_path / PROJECT_ID
    missing_asset = "/assets/missing.png"
    _write_project(project_dir, _snippet(missing_asset))

    with _simulated_host_page(_snippet(missing_asset)) as url:
        report = _run_gate(url, project_dir, tmp_path / "missing-output")

    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks["assets"]["passed"] is False
    assert any(
        item["status"] == 404 for item in checks["assets"]["details"]["responses"]
    )
    assert report["overall_pass"] is False


def test_manifest_snippet_hash_mismatch_fails_release_integrity(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / PROJECT_ID
    _write_project(project_dir, _snippet("/assets/ok.png"))
    manifest_path = project_dir / "delivery" / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checksum_tree"]["delivery/ghl-ready.html"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with _simulated_host_page(
        _snippet("/assets/ok.png")
    ) as url:
        report = _run_gate(url, project_dir, tmp_path / "hash-output")

    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks["snippet_hash"]["passed"] is False
    assert report["overall_pass"] is False


def test_cli_rejects_non_https_published_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["http://127.0.0.1/", str(tmp_path)])

    assert exit_code == 2
    assert "published_url must use HTTPS" in capsys.readouterr().err
