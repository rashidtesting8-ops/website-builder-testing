"""Tests for the 5-viewport Playwright QA runner in app/qa_engine.py."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from app.qa_engine import (
    BREAKPOINTS,
    QADefectReport,
    compute_contrast_ratio,
    run_qa_engine,
)
from PIL import Image


def _make_png_bytes() -> bytes:
    im = Image.new("RGB", (60, 40), color=(30, 80, 140))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, url: str, status: int) -> None:
        self.url = url
        self.status = status


class _FakeConsoleMessage:
    def __init__(self, msg_type: str, text: str) -> None:
        self.type = msg_type
        self.text = text


class _FakePage:
    def __init__(
        self,
        width: int,
        *,
        simulate_defects: bool,
        events: list[str],
    ) -> None:
        self.width = width
        self._simulate_defects = simulate_defects
        self._events = events
        self._listeners: dict[str, list[Any]] = {}
        self.closed = False
        self.visited_url: str | None = None

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def goto(self, url: str, **kwargs: Any) -> None:
        _ = kwargs
        self.visited_url = url
        assert url.startswith("http://localhost:") or url.startswith("http://127.0.0.1:")
        if self._simulate_defects:
            for h in self._listeners.get("response", []):
                h(_FakeResponse(f"{url}/missing-hero.jpg", 404))
            for h in self._listeners.get("console", []):
                h(
                    _FakeConsoleMessage(
                        "error",
                        "Uncaught ReferenceError: bookingWidget is not defined",
                    )
                )
            for h in self._listeners.get("pageerror", []):
                h(RuntimeError("TypeError: Cannot read properties of null"))

    def wait_for_timeout(self, ms: int) -> None:
        _ = ms

    def evaluate(self, script: str) -> Any:
        if "document.images" in script and "loading" in script:
            self._events.append(f"images_prepared:{self.width}")
            return None
        if "document.documentElement.scrollWidth > window.innerWidth" in script:
            if self._simulate_defects:
                return {
                    "overflow": self.width <= 390,
                    "scrollWidth": self.width + (120 if self.width <= 390 else 0),
                    "innerWidth": self.width,
                    "brokenImages": ["/assets/website/broken-dog.jpg"],
                    "contrastFailures": [
                        {
                            "selector": "a.cta",
                            "text": "Book Now",
                            "ratio": 1.85,
                            "required": 4.5,
                        }
                    ],
                }
            return {
                "overflow": False,
                "scrollWidth": self.width,
                "innerWidth": self.width,
                "brokenImages": [],
                "contrastFailures": [],
            }
        return {}

    def screenshot(self, *, path: str, full_page: bool = True, **kwargs: Any) -> bytes:
        _ = (full_page, kwargs)
        data = _make_png_bytes()
        Path(path).write_bytes(data)
        return data

    def close(self) -> None:
        self.closed = True
        self._events.append(f"page_closed:{self.width}")


class _FakeContext:
    def __init__(
        self,
        width: int,
        *,
        simulate_defects: bool,
        events: list[str],
    ) -> None:
        self.width = width
        self._simulate_defects = simulate_defects
        self._events = events
        self.closed = False
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage(
            self.width,
            simulate_defects=self._simulate_defects,
            events=self._events,
        )
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True
        self._events.append(f"context_closed:{self.width}")


class _FakeBrowser:
    def __init__(self, *, simulate_defects: bool, events: list[str]) -> None:
        self._simulate_defects = simulate_defects
        self._events = events
        self.closed = False
        self.contexts: list[_FakeContext] = []

    def new_context(self, **kwargs: Any) -> _FakeContext:
        viewport = kwargs.get("viewport") or {}
        width = int(viewport.get("width", 1440))
        ctx = _FakeContext(
            width,
            simulate_defects=self._simulate_defects,
            events=self._events,
        )
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed = True
        self._events.append("browser_closed")


class _FakePlaywrightManager:
    def __init__(self, *, simulate_defects: bool, events: list[str]) -> None:
        self.browser = _FakeBrowser(simulate_defects=simulate_defects, events=events)

    def __enter__(self) -> Any:
        class _P:
            pass

        p = _P()
        chromium = _P()
        chromium.launch = lambda **kw: self.browser  # type: ignore[attr-defined]
        p.chromium = chromium  # type: ignore[attr-defined]
        return p

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        pass


def test_wcag_contrast_ratio_calculation() -> None:
    # Black on white is 21.0:1
    assert round(compute_contrast_ratio((0, 0, 0), (255, 255, 255)), 2) == 21.0
    # Light gray on white fails WCAG AA (< 4.5)
    assert compute_contrast_ratio((200, 200, 200), (255, 255, 255)) < 4.5


def test_qa_engine_passing_across_all_five_breakpoints(tmp_path: Path) -> None:
    project = tmp_path / "paws-salon"
    (project / "website").mkdir(parents=True)
    (project / "assets/website").mkdir(parents=True)
    (project / "assets/website/hero.png").write_bytes(_make_png_bytes())
    (project / "website/index.html").write_text(
        """<!DOCTYPE html>
<html>
<head>
  <style>
    body { background: #ffffff; color: #111827; }
    h1 { color: #111827; background: #ffffff; }
    a.cta { color: #ffffff; background: #1e3a8a; }
  </style>
</head>
<body>
  <h1>Paws Salon</h1>
  <img src="../assets/website/hero.png" alt="Dog">
  <a class="cta" href="https://booking.paws.co.uk/book">Book Now</a>
</body>
</html>""",
        encoding="utf-8",
    )

    lifecycle_events: list[str] = []
    report = run_qa_engine(
        project,
        playwright_factory=lambda: _FakePlaywrightManager(
            simulate_defects=False,
            events=lifecycle_events,
        ),
    )

    assert isinstance(report, QADefectReport)
    assert report.passed is True
    assert [v.width for v in report.viewports] == [1440, 1024, 768, 390, 360]
    assert [v.label for v in report.viewports] == [b[0] for b in BREAKPOINTS]

    for width in (1440, 1024, 768, 390, 360):
        shot = project / "qa" / "screenshots" / f"{width}px.png"
        assert shot.is_file()
        assert f"images_prepared:{width}" in lifecycle_events
        assert f"context_closed:{width}" in lifecycle_events
        assert f"page_closed:{width}" in lifecycle_events

    assert "browser_closed" in lifecycle_events

    report_path = project / "qa" / "qa-report.json"
    assert report_path.is_file()
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["passed"] is True


def test_qa_engine_flags_overflow_404s_console_errors_and_contrast_defects(
    tmp_path: Path,
) -> None:
    project = tmp_path / "defective-salon"
    (project / "website").mkdir(parents=True)
    (project / "website/index.html").write_text(
        "<html><body><h1>Low Contrast</h1><a class='cta' href='/book'>Book</a></body></html>",
        encoding="utf-8",
    )

    lifecycle_events: list[str] = []
    report = run_qa_engine(
        project,
        playwright_factory=lambda: _FakePlaywrightManager(
            simulate_defects=True,
            events=lifecycle_events,
        ),
    )

    assert report.passed is False
    mobile_390 = next(v for v in report.viewports if v.width == 390)
    assert mobile_390.horizontal_overflow is True
    assert len(mobile_390.broken_assets) >= 2  # 404 response + naturalWidth === 0
    assert len(mobile_390.console_errors) >= 2  # console.error + pageerror
    assert len(mobile_390.contrast_failures) == 1
    assert mobile_390.contrast_failures[0].selector == "a.cta"

    # Even when defects occur, every context and the browser must be closed in finally blocks
    for width in (1440, 1024, 768, 390, 360):
        assert f"context_closed:{width}" in lifecycle_events
    assert "browser_closed" in lifecycle_events
