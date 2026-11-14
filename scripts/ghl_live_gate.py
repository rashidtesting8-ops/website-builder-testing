from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

WIDTHS = (1440, 1024, 768, 390, 360)
STYLE_SELECTORS = (
    "header",
    "nav",
    "h1",
    "h2",
    "p",
    "a",
    "button",
    "img",
    "section",
    "footer",
)
STYLE_PROPERTIES = (
    "color",
    "backgroundColor",
    "fontFamily",
    "fontSize",
    "fontWeight",
    "lineHeight",
    "marginTop",
    "marginBottom",
    "paddingTop",
    "paddingBottom",
    "display",
    "position",
    "borderRadius",
)
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_SRCSET_URL_RE = re.compile(r"(?:^|,)\s*([^\s,]+)")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"<>]+")
_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}


def _safe_url_for_report(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "[redacted]", ""))


def _safe_message_for_report(message: str) -> str:
    def redact(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        suffix = ""
        while raw_url and raw_url[-1] in ".,);]":
            suffix = raw_url[-1] + suffix
            raw_url = raw_url[:-1]
        return _safe_url_for_report(raw_url) + suffix

    return _URL_IN_TEXT_RE.sub(redact, message)


def _is_local_host(host: str) -> bool:
    clean_host = host.strip("[]").rstrip(".").lower()
    if clean_host in _LOCAL_HOSTS or clean_host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(clean_host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _validate_published_url(url: str, *, allow_local_http: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("published_url must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ValueError("published_url must not contain embedded credentials")
    if allow_local_http:
        if parts.scheme != "http" or not _is_local_host(parts.hostname):
            raise ValueError("Local test mode only permits an HTTP loopback host")
        return
    if parts.scheme != "https":
        raise ValueError("published_url must use HTTPS")
    if _is_local_host(parts.hostname):
        raise ValueError("published_url must not point to a local or private host")


def _normalise_inline_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").splitlines()).strip()


def _inline_blocks(html: str, tag_name: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [
        _normalise_inline_text(tag.get_text("\n"))
        for tag in soup.find_all(tag_name)
        if _normalise_inline_text(tag.get_text("\n"))
    ]


def _asset_references(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    raw_urls: list[str] = []
    for tag in soup.find_all(True):
        for attribute in ("src", "poster"):
            value = tag.get(attribute)
            if isinstance(value, str) and value.strip():
                raw_urls.append(value.strip())
        srcset = tag.get("srcset")
        if isinstance(srcset, str):
            raw_urls.extend(match.group(1).strip() for match in _SRCSET_URL_RE.finditer(srcset))
        if tag.name == "link":
            rel = tag.get("rel") or []
            rels = [rel] if isinstance(rel, str) else rel
            href = tag.get("href")
            if (
                isinstance(href, str)
                and href.strip()
                and any(
                    str(item).lower()
                    in {"stylesheet", "icon", "preload", "modulepreload", "manifest"}
                    for item in rels
                )
            ):
                raw_urls.append(href.strip())
        if tag.name == "style":
            raw_urls.extend(
                match.group(2).strip()
                for match in _CSS_URL_RE.finditer(tag.get_text())
            )
        style_attribute = tag.get("style")
        if isinstance(style_attribute, str):
            raw_urls.extend(
                match.group(2).strip()
                for match in _CSS_URL_RE.finditer(style_attribute)
            )

    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_urls:
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        resolved = urljoin(base_url, raw)
        key = (raw, resolved)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"raw": raw, "url": resolved})
    return refs


def _is_test_local_url(url: str, published_url: str) -> bool:
    parts = urlsplit(url)
    published = urlsplit(published_url)
    return (
        parts.scheme == "http"
        and parts.hostname is not None
        and _is_local_host(parts.hostname)
        and parts.hostname == published.hostname
        and parts.port == published.port
    )


def _local_reference_reason(raw: str, resolved: str) -> str | None:
    raw_parts = urlsplit(raw)
    resolved_parts = urlsplit(resolved)
    if not raw_parts.scheme and not raw.startswith("//"):
        return "relative_or_local_path"
    if raw_parts.scheme == "file":
        return "file_url"
    if resolved_parts.hostname and _is_local_host(resolved_parts.hostname):
        return "localhost_or_private_host"
    return None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except (OSError, TypeError):
        temp_path.unlink(missing_ok=True)
        raise


def _check(
    passed: bool,
    details: Any,
) -> dict[str, Any]:
    return {"passed": bool(passed), "details": details}


def _computed_styles(page: Any, selectors: tuple[str, ...], root_id: str | None) -> dict[str, Any]:
    return page.evaluate(
        """({selectors, rootId, properties}) => {
          const scope = rootId ? document.getElementById(rootId) : document;
          const result = {};
          for (const selector of selectors) {
            const element = scope ? scope.querySelector(selector) : null;
            if (!element) {
              result[selector] = null;
              continue;
            }
            const style = getComputedStyle(element);
            result[selector] = Object.fromEntries(
              properties.map(property => [property, style[property]])
            );
          }
          return result;
        }""",
        {
            "selectors": list(selectors),
            "rootId": root_id,
            "properties": list(STYLE_PROPERTIES),
        },
    )


def _style_differences(reference: dict[str, Any], observed: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for selector in STYLE_SELECTORS:
        expected = reference.get(selector)
        actual = observed.get(selector)
        if expected is None or actual is None:
            differences.append(
                {
                    "selector": selector,
                    "reason": "element_missing",
                    "expected": expected is not None,
                }
            )
            continue
        for property_name in STYLE_PROPERTIES:
            expected_value = expected.get(property_name)
            actual_value = actual.get(property_name)
            if expected_value != actual_value:
                differences.append(
                    {
                        "selector": selector,
                        "property": property_name,
                        "approved": expected_value,
                        "published": actual_value,
                    }
                )
    return differences


def _outside_snapshot(page: Any, root_id: str) -> dict[str, dict[str, str]]:
    return page.evaluate(
        """({rootId, properties}) => {
          const root = document.getElementById(rootId);
          const counts = new Map();
          const result = {};
          const nodes = [
            document.documentElement,
            document.body,
            ...document.body.querySelectorAll('*')
          ].filter(element =>
            !element.closest('script, style, noscript') &&
            (!root || (element !== root && !root.contains(element)))
          );
          for (const element of nodes) {
            const classes = [...element.classList].sort().join('.');
            const signature = element.id
              ? `${element.tagName.toLowerCase()}#${element.id}`
              : `${element.tagName.toLowerCase()}${classes ? '.' + classes : ''}`;
            const index = counts.get(signature) || 0;
            counts.set(signature, index + 1);
            const key = `${signature}[${index}]`;
            const style = getComputedStyle(element);
            const computed = {};
            const flowProperties = new Set([
              'block-size',
              'height',
              'perspective-origin',
              'transform-origin'
            ]);
            for (let index = 0; index < style.length; index += 1) {
              const property = style.item(index);
              if (flowProperties.has(property)) continue;
              computed[property] = style.getPropertyValue(property);
            }
            result[key] = computed;
          }
          return result;
        }""",
        {"rootId": root_id},
    )


def _compare_outside_styles(
    baseline: dict[str, dict[str, str]],
    published: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for key in sorted(set(baseline) | set(published)):
        before = baseline.get(key)
        after = published.get(key)
        if before is None or after is None:
            differences.append({"element": key, "reason": "element_added_or_removed"})
            continue
        for property_name in sorted(set(before) | set(after)):
            if before.get(property_name) != after.get(property_name):
                differences.append(
                    {
                        "element": key,
                        "property": property_name,
                        "baseline": before.get(property_name),
                        "published": after.get(property_name),
                    }
                )
    return differences


def _interactions(page: Any, root_id: str) -> dict[str, Any]:
    return page.evaluate(
        """rootId => {
          const root = document.getElementById(rootId);
          if (!root) return {menu: {passed: false, reason: 'root_missing'},
                             slider: {passed: false, reason: 'root_missing'}};

          const toggle = root.querySelector('[data-ev-nav-toggle]');
          const drawer = root.querySelector('[data-ev-nav-drawer]');
          let menu;
          if (!toggle || !drawer) {
            menu = {passed: false, reason: 'menu_toggle_or_drawer_missing'};
          } else {
            const initialExpanded = toggle.getAttribute('aria-expanded');
            const initialCollapsed = drawer.getAttribute('data-collapsed');
            toggle.click();
            const opened = toggle.getAttribute('aria-expanded') !== initialExpanded &&
              drawer.getAttribute('data-collapsed') !== initialCollapsed;
            toggle.click();
            const closed = toggle.getAttribute('aria-expanded') === initialExpanded &&
              drawer.getAttribute('data-collapsed') === initialCollapsed;
            menu = {passed: opened && closed, opened, closed};
          }

          const range = root.querySelector('[data-ev-ba-range]');
          const after = root.querySelector('[data-ev-ba-after]');
          let slider;
          if (!range && !after) {
            slider = {passed: true, present: false, reason: 'no_slider_in_snippet'};
          } else if (!range || !after) {
            slider = {passed: false, present: true, reason: 'slider_control_or_target_missing'};
          } else {
            const original = range.value;
            const testValue = original === '37' ? '63' : '37';
            range.value = testValue;
            range.dispatchEvent(new Event('input', {bubbles: true}));
            const moved = after.getAttribute('data-split') === testValue;
            range.value = original;
            range.dispatchEvent(new Event('input', {bubbles: true}));
            slider = {passed: moved, present: true, changed: moved};
          }
          return {menu, slider};
        }""",
        root_id,
    )


def _strip_root_html(html: str, root_id: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for root in soup.find_all(id=root_id):
        root.decompose()
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        if soup.html is not None:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    base = soup.new_tag("base", href=base_url)
    head.insert(0, base)
    return str(soup)


def _check_asset_urls(
    context: Any,
    refs: list[dict[str, str]],
    published_url: str,
    *,
    allow_local_http: bool,
    timeout_ms: int,
) -> dict[str, Any]:
    from playwright.sync_api import Error as PlaywrightError

    results: list[dict[str, Any]] = []
    for ref in refs:
        raw = ref["raw"]
        url = ref["url"]
        local_reason = _local_reference_reason(raw, url)
        parts = urlsplit(url)
        test_local = allow_local_http and _is_test_local_url(url, published_url)
        if local_reason and not test_local:
            results.append(
                {
                    "raw": raw,
                    "url": _safe_url_for_report(url),
                    "status": None,
                    "reason": local_reason,
                }
            )
            continue
        if parts.scheme != "https" and not test_local:
            results.append(
                {
                    "raw": raw,
                    "url": _safe_url_for_report(url),
                    "status": None,
                    "reason": "asset_must_use_https",
                }
            )
            continue
        try:
            response = context.request.get(url, timeout=timeout_ms, max_redirects=5)
        except PlaywrightError as exc:
            results.append(
                {
                    "raw": raw,
                    "url": _safe_url_for_report(url),
                    "status": None,
                    "reason": f"request_error: {type(exc).__name__}: {exc}",
                }
            )
            continue
        final_url = response.url
        status = response.status
        final_parts = urlsplit(final_url)
        final_is_secure = final_parts.scheme == "https" or (
            test_local and _is_test_local_url(final_url, published_url)
        )
        results.append(
            {
                "raw": raw,
                "url": _safe_url_for_report(url),
                "final_url": _safe_url_for_report(final_url),
                "status": status,
                "passed": status == 200 and final_is_secure,
                "reason": None
                if status == 200 and final_is_secure
                else "expected_https_200",
            }
        )
    return {"passed": all(item.get("passed", False) for item in results), "responses": results}


def _load_release(project_root: Path) -> tuple[bytes, dict[str, Any]]:
    snippet_path = project_root / "delivery" / "ghl-ready.html"
    manifest_path = project_root / "delivery" / "release-manifest.json"
    if not snippet_path.is_file():
        raise ValueError(f"Missing delivered snippet: {snippet_path}")
    if not manifest_path.is_file():
        raise ValueError(f"Missing release manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid release manifest JSON: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("release-manifest.json must contain a JSON object")
    return snippet_path.read_bytes(), manifest


def _initial_checks(
    project_root: Path,
    snippet_bytes: bytes,
    manifest: dict[str, Any],
    published_url: str,
    *,
    allow_local_http: bool,
) -> tuple[dict[str, Any], str, str, str, list[dict[str, str]]]:
    snippet_html = snippet_bytes.decode("utf-8", errors="replace")
    snippet_sha = hashlib.sha256(snippet_bytes).hexdigest()
    manifest_files = manifest.get("checksum_tree")
    manifest_snippet_sha = (
        manifest_files.get("delivery/ghl-ready.html")
        if isinstance(manifest_files, dict)
        else None
    )
    project_id_value = manifest.get("project_id")
    project_id = (
        str(project_id_value).strip()
        if isinstance(project_id_value, str) and project_id_value.strip()
        else project_root.name
    )
    root_id = f"ghl-ev-engine-root-{project_id}"
    hash_matches = isinstance(manifest_snippet_sha, str) and snippet_sha == manifest_snippet_sha
    try:
        _validate_published_url(published_url, allow_local_http=allow_local_http)
        url_ok = True
        url_reason = "HTTPS URL accepted" if not allow_local_http else "Local test URL accepted"
    except ValueError as exc:
        url_ok = False
        url_reason = str(exc)
    refs = _asset_references(snippet_html, published_url)
    checks: dict[str, Any] = {
        "published_url": _check(
            url_ok,
            {"url": _safe_url_for_report(published_url), "reason": url_reason},
        ),
        "snippet_hash": _check(
            hash_matches,
            {
                "tested_snippet_sha256": snippet_sha,
                "manifest_snippet_sha256": manifest_snippet_sha,
            },
        ),
        "root_mounted": _check(False, {"root_id": root_id, "by_viewport": {}}),
        "published_page": _check(False, {"by_viewport": {}}),
        "script_style_survival": _check(False, {"scripts": {}, "styles": {}}),
        "computed_styles": _check(False, {"selectors": list(STYLE_SELECTORS), "differences": []}),
        "host_elements_unchanged": _check(False, {"differences": []}),
        "assets": _check(False, {"responses": []}),
        "console_errors": _check(False, {"errors": []}),
        "failed_requests": _check(False, {"requests": []}),
        "horizontal_overflow": _check(False, {"by_viewport": {}}),
        "broken_images": _check(False, {"by_viewport": {}}),
        "interactivity": _check(False, {"by_viewport": {}}),
        "screenshots": _check(False, {"paths": {}}),
    }
    return checks, project_id, root_id, snippet_html, refs


def _write_report(out_dir: Path, report: dict[str, Any]) -> Path:
    report_path = out_dir / "ghl-live-report.json"
    _write_json_atomic(report_path, report)
    return report_path


def run_gate(
    published_url: str,
    project_dir: str | Path,
    *,
    out: str | Path = "qa/ghl-live",
    timeout_ms: int = 20_000,
    _allow_local_http: bool = False,
) -> dict[str, Any]:
    project_root = Path(project_dir).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError(f"Project directory does not exist: {project_root}")
    output_dir = Path(out).expanduser()
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    output_dir = output_dir.resolve()
    if output_dir == project_root / "delivery" or project_root / "delivery" in output_dir.parents:
        raise ValueError("GHL live evidence must not be written inside immutable delivery/")
    output_dir.mkdir(parents=True, exist_ok=True)

    snippet_bytes, manifest = _load_release(project_root)
    checks, project_id, root_id, snippet_html, snippet_refs = _initial_checks(
        project_root,
        snippet_bytes,
        manifest,
        published_url,
        allow_local_http=_allow_local_http,
    )
    approved_path = project_root / "delivery" / "approved-homepage.html"
    release_hash = manifest.get("version_hash")
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "published_url": _safe_url_for_report(published_url),
        "project_id": project_id,
        "root_id": root_id,
        "release_hash": release_hash,
        "tested_snippet_sha256": hashlib.sha256(snippet_bytes).hexdigest(),
        "scope": (
            "Browser checks for the supplied published URL; "
            "GHL account behavior is not certified."
        ),
        "ghl_account_validation": "not_performed_by_tool",
        "checks": checks,
        "evidence": {"screenshots": {}, "report": ""},
        "viewports": {},
        "warnings": [],
    }

    if not approved_path.is_file():
        checks["computed_styles"] = _check(
            False, {"reason": "Missing local approved render", "path": str(approved_path)}
        )

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        checks["browser_runtime"] = _check(False, {"error": str(exc)})
        report["overall_pass"] = False
        report_path = _write_report(output_dir, report)
        report["evidence"]["report"] = str(report_path)
        _write_report(output_dir, report)
        return report

    browser_error: str | None = None
    root_by_viewport: dict[str, bool] = {}
    overflow_by_viewport: dict[str, Any] = {}
    images_by_viewport: dict[str, list[str]] = {}
    interactions_by_viewport: dict[str, Any] = {}
    screenshots: dict[str, str] = {}
    console_errors: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    page_responses: list[dict[str, Any]] = []
    navigation_by_viewport: dict[str, dict[str, Any]] = {}
    script_style_details: dict[str, Any] = {}
    computed_differences: list[dict[str, Any]] = []
    computed_differences_by_viewport: dict[str, list[dict[str, Any]]] = {}
    computed_elements_by_viewport: dict[str, int] = {}
    outside_differences: list[dict[str, Any]] = []
    outside_differences_by_viewport: dict[str, list[dict[str, Any]]] = {}
    outside_baseline_counts: dict[str, int] = {}
    actual_asset_refs: list[dict[str, str]] = []
    asset_results: dict[str, Any] | None = None
    local_reference_page: Any = None
    baseline_reference_page: Any = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=False)
            if approved_path.is_file():
                local_reference_page = context.new_page()
                local_reference_page.set_viewport_size(
                    {"width": WIDTHS[0], "height": 1000}
                )
                try:
                    local_reference_page.goto(
                        approved_path.as_uri(),
                        wait_until="load",
                        timeout=timeout_ms,
                    )
                except PlaywrightError as exc:
                    checks["computed_styles"] = _check(
                        False,
                        {
                            "reason": "Could not render local approved HTML",
                            "error": _safe_message_for_report(str(exc)),
                        },
                    )
                    local_reference_page.close()
                    local_reference_page = None

            try:
                baseline_response = context.request.get(published_url, timeout=timeout_ms)
                if baseline_response.status != 200:
                    raise ValueError(f"Baseline page returned HTTP {baseline_response.status}")
                baseline_html = _strip_root_html(
                    baseline_response.text(),
                    root_id,
                    published_url,
                )
                baseline_reference_page = context.new_page()
                baseline_reference_page.set_viewport_size(
                    {"width": WIDTHS[0], "height": 1000}
                )
                baseline_reference_page.set_content(
                    baseline_html,
                    wait_until="load",
                    timeout=timeout_ms,
                )
                baseline_reference_page.wait_for_timeout(100)
                baseline_reference_page.evaluate(
                    "rootId => document.getElementById(rootId)?.remove()",
                    root_id,
                )
            except (PlaywrightError, ValueError, OSError) as exc:
                if baseline_reference_page is not None:
                    baseline_reference_page.close()
                    baseline_reference_page = None
                checks["host_elements_unchanged"] = _check(
                    False,
                    {
                        "reason": "Could not create host-without-embed baseline",
                        "error": _safe_message_for_report(str(exc)),
                    },
                )

            for width in WIDTHS:
                page = context.new_page()
                page.set_viewport_size({"width": width, "height": 1000})
                page.set_default_timeout(10_000)
                width_key = str(width)
                page_errors: list[dict[str, Any]] = []
                request_errors: list[dict[str, Any]] = []
                response_errors: list[dict[str, Any]] = []
                page.on(
                    "console",
                    lambda message, w=width, errors=page_errors: errors.append(
                        {
                            "width": w,
                            "type": message.type,
                            "text": _safe_message_for_report(message.text),
                        }
                    )
                    if message.type == "error"
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda error, w=width, errors=page_errors: errors.append(
                        {
                            "width": w,
                            "type": "pageerror",
                            "text": _safe_message_for_report(str(error)),
                        }
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request, w=width, errors=request_errors: errors.append(
                        {
                            "width": w,
                            "url": _safe_url_for_report(request.url),
                            "failure": request.failure,
                        }
                    ),
                )
                page.on(
                    "response",
                    lambda response, w=width, errors=response_errors: errors.append(
                        {
                            "width": w,
                            "url": _safe_url_for_report(response.url),
                            "status": response.status,
                            "resource_type": response.request.resource_type,
                        }
                    )
                    if response.status >= 400
                    else None,
                )

                navigation_status: int | None = None
                navigation_error: str | None = None
                try:
                    response = page.goto(published_url, wait_until="load", timeout=timeout_ms)
                    navigation_status = response.status if response is not None else None
                    page.evaluate(
                        """async () => {
                          const images = [...document.images];
                          for (const image of images) image.loading = 'eager';
                          await Promise.race([
                            Promise.all(images.map(image => image.decode().catch(() => null))),
                            new Promise(resolve => setTimeout(resolve, 5000))
                          ]);
                          if (document.fonts) {
                            await Promise.race([
                              document.fonts.ready,
                              new Promise(resolve => setTimeout(resolve, 3000))
                            ]);
                          }
                        }""",
                    )
                    page.wait_for_timeout(100)
                except PlaywrightError as exc:
                    navigation_error = _safe_message_for_report(str(exc))
                navigation_by_viewport[width_key] = {
                    "status": navigation_status,
                    "error": navigation_error,
                }

                root_present = bool(
                    page.evaluate("rootId => Boolean(document.getElementById(rootId))", root_id)
                )
                root_by_viewport[width_key] = root_present
                if baseline_reference_page is not None and root_present:
                    baseline_reference_page.set_viewport_size(
                        {"width": width, "height": 1000}
                    )
                    baseline_styles = _outside_snapshot(
                        baseline_reference_page,
                        root_id,
                    )
                    published_outside = _outside_snapshot(page, root_id)
                    outside_baseline_counts[width_key] = len(baseline_styles)
                    outside_differences_by_viewport[width_key] = _compare_outside_styles(
                        baseline_styles,
                        published_outside,
                    )
                if local_reference_page is not None and root_present:
                    local_reference_page.set_viewport_size(
                        {"width": width, "height": 1000}
                    )
                    local_styles = _computed_styles(
                        local_reference_page,
                        STYLE_SELECTORS,
                        None,
                    )
                    published_styles = _computed_styles(page, STYLE_SELECTORS, root_id)
                    computed_differences_by_viewport[width_key] = _style_differences(
                        local_styles,
                        published_styles,
                    )
                    computed_elements_by_viewport[width_key] = sum(
                        1
                        for selector in STYLE_SELECTORS
                        if local_styles.get(selector) is not None
                    )
                dimensions = page.evaluate(
                    """() => ({
                      viewport: window.innerWidth,
                      scrollWidth: Math.max(document.documentElement.scrollWidth,
                                             document.body?.scrollWidth || 0),
                      overflow: Math.max(document.documentElement.scrollWidth,
                                         document.body?.scrollWidth || 0) > window.innerWidth + 2,
                      brokenImages: [...document.images]
                        .filter(image => !image.complete || image.naturalWidth === 0)
                        .map(image => image.currentSrc || image.src || 'empty')
                    })"""
                )
                overflow_by_viewport[width_key] = {
                    "viewport": dimensions["viewport"],
                    "scroll_width": dimensions["scrollWidth"],
                    "overflow": bool(dimensions["overflow"]),
                }
                images_by_viewport[width_key] = list(dimensions["brokenImages"])
                screenshot_path = output_dir / f"{width}px.png"
                try:
                    page.screenshot(
                        path=str(screenshot_path),
                        full_page=True,
                        animations="disabled",
                    )
                    screenshots[width_key] = str(screenshot_path)
                except PlaywrightError as exc:
                    page_errors.append(
                        {
                            "width": width,
                            "type": "screenshot_error",
                            "text": _safe_message_for_report(str(exc)),
                        }
                    )

                if width == WIDTHS[0] and root_present:
                    root_html = page.evaluate(
                        "rootId => document.getElementById(rootId).outerHTML",
                        root_id,
                    )
                    actual_asset_refs = _asset_references(root_html, published_url)
                    actual_scripts = page.evaluate(
                        "() => [...document.querySelectorAll('script')]"
                        ".map(tag => tag.textContent || '')"
                    )
                    actual_styles = page.evaluate(
                        "() => [...document.querySelectorAll('style')]"
                        ".map(tag => tag.textContent || '')"
                    )
                    source_scripts = _inline_blocks(snippet_html, "script")
                    source_styles = _inline_blocks(snippet_html, "style")
                    normalized_actual_scripts = [
                        _normalise_inline_text(value) for value in actual_scripts if value.strip()
                    ]
                    normalized_actual_styles = [
                        _normalise_inline_text(value) for value in actual_styles if value.strip()
                    ]
                    missing_scripts = [
                        block for block in source_scripts if block not in normalized_actual_scripts
                    ]
                    missing_styles = [
                        block for block in source_styles if block not in normalized_actual_styles
                    ]
                    script_style_details = {
                        "source_script_blocks": len(source_scripts),
                        "published_script_blocks": len(normalized_actual_scripts),
                        "source_style_blocks": len(source_styles),
                        "published_style_blocks": len(normalized_actual_styles),
                        "missing_or_altered_scripts": len(missing_scripts),
                        "missing_or_altered_styles": len(missing_styles),
                    }
                interaction_result: dict[str, Any]
                if width <= 768 and root_present:
                    interaction_result = _interactions(page, root_id)
                elif width <= 768:
                    interaction_result = {
                        "menu": {"passed": False, "reason": "root_missing"},
                        "slider": {"passed": False, "reason": "root_missing"},
                    }
                else:
                    interaction_result = {
                        "menu": {"passed": True, "not_applicable": True},
                        "slider": {"passed": True, "not_applicable": True},
                    }
                interactions_by_viewport[width_key] = interaction_result
                console_errors.extend(page_errors)
                failed_requests.extend(request_errors)
                page_responses.extend(response_errors)
                page.close()
            computed_differences = [
                difference
                for viewport_differences in computed_differences_by_viewport.values()
                for difference in viewport_differences
            ]
            if local_reference_page is not None:
                local_reference_page.close()
            if baseline_reference_page is not None:
                baseline_reference_page.close()
            outside_differences = [
                difference
                for viewport_differences in outside_differences_by_viewport.values()
                for difference in viewport_differences
            ]
            references_by_key = {
                (item["raw"], item["url"]): item
                for item in [*snippet_refs, *actual_asset_refs]
            }
            asset_results = _check_asset_urls(
                context,
                list(references_by_key.values()),
                published_url,
                allow_local_http=_allow_local_http,
                timeout_ms=timeout_ms,
            )
            context.close()
            browser.close()
    except PlaywrightError as exc:
        browser_error = _safe_message_for_report(str(exc))

    if browser_error is not None:
        checks["browser_runtime"] = _check(False, {"error": browser_error})

    root_passed = len(root_by_viewport) == len(WIDTHS) and all(root_by_viewport.values())
    checks["root_mounted"] = _check(
        root_passed,
        {"root_id": root_id, "by_viewport": root_by_viewport},
    )
    checks["published_page"] = _check(
        len(navigation_by_viewport) == len(WIDTHS)
        and all(item["status"] == 200 and item["error"] is None
                for item in navigation_by_viewport.values()),
        {"by_viewport": navigation_by_viewport},
    )
    if script_style_details:
        survival_passed = (
            script_style_details["source_script_blocks"] > 0
            and script_style_details["source_style_blocks"] > 0
            and script_style_details["missing_or_altered_scripts"] == 0
            and script_style_details["missing_or_altered_styles"] == 0
        )
        checks["script_style_survival"] = _check(survival_passed, script_style_details)
    elif browser_error is None:
        checks["script_style_survival"] = _check(
            False,
            {"reason": "Root was not available for DOM comparison", **script_style_details},
        )

    if computed_differences_by_viewport:
        checks["computed_styles"] = _check(
            len(computed_differences_by_viewport) == len(WIDTHS)
            and not computed_differences
            and all(
                count == len(STYLE_SELECTORS)
                for count in computed_elements_by_viewport.values()
            ),
            {
                "selectors": list(STYLE_SELECTORS),
                "expected": len(STYLE_SELECTORS),
                "by_viewport": {
                    width: {
                        "compared": computed_elements_by_viewport.get(width, 0),
                        "differences": differences,
                    }
                    for width, differences in computed_differences_by_viewport.items()
                },
            },
        )
    if outside_baseline_counts:
        checks["host_elements_unchanged"] = _check(
            len(outside_differences_by_viewport) == len(WIDTHS)
            and not outside_differences,
            {
                "baseline_elements_by_viewport": outside_baseline_counts,
                "by_viewport": outside_differences_by_viewport,
                "baseline_method": (
                    "Fetched host HTML with the snippet root removed, then rendered locally "
                    "at each tested viewport. All computed CSS properties are compared; "
                    "flow-dependent sizing/origin properties are omitted because the embed "
                    "naturally changes host document flow."
                ),
            },
        )

    if interactions_by_viewport:
        mobile_results = {
            width: result
            for width, result in interactions_by_viewport.items()
            if int(width) <= 768
        }
        interactivity_passed = bool(mobile_results) and all(
            result["menu"]["passed"] and result["slider"]["passed"]
            for result in mobile_results.values()
        )
        checks["interactivity"] = _check(
            interactivity_passed,
            {"by_viewport": interactions_by_viewport},
        )

    references_by_key = {
        (item["raw"], item["url"]): item for item in [*snippet_refs, *actual_asset_refs]
    }
    if asset_results is None:
        asset_results = {
            "passed": False,
            "responses": [],
            "reason": browser_error or "Browser checks did not reach asset validation",
        }
    checks["assets"] = _check(
        bool(asset_results["passed"]),
        {
            **asset_results,
            "reference_count": len(references_by_key),
            "test_local_http_allowed": _allow_local_http,
        },
    )

    all_failed_requests = [
        *failed_requests,
        *page_responses,
    ]
    checks["console_errors"] = _check(not console_errors, {"errors": console_errors})
    checks["failed_requests"] = _check(
        not all_failed_requests,
        {"requests": all_failed_requests},
    )
    checks["horizontal_overflow"] = _check(
        len(overflow_by_viewport) == len(WIDTHS)
        and all(not item["overflow"] for item in overflow_by_viewport.values()),
        {"by_viewport": overflow_by_viewport},
    )
    checks["broken_images"] = _check(
        len(images_by_viewport) == len(WIDTHS)
        and all(not images for images in images_by_viewport.values()),
        {"by_viewport": images_by_viewport},
    )
    checks["interactivity"] = checks.get(
        "interactivity",
        _check(False, {"reason": "No viewport interaction results"}),
    )
    checks["screenshots"] = _check(
        len(screenshots) == len(WIDTHS),
        {"paths": screenshots, "expected_widths": list(WIDTHS)},
    )
    report["viewports"] = {
        width: {
            "root_present": root_by_viewport.get(width, False),
            "horizontal_overflow": overflow_by_viewport.get(width),
            "broken_images": images_by_viewport.get(width, []),
            "interactivity": interactions_by_viewport.get(width),
            "screenshot": screenshots.get(width),
        }
        for width in map(str, WIDTHS)
    }
    report["evidence"]["screenshots"] = screenshots
    report["evidence"]["report"] = str(output_dir / "ghl-live-report.json")
    report["checks"] = checks
    report["overall_pass"] = all(item["passed"] for item in checks.values())
    report["warnings"].append(
        "A passing browser report is not a GHL account approval or a substitute for human review."
    )
    _write_report(output_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run browser checks against a published GHL page and its approved release."
    )
    parser.add_argument("published_url")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--out", default="qa/ghl-live")
    args = parser.parse_args(argv)
    try:
        _validate_published_url(args.published_url, allow_local_http=False)
        report = run_gate(args.published_url, args.project_dir, out=args.out)
    except (OSError, ValueError) as exc:
        print(f"ghl-live-gate: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
