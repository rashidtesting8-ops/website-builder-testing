"""TDD tests: TypeScript progress frontend for non-technical operators."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_frontend_sources_exist_and_are_compiled() -> None:
    source = REPO / "frontend/src/progress.ts"
    compiled = REPO / "frontend/public/progress.js"
    stylesheet = REPO / "frontend/public/progress.css"
    assert source.is_file(), "TypeScript source is missing"
    assert compiled.is_file(), "Compiled progress bundle is missing"
    assert stylesheet.is_file(), "Progress stylesheet is missing"
    compiled_text = compiled.read_text(encoding="utf-8")
    assert "interface " not in compiled_text, "Compiled bundle contains TypeScript-only syntax"
    assert "progress" in compiled_text
    assert "Copy GHL code" in compiled_text, "Progress bundle must expose the copy-code action"


def test_dashboard_links_to_plain_language_progress_page() -> None:
    import tempfile
    from pathlib import Path as P

    import requests

    from test_operator_cockpit import _ephemeral_cockpit_server, _scaffold_cockpit_project

    with tempfile.TemporaryDirectory() as td:
        projects = P(td) / "projects"
        projects.mkdir()
        _scaffold_cockpit_project(projects, "progress-demo")
        with _ephemeral_cockpit_server(projects) as base:
            page = requests.get(f"{base}/dashboard", timeout=3.0)
            assert page.status_code == 200
            assert "/progress/progress-demo" in page.text
            assert "Live status" in page.text or "Plain-English status" in page.text


def test_progress_page_serves_typescript_bundle_and_styles(
    tmp_path: Path,
) -> None:
    import requests

    from test_operator_cockpit import _ephemeral_cockpit_server, _scaffold_cockpit_project

    projects = tmp_path / "projects"
    projects.mkdir()
    _scaffold_cockpit_project(projects, "progress-demo")
    with _ephemeral_cockpit_server(projects) as base:
        page = requests.get(f"{base}/progress/progress-demo", timeout=3.0)
        assert page.status_code == 200
        assert "/static/progress.js" in page.text
        assert "/static/progress.css" in page.text

        js = requests.get(f"{base}/static/progress.js", timeout=3.0)
        assert js.status_code == 200
        assert "javascript" in js.headers.get("content-type", "")

        css = requests.get(f"{base}/static/progress.css", timeout=3.0)
        assert css.status_code == 200
        assert "css" in css.headers.get("content-type", "")


def test_progress_api_returns_friendly_payload(tmp_path: Path) -> None:
    import requests

    from test_operator_cockpit import _ephemeral_cockpit_server, _scaffold_cockpit_project

    projects = tmp_path / "projects"
    projects.mkdir()
    _scaffold_cockpit_project(projects, "progress-demo")
    with _ephemeral_cockpit_server(projects) as base:
        resp = requests.get(
            f"{base}/api/projects/progress-demo/progress", timeout=3.0
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["project_id"] == "progress-demo"
        assert payload["friendly_message"]
        assert payload["current_stage"]["plain_english"]
        assert payload["progress_percent"] >= 0
        assert payload["action_required"]
        assert payload["review_url"].startswith("/review/")
        assert payload["ghl_export_url"].startswith("/api/projects/")
