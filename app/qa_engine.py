"""Automated 5-viewport Playwright QA runner with WCAG AA contrast, overflow, and asset checks."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BREAKPOINTS: tuple[tuple[str, int], ...] = (
    ("Desktop", 1440),
    ("Laptop/Small Desktop", 1024),
    ("Tablet", 768),
    ("Mobile Large", 390),
    ("Mobile Small", 360),
)

_LOAD_IMAGES_JS = """async () => {
    const images = [...document.images];
    for (const img of images) img.loading = 'eager';
    await Promise.all(images.map(img => {
        if (img.complete) return Promise.resolve();
        return new Promise(resolve => {
            const finish = () => {
                clearTimeout(timeout);
                resolve();
            };
            const timeout = setTimeout(finish, 5000);
            img.addEventListener('load', finish, { once: true });
            img.addEventListener('error', finish, { once: true });
        });
    }));
}"""

_BROWSER_INSPECTION_JS = """() => {
    const overflow = document.documentElement.scrollWidth > window.innerWidth;
    const scrollWidth = document.documentElement.scrollWidth;
    const innerWidth = window.innerWidth;

    const brokenImages = [...document.images]
        .filter(img => !img.complete || img.naturalWidth === 0)
        .map(img => img.getAttribute('src') || 'empty');

    function parseRgb(str) {
        const m = (str || '').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/i);
        if (!m) return null;
        const alpha = m[4] !== undefined ? parseFloat(m[4]) : 1.0;
        if (alpha === 0) return null;
        return [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10)];
    }

    function effectiveBackground(el) {
        let cur = el;
        while (cur && cur !== document.documentElement) {
            const bg = parseRgb(window.getComputedStyle(cur).backgroundColor);
            if (bg) return bg;
            cur = cur.parentElement;
        }
        return parseRgb(window.getComputedStyle(document.body).backgroundColor) || [255, 255, 255];
    }

    function relLuminance(rgb) {
        const chans = rgb.map(v => {
            const c = v / 255.0;
            return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2];
    }

    function contrastRatio(fg, bg) {
        const l1 = relLuminance(fg);
        const l2 = relLuminance(bg);
        const lighter = Math.max(l1, l2);
        const darker = Math.min(l1, l2);
        return (lighter + 0.05) / (darker + 0.05);
    }

    const selector = 'h1, h2, h3, h4, h5, h6, a.cta, a.btn, button, [role="button"]';
    const targets = [...document.querySelectorAll(selector)];
    const contrastFailures = [];
    for (const el of targets) {
        const text = (el.textContent || '').trim();
        if (!text) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const fg = parseRgb(style.color) || [0, 0, 0];
        const bg = effectiveBackground(el);
        const ratio = contrastRatio(fg, bg);
        const fontSizePx = parseFloat(style.fontSize || '16');
        const weight = parseInt(style.fontWeight || '400', 10) || 400;
        const isLargeText = fontSizePx >= 24.0 || (fontSizePx >= 18.66 && weight >= 700);
        const required = isLargeText ? 3.0 : 4.5;
        if (ratio < required) {
            const tag = el.tagName.toLowerCase();
            const cls = (el.className && typeof el.className === 'string')
                ? '.' + el.className.trim().split(/\\s+/).join('.')
                : '';
            contrastFailures.push({
                selector: (tag + cls).slice(0, 80),
                text: text.slice(0, 80),
                ratio: Number(ratio.toFixed(2)),
                required: required
            });
        }
    }

    return {
        overflow,
        scrollWidth,
        innerWidth,
        brokenImages,
        contrastFailures
    };
}"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """Calculate WCAG 2.1 relative luminance for an 8-bit sRGB tuple."""
    channels: list[float] = []
    for val in rgb:
        c = max(0, min(255, int(val))) / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def compute_contrast_ratio(
    fg_rgb: tuple[int, int, int],
    bg_rgb: tuple[int, int, int],
) -> float:
    """Compute the WCAG 2.1 contrast ratio between two RGB colors."""
    l1 = relative_luminance(fg_rgb)
    l2 = relative_luminance(bg_rgb)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


class ContrastDefect(BaseModel):
    """WCAG AA contrast defect record for a heading or CTA."""

    model_config = ConfigDict(extra="forbid")

    selector: str
    text: str
    ratio: float
    required: float = 4.5


class ViewportQAResult(BaseModel):
    """Per-viewport QA outcome across a single screen width."""

    model_config = ConfigDict(extra="forbid")

    label: str
    width: int
    screenshot_path: str
    horizontal_overflow: bool
    scroll_width: int
    inner_width: int
    broken_assets: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    contrast_failures: list[ContrastDefect] = Field(default_factory=list)
    passed: bool


class QADefectReport(BaseModel):
    """Structured QA defect report written to projects/<business-id>/qa/qa-report.json."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    target_url: str
    checked_at: datetime = Field(default_factory=_utc_now)
    passed: bool
    candidate_sha256: str = Field(default="")
    viewports: list[ViewportQAResult]


class _SilentHTTPHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _serve_project_directory(root_dir: Path) -> Iterator[int]:
    """Spin up an ephemeral local HTTP server bound to 127.0.0.1:<port>."""
    handler = partial(_SilentHTTPHandler, directory=str(root_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        yield port
    finally:
        server.shutdown()
        server.server_close()


def _default_playwright_factory() -> Any:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    return sync_playwright()


def run_qa_engine(
    project_folder: str | Path,
    *,
    html_rel_path: str | None = None,
    viewport_height: int = 900,
    playwright_factory: Callable[[], Any] | None = None,
) -> QADefectReport:
    """Run automated Playwright QA inspections across all 5 breakpoints and save qa-report.json."""
    project_root = Path(project_folder).resolve()
    if html_rel_path is None:
        if (project_root / "release-candidate" / "standalone.html").is_file():
            html_rel_path = "release-candidate/standalone.html"
        else:
            html_rel_path = "website/index.html"
    html_file = (project_root / html_rel_path).resolve()
    if not html_file.is_relative_to(project_root) or not html_file.is_file():
        raise ValueError(f"Target HTML file not found inside project: {html_file}")

    candidate_sha256 = hashlib.sha256(html_file.read_bytes()).hexdigest()

    screenshots_dir = project_root / "qa" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    report_path = project_root / "qa" / "qa-report.json"

    active_factory = playwright_factory or _default_playwright_factory
    viewport_results: list[ViewportQAResult] = []

    with _serve_project_directory(project_root) as port:
        rel_url_path = html_file.relative_to(project_root).as_posix()
        target_url = f"http://localhost:{port}/{rel_url_path}"

        with active_factory() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for label, width in BREAKPOINTS:
                    context = browser.new_context(
                        viewport={"width": width, "height": viewport_height},
                        device_scale_factor=1,
                    )
                    page: Any = None
                    try:
                        page = context.new_page()
                        network_404s: list[str] = []
                        console_errs: list[str] = []

                        def _on_response(
                            resp: Any,
                            bucket: list[str] = network_404s,
                        ) -> None:
                            if getattr(resp, "status", 200) >= 400:
                                bucket.append(f"{resp.status}:{resp.url}")

                        def _on_console(
                            msg: Any,
                            bucket: list[str] = console_errs,
                        ) -> None:
                            if getattr(msg, "type", "") == "error":
                                bucket.append(str(getattr(msg, "text", ""))[:240])

                        def _on_pageerror(
                            err: Any,
                            bucket: list[str] = console_errs,
                        ) -> None:
                            bucket.append(str(err)[:240])

                        page.on("response", _on_response)
                        page.on("console", _on_console)
                        page.on("pageerror", _on_pageerror)

                        page.goto(target_url, wait_until="load", timeout=20000)
                        page.wait_for_timeout(200)
                        page.evaluate(_LOAD_IMAGES_JS)

                        eval_res = page.evaluate(_BROWSER_INSPECTION_JS) or {}
                        overflow = bool(eval_res.get("overflow", False))
                        scroll_w = int(eval_res.get("scrollWidth", width))
                        inner_w = int(eval_res.get("innerWidth", width))
                        broken_imgs = [str(x) for x in eval_res.get("brokenImages", [])]
                        raw_contrast = eval_res.get("contrastFailures", [])
                        contrast_defects = [
                            ContrastDefect.model_validate(item) for item in raw_contrast
                        ]

                        shot_file = screenshots_dir / f"{width}px.png"
                        page.screenshot(
                            path=str(shot_file),
                            full_page=True,
                            animations="disabled",
                        )

                        combined_broken = list(dict.fromkeys([*network_404s, *broken_imgs]))
                        vp_passed = (
                            not overflow
                            and not combined_broken
                            and not console_errs
                            and not contrast_defects
                        )

                        viewport_results.append(
                            ViewportQAResult(
                                label=label,
                                width=width,
                                screenshot_path=shot_file.relative_to(project_root).as_posix(),
                                horizontal_overflow=overflow,
                                scroll_width=scroll_w,
                                inner_width=inner_w,
                                broken_assets=combined_broken,
                                console_errors=console_errs,
                                contrast_failures=contrast_defects,
                                passed=vp_passed,
                            )
                        )
                    finally:
                        if page is not None:
                            page.close()
                        context.close()
            finally:
                browser.close()

    overall_passed = all(v.passed for v in viewport_results)
    report = QADefectReport(
        project_id=project_root.name,
        target_url=target_url,
        passed=overall_passed,
        candidate_sha256=candidate_sha256,
        viewports=viewport_results,
    )
    _atomic_write_text(report_path, report.model_dump_json(indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run 5-viewport Playwright QA inspection against a local project."
    )
    parser.add_argument("--project", required=True, help="Path to project directory")
    parser.add_argument(
        "--html",
        default="website/index.html",
        help="Relative path to HTML file inside project",
    )
    args = parser.parse_args()
    report = run_qa_engine(args.project, html_rel_path=args.html)
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
