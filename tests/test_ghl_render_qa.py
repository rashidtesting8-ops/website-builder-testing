"""TDD tests: render the GHL snippet in a hostile host page across 5 viewports."""

from __future__ import annotations

import json
from pathlib import Path

from app.qa_engine import run_ghl_render_qa

from test_qa_engine import _FakePlaywrightManager


def _project(tmp_path: Path, snippet: str) -> Path:
    project = tmp_path / "ghl-render"
    (project / "delivery").mkdir(parents=True)
    (project / "qa").mkdir(parents=True)
    (project / "delivery/ghl-ready.html").write_text(snippet, encoding="utf-8")
    return project


def test_ghl_render_qa_passes_with_public_asset_urls(tmp_path: Path) -> None:
    snippet = (
        '<div id="ghl-ev-engine-root-ghl-render">'
        '<img src="https://cdn.example.com/hero.jpg" alt="Dog">'
        "</div>"
        "<style>#ghl-ev-engine-root-ghl-render img{max-width:100%}</style>"
        "<script>(function(){var root=document.getElementById("
        "'ghl-ev-engine-root-ghl-render');})();</script>"
    )
    project = _project(tmp_path, snippet)
    events: list[str] = []

    report = run_ghl_render_qa(
        project, playwright_factory=lambda: _FakePlaywrightManager(
            simulate_defects=False, events=events
        )
    )

    assert report.passed is True
    assert report.static_defects == []
    assert report.simulation["is_valid"] is True
    assert len(report.viewports) == 5
    host = (project / "qa/ghl-host.html").read_text(encoding="utf-8")
    assert snippet in host
    assert "ghl-host-bar" in host
    assert (project / "qa/screenshots/ghl-1440px.png").is_file()
    saved = json.loads((project / "qa/ghl-qa-report.json").read_text(encoding="utf-8"))
    assert saved["passed"] is True


def test_ghl_render_qa_cli_flag_runs_both_reports(
    monkeypatch: object, tmp_path: Path
) -> None:
    import app.qa_engine as qa

    class _Report:
        def model_dump_json(self, indent: int = 2) -> str:
            return "{}"

    calls: list[str] = []

    def fake_main_run(project: object, html_rel_path: object = None) -> _Report:
        calls.append("standalone")
        return _Report()

    def fake_ghl_run(project: object, **kwargs: object) -> _Report:
        calls.append("ghl")
        return _Report()

    monkeypatch.setattr(qa, "run_qa_engine", fake_main_run)  # type: ignore[attr-defined]
    monkeypatch.setattr(qa, "run_ghl_render_qa", fake_ghl_run)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "sys.argv", ["qa_engine", "--project", str(tmp_path), "--ghl-render"]
    )
    qa.main()
    assert calls == ["standalone", "ghl"]


def test_ghl_render_qa_fails_on_relative_or_localhost_assets(tmp_path: Path) -> None:
    snippet = (
        '<div id="ghl-ev-engine-root-ghl-render">'
        '<img src="../assets/optimized/hero.jpg" alt="Dog">'
        "</div>"
    )
    project = _project(tmp_path, snippet)
    events: list[str] = []

    report = run_ghl_render_qa(
        project, playwright_factory=lambda: _FakePlaywrightManager(
            simulate_defects=False, events=events
        )
    )

    assert report.passed is False
    assert any("../assets/" in defect for defect in report.static_defects)
