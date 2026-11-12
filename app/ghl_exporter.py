"""GoHighLevel (GHL) custom code exporter with CSS selector scoping and IIFE JS isolation."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Doctype, Tag

from engine.contracts import (
    DemoKind,
    contains_demo_banner,
    freeze_demo_metadata,
    has_demo_banner,
    read_demo_metadata,
)

_GLOBAL_LEAK_RE = re.compile(
    r"\b(?:window|document|globalThis)\s*(?:\.\s*[A-Za-z_$][A-Za-z0-9_$]*|\[\s*['\"][^'\"]+['\"]\s*\])\s*=(?!=)"
)

_CSS_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_ROOT_LEADING_RE = re.compile(r"^(?::root|html|body)\b", re.I)
_CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)([^)]*?)\1\s*\)", re.IGNORECASE)
_SRC_POSTER_PATTERN = re.compile(r"\b(src|poster)\s*=\s*(['\"])(.*?)\2", re.IGNORECASE)
_SRCSET_PATTERN = re.compile(r"\bsrcset\s*=\s*(['\"])(.*?)\2", re.IGNORECASE)


class GlobalLeakageError(ValueError):
    """Raised when candidate JavaScript assigns properties directly onto window.* or document.*."""


class UnscopedCSSError(ValueError):
    """Raised when any CSS rule selector lacks the mandatory root scoping prefix."""


def _is_absolute_url(url: str) -> bool:
    trimmed = url.strip()
    lower = trimmed.lower()
    return lower.startswith(
        ("http://", "https://", "//", "data:", "blob:", "mailto:", "tel:", "#", "javascript:")
    )


def _resolve_asset_url(
    raw_url: str,
    cdn_base_url: str | None,
    asset_url_map: dict[str, str] | None,
) -> str:
    raw_clean = raw_url.strip()
    if not raw_clean or _is_absolute_url(raw_clean):
        return raw_url

    unslashed = raw_clean.lstrip("/")
    slashed = f"/{unslashed}"

    if asset_url_map:
        if raw_clean in asset_url_map:
            return asset_url_map[raw_clean]
        if unslashed in asset_url_map:
            return asset_url_map[unslashed]
        if slashed in asset_url_map:
            return asset_url_map[slashed]

    if cdn_base_url:
        base = cdn_base_url.rstrip("/")
        return f"{base}/{unslashed}"

    return raw_url


def rewrite_ghl_asset_urls(
    html_text: str,
    *,
    cdn_base_url: str | None = None,
    asset_url_map: dict[str, str] | None = None,
) -> str:
    """Rewrite relative asset paths (images, backgrounds, srcset) to CDN URLs.

    Optionally maps to explicit remote endpoint dictionaries.
    """
    if not cdn_base_url and not asset_url_map:
        return html_text

    def _replace_css_url(match: re.Match[str]) -> str:
        raw_val = match.group(2).strip()
        resolved = _resolve_asset_url(raw_val, cdn_base_url, asset_url_map)
        return f'url("{resolved}")'

    def _replace_src_poster(match: re.Match[str]) -> str:
        attr = match.group(1)
        quote = match.group(2)
        raw_val = match.group(3).strip()
        resolved = _resolve_asset_url(raw_val, cdn_base_url, asset_url_map)
        return f"{attr}={quote}{resolved}{quote}"

    def _replace_srcset(match: re.Match[str]) -> str:
        quote = match.group(1)
        raw_val = match.group(2)
        candidates = []
        for cand in raw_val.split(","):
            parts = cand.strip().split()
            if parts:
                parts[0] = _resolve_asset_url(parts[0], cdn_base_url, asset_url_map)
                candidates.append(" ".join(parts))
        return f"srcset={quote}{', '.join(candidates)}{quote}"

    out = _CSS_URL_PATTERN.sub(_replace_css_url, html_text)
    out = _SRC_POSTER_PATTERN.sub(_replace_src_poster, out)
    out = _SRCSET_PATTERN.sub(_replace_srcset, out)
    return out


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


def _scope_single_selector(selector: str, root_selector: str) -> str:
    sel = " ".join(selector.strip().split())
    if not sel:
        return root_selector

    if sel.startswith(root_selector):
        return sel

    # Move :root, html, or body directly to the root container ID
    if sel.lower() in (":root", "html", "body"):
        return root_selector

    # Replace leading :root / html / body in compound selectors (e.g., "body .hero")
    if _ROOT_LEADING_RE.match(sel):
        rest = _ROOT_LEADING_RE.sub("", sel, count=1).strip()
        return f"{root_selector} {rest}" if rest else root_selector

    return f"{root_selector} {sel}"


def _scope_selector_list(selector_group: str, root_selector: str) -> str:
    raw_parts = [part.strip() for part in selector_group.split(",") if part.strip()]
    scoped_parts: list[str] = []
    for part in raw_parts:
        scoped = _scope_single_selector(part, root_selector)
        if scoped not in scoped_parts:
            scoped_parts.append(scoped)
    return ", ".join(scoped_parts)


def _split_top_level_css_blocks(css: str) -> list[tuple[str, str]]:
    """Parse CSS into a sequence of (header_or_statement, body_without_outer_braces)."""
    blocks: list[tuple[str, str]] = []
    n = len(css)
    i = 0
    header_start = 0

    while i < n:
        ch = css[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if css[i] == "\\":
                    i += 2
                    continue
                if css[i] == quote:
                    break
                i += 1
        elif ch == ";" and "{" not in css[header_start:i]:
            stmt = css[header_start : i + 1].strip()
            if stmt:
                blocks.append((stmt, ""))
            header_start = i + 1
        elif ch == "{":
            header = css[header_start:i].strip()
            depth = 1
            body_start = i + 1
            i += 1
            while i < n and depth > 0:
                c = css[i]
                if c in ("'", '"'):
                    q = c
                    i += 1
                    while i < n:
                        if css[i] == "\\":
                            i += 2
                            continue
                        if css[i] == q:
                            break
                        i += 1
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
            body = css[body_start : i - 1].strip()
            blocks.append((header, body))
            header_start = i
            continue
        i += 1

    return blocks


def scope_css(css_text: str, root_selector: str) -> str:
    """Prefix every CSS rule selector with root_selector and move :root vars to root_selector."""
    cleaned = _CSS_COMMENT_RE.sub("", css_text)
    blocks = _split_top_level_css_blocks(cleaned)
    out: list[str] = []

    for header, body in blocks:
        if not body and header.endswith(";"):
            out.append(header)
            continue

        lower_hdr = header.lower()
        if lower_hdr.startswith(("@media", "@supports", "@container")):
            nested_scoped = scope_css(body, root_selector)
            indented = "\n".join(f"  {line}" for line in nested_scoped.splitlines() if line.strip())
            out.append(f"{header} {{\n{indented}\n}}")
        elif lower_hdr.startswith(("@keyframes", "@-webkit-keyframes", "@font-face")):
            out.append(f"{header} {{\n  {body}\n}}")
        else:
            scoped_header = _scope_selector_list(header, root_selector)
            declarations = "; ".join(
                d.strip() for d in body.split(";") if d.strip()
            )
            if declarations:
                declarations += ";"
            out.append(f"{scoped_header} {{ {declarations} }}")

    return "\n".join(out)


def find_unscoped_selectors(css_text: str, root_selector: str) -> list[str]:
    """Return any style rule selector in css_text that does not begin with root_selector."""
    cleaned = _CSS_COMMENT_RE.sub("", css_text)
    blocks = _split_top_level_css_blocks(cleaned)
    offending: list[str] = []

    for header, body in blocks:
        if not body and header.endswith(";"):
            continue
        lower_hdr = header.lower()
        if lower_hdr.startswith(("@media", "@supports", "@container")):
            offending.extend(find_unscoped_selectors(body, root_selector))
        elif lower_hdr.startswith(("@keyframes", "@-webkit-keyframes", "@font-face")):
            continue
        else:
            for part in header.split(","):
                sel = part.strip()
                if not sel:
                    continue
                if not sel.startswith(root_selector):
                    offending.append(sel)

    return offending


def check_no_global_js_leakage(js_text: str) -> None:
    """Raise GlobalLeakageError if JS assigns directly to window.*, document.*, or globalThis.*."""
    match = _GLOBAL_LEAK_RE.search(js_text)
    if match:
        raise GlobalLeakageError(
            f"Prohibited global assignment detected in script: {match.group(0)!r}"
        )


def isolate_javascript(js_text: str, root_id: str) -> str:
    """Wrap scripts in a strict IIFE with a MutationObserver fallback for dynamic GHL mounting."""
    check_no_global_js_leakage(js_text)
    body = js_text.strip()
    return (
        "(function(){ 'use strict';\n"
        f"  var ROOT_ID = {root_id!r};\n"
        "  var initialized = false;\n"
        "  function init(root) {\n"
        "    if (initialized || !root) { return; }\n"
        "    initialized = true;\n"
        f"    {body}\n"
        "  }\n"
        "  var existing = document.getElementById(ROOT_ID);\n"
        "  if (existing) {\n"
        "    init(existing);\n"
        "  } else if (typeof MutationObserver !== 'undefined') {\n"
        "    var observer = new MutationObserver(function() {\n"
        "      var el = document.getElementById(ROOT_ID);\n"
        "      if (el) {\n"
        "        observer.disconnect();\n"
        "        init(el);\n"
        "      }\n"
        "    });\n"
        "    observer.observe(document.documentElement || document.body, {\n"
        "      childList: true,\n"
        "      subtree: true\n"
        "    });\n"
        "  }\n"
        "})();"
    )


def sanitize_and_extract_markup(
    html_text: str,
    *,
    demo_kind: DemoKind | None = None,
) -> tuple[str, list[str], list[str]]:
    """Strip DOCTYPE, html, head, body tags and extract inline <style> and <script> blocks."""
    if contains_demo_banner(html_text):
        if demo_kind == "third_party_internal_demo":
            raise PermissionError("GHL export is blocked for third-party internal demo projects")
        if demo_kind != "fictional":
            raise PermissionError(
                "GHL export of a demo page requires its project demo classification"
            )
        if not has_demo_banner(html_text):
            raise ValueError(
                "GHL export is blocked because the fixture banner is missing or altered"
            )
    elif demo_kind is not None:
        raise ValueError("GHL export is blocked because the fixture banner is missing")

    soup = BeautifulSoup(html_text, "html.parser")

    for item in list(soup.contents):
        if isinstance(item, Doctype):
            item.extract()

    inline_styles: list[str] = []
    for style_tag in soup.find_all("style"):
        inline_styles.append(style_tag.get_text("\n"))
        style_tag.decompose()

    for link_tag in soup.find_all("link"):
        link_tag.decompose()

    inline_scripts: list[str] = []
    for script_tag in soup.find_all("script"):
        script_text = script_tag.get_text("\n").strip()
        if script_text:
            inline_scripts.append(script_text)
        script_tag.decompose()

    body = soup.find("body")
    if isinstance(body, Tag):
        inner_markup = "".join(str(child) for child in body.contents).strip()
    else:
        for wrapper_name in ("head", "html"):
            for tag in soup.find_all(wrapper_name):
                tag.unwrap()
        inner_markup = str(soup).strip()

    return inner_markup, inline_styles, inline_scripts


def export_ghl_snippet_from_html(
    html_text: str,
    *,
    business_id: str,
    demo_kind: DemoKind | None = None,
    extra_css: str = "",
    extra_js: str = "",
    cdn_base_url: str | None = None,
    asset_url_map: dict[str, str] | None = None,
) -> str:
    """Transform HTML into a CSS-scoped and IIFE-isolated GHL snippet."""
    if cdn_base_url or asset_url_map:
        html_text = rewrite_ghl_asset_urls(
            html_text,
            cdn_base_url=cdn_base_url,
            asset_url_map=asset_url_map,
        )
        if extra_css:
            extra_css = rewrite_ghl_asset_urls(
                extra_css,
                cdn_base_url=cdn_base_url,
                asset_url_map=asset_url_map,
            )

    root_id = f"ghl-ev-engine-root-{business_id}"
    root_selector = f"#{root_id}"

    inner_markup, inline_styles, inline_scripts = sanitize_and_extract_markup(
        html_text,
        demo_kind=demo_kind,
    )

    css_chunks: list[str] = [extra_css] if extra_css.strip() else []
    css_chunks.extend(inline_styles)
    combined_raw_css = "\n".join(c for c in css_chunks if c.strip())
    scoped_css = scope_css(combined_raw_css, root_selector) if combined_raw_css else ""
    unscoped = find_unscoped_selectors(scoped_css, root_selector)
    if unscoped:
        raise UnscopedCSSError(f"Unscoped CSS selectors detected: {unscoped}")

    js_chunks: list[str] = [extra_js] if extra_js.strip() else []
    js_chunks.extend(inline_scripts)
    combined_raw_js = "\n".join(j for j in js_chunks if j.strip())
    isolated_js = isolate_javascript(combined_raw_js, root_id)

    parts: list[str] = [f'<div id="{root_id}">']
    if scoped_css:
        parts.append(f"<style>\n{scoped_css}\n</style>")
    parts.append(inner_markup)
    parts.append(f"<script>\n{isolated_js}\n</script>")
    parts.append("</div>\n")
    return "\n".join(parts)


def export_ghl_snippet(
    project_folder: str | Path,
    *,
    business_id: str | None = None,
    cdn_base_url: str | None = None,
    asset_url_map: dict[str, str] | None = None,
) -> str:
    """Build a self-contained, CSS-scoped, IIFE-isolated GHL snippet from delivery/."""
    project_root = Path(project_folder).resolve()
    delivery_dir = project_root / "delivery"
    if not delivery_dir.is_dir():
        raise ValueError(f"delivery/ directory not found in {project_root}")

    demo = read_demo_metadata(project_root)
    if demo is not None and demo.is_fixture and demo.kind == "third_party_internal_demo":
        raise PermissionError("GHL export is blocked for third-party internal demo projects")
    freeze_demo_metadata(project_root)

    candidate_files = (
        delivery_dir / "approved-homepage.html",
        delivery_dir / "standalone.html",
    )
    source_path = next((p for p in candidate_files if p.is_file()), None)
    if source_path is None:
        raise ValueError(f"Approved source HTML not found in delivery/ for {project_root}")

    biz_id = business_id or project_root.name
    raw_html = source_path.read_text(encoding="utf-8")
    if demo is not None and demo.is_fixture and not has_demo_banner(raw_html):
        raise ValueError("GHL export is blocked because the fixture banner is missing or altered")
    if (demo is None or not demo.is_fixture) and contains_demo_banner(raw_html):
        raise ValueError("GHL export is blocked because fixture mode is missing from input.json")
    final_output = export_ghl_snippet_from_html(
        raw_html,
        business_id=biz_id,
        demo_kind=demo.kind if demo is not None and demo.is_fixture else None,
        cdn_base_url=cdn_base_url,
        asset_url_map=asset_url_map,
    )

    out_path = delivery_dir / "ghl-ready.html"
    _atomic_write_text(out_path, final_output)
    return final_output


def validate_ghl_embed_simulation(
    host_page_html: str,
    root_id: str,
) -> dict[str, Any]:
    """Validate that an embedded snippet inside a host page is isolated, scoped, and leak-free."""
    soup = BeautifulSoup(host_page_html, "html.parser")
    root_el = soup.find(id=root_id)
    root_element_found = isinstance(root_el, Tag)

    unscoped_selectors: list[str] = []
    global_leak_detected = False
    asset_urls_absolute = True

    if root_element_found and isinstance(root_el, Tag):
        root_selector = f"#{root_id}"
        # 1. Check style blocks inside root
        for style_tag in root_el.find_all("style"):
            css_text = style_tag.get_text()
            unscoped = find_unscoped_selectors(css_text, root_selector)
            unscoped_selectors.extend(unscoped)

        # 2. Check scripts inside root for global leakage
        for script_tag in root_el.find_all("script"):
            script_text = script_tag.get_text()
            if _GLOBAL_LEAK_RE.search(script_text):
                global_leak_detected = True

        # 3. Check asset URLs in root
        for media_tag in root_el.find_all(["img", "source", "video", "audio"]):
            src = media_tag.get("src")
            if src and not _is_absolute_url(str(src)):
                asset_urls_absolute = False
            srcset = media_tag.get("srcset")
            if srcset:
                for candidate in str(srcset).split(","):
                    cand_parts = candidate.strip().split()
                    if cand_parts and not _is_absolute_url(cand_parts[0]):
                        asset_urls_absolute = False

        for style_tag in root_el.find_all("style"):
            for match in _CSS_URL_PATTERN.finditer(style_tag.get_text()):
                url_val = match.group(2).strip()
                if url_val and not _is_absolute_url(url_val):
                    asset_urls_absolute = False

        for tag_with_style in root_el.find_all(style=True):
            style_attr = str(tag_with_style.get("style", ""))
            for match in _CSS_URL_PATTERN.finditer(style_attr):
                url_val = match.group(2).strip()
                if url_val and not _is_absolute_url(url_val):
                    asset_urls_absolute = False

    is_valid = (
        root_element_found
        and len(unscoped_selectors) == 0
        and not global_leak_detected
        and asset_urls_absolute
    )

    return {
        "is_valid": is_valid,
        "root_element_found": root_element_found,
        "unscoped_css_count": len(unscoped_selectors),
        "unscoped_selectors": unscoped_selectors,
        "global_leak_detected": global_leak_detected,
        "asset_urls_absolute": asset_urls_absolute,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export approved website into a scoped GoHighLevel custom code snippet."
    )
    parser.add_argument("--project", required=True, help="Path to project directory")
    parser.add_argument("--business-id", default=None, help="Optional explicit business ID")
    parser.add_argument(
        "--cdn-base-url", default=None, help="Base URL for remote CDN asset hosting"
    )
    args = parser.parse_args()
    export_ghl_snippet(
        args.project,
        business_id=args.business_id,
        cdn_base_url=args.cdn_base_url,
    )


if __name__ == "__main__":
    main()
