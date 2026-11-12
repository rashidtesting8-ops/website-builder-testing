"""Standalone HTML and GoHighLevel snippet exporter with mandatory asset rights release gate.

Enforces two non-negotiable release gates from AGENTS.md and DEVELOPER_BRIEF.md:
1. Every asset used in the page must exist in the project manifests with
   rights_confirmed_for_preview == True. If any used asset has
   rights_confirmed_for_preview=False (or is missing from the manifest), the
   exporter refuses to build delivery/approved-homepage.html or
   delivery/ghl-ready.html and returns the clear list of offending assets.
2. Approved releases under delivery/ are immutable and are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from engine.contracts import (
    DemoKind,
    contains_demo_banner,
    freeze_demo_metadata,
    has_demo_banner,
)

ASSET_REF_RE = re.compile(
    r"(?:\.\./|\./|/|^)(assets/[A-Za-z0-9._/-]+)"
)


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


def _normalize_asset_ref(raw_ref: str) -> str | None:
    cleaned = raw_ref.strip().strip("'\"()").split("?")[0].split("#")[0].replace("\\", "/")
    while cleaned.startswith("../") or cleaned.startswith("./") or cleaned.startswith("/"):
        if cleaned.startswith("../"):
            cleaned = cleaned[3:]
        elif cleaned.startswith("./"):
            cleaned = cleaned[2:]
        elif cleaned.startswith("/"):
            cleaned = cleaned[1:]
    if cleaned.startswith("assets/"):
        return cleaned
    return None


def extract_used_assets(html: str) -> list[str]:
    """Discover all local asset paths referenced in HTML attributes, srcset, or CSS."""
    used: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(True):
        for attr in ("src", "href", "poster", "data-src", "data-bg"):
            val = tag.get(attr)
            if isinstance(val, str):
                norm = _normalize_asset_ref(val)
                if norm:
                    used.add(norm)
        srcset = tag.get("srcset")
        if isinstance(srcset, str):
            for part in srcset.split(","):
                tokens = part.strip().split()
                if tokens:
                    norm = _normalize_asset_ref(tokens[0])
                    if norm:
                        used.add(norm)

    for match in ASSET_REF_RE.finditer(html):
        norm = _normalize_asset_ref(match.group(1))
        if norm:
            used.add(norm)

    return sorted(used)


def _load_manifest_index(project_root: Path) -> dict[str, dict[str, Any]]:
    """Index all known asset manifest rows by their relative file paths."""
    index: dict[str, dict[str, Any]] = {}
    manifest_paths = (
        project_root / "research" / "asset-manifest.json",
        project_root / "research" / "social-media-manifest.json",
        project_root / "delivery" / "asset-manifest.json",
    )
    for mpath in manifest_paths:
        if not mpath.is_file():
            continue
        try:
            rows = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("file", "local_path", "optimized_file"):
                val = row.get(key)
                if isinstance(val, str):
                    norm = _normalize_asset_ref(val)
                    if norm:
                        index[norm] = row
                        fname = Path(norm).name
                        index[f"assets/optimized/{fname}"] = row
    return index


def check_used_asset_rights(
    project_folder: str | Path,
    *,
    source_html_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return a list of any used assets that lack rights_confirmed_for_preview == True."""
    project_root = Path(project_folder).resolve()
    if source_html_path is not None:
        html_file = Path(source_html_path).resolve()
    elif (project_root / "delivery" / "standalone.html").is_file():
        html_file = project_root / "delivery" / "standalone.html"
    elif (project_root / "delivery" / "approved-homepage.html").is_file():
        html_file = project_root / "delivery" / "approved-homepage.html"
    elif (project_root / "release-candidate" / "standalone.html").is_file():
        html_file = project_root / "release-candidate" / "standalone.html"
    elif (project_root / "website" / "index.html").is_file():
        html_file = project_root / "website" / "index.html"
    else:
        raise ValueError(f"Source HTML file not found in {project_root}")

    if not html_file.is_file():
        raise ValueError(f"Source HTML file not found: {html_file}")

    html = html_file.read_text(encoding="utf-8")
    used_assets = extract_used_assets(html)
    manifest_index = _load_manifest_index(project_root)

    offending: list[dict[str, Any]] = []
    for rel_path in used_assets:
        record = manifest_index.get(rel_path)
        if record is None:
            offending.append(
                {
                    "file": rel_path,
                    "sha256": None,
                    "source_url": None,
                    "rights_confirmed_for_preview": False,
                    "reason": "missing_from_manifest",
                }
            )
            continue

        confirmed = record.get("rights_confirmed_for_preview") is True
        if not confirmed:
            offending.append(
                {
                    "file": rel_path,
                    "sha256": record.get("sha256"),
                    "source_url": (
                        record.get("source_url")
                        or record.get("original_post_url")
                        or record.get("source_post_url")
                        or record.get("url")
                    ),
                    "rights_confirmed_for_preview": False,
                    "reason": "rights_confirmed_for_preview is false",
                }
            )

    return offending


def _build_ghl_snippet(
    html: str,
    scope_id: str,
    *,
    demo_kind: DemoKind | None = None,
) -> str:
    """Convert approved standalone HTML into a scoped GoHighLevel snippet with no html/head/body."""
    from app.ghl_exporter import (
        UnscopedCSSError,
        find_unscoped_selectors,
        isolate_javascript,
        sanitize_and_extract_markup,
        scope_css,
    )

    root_selector = f"#{scope_id}"
    inner_markup, styles, scripts = sanitize_and_extract_markup(
        html,
        demo_kind=demo_kind,
    )

    parts: list[str] = [f'<div id="{scope_id}" class="ev-ghl-scope">']
    if styles:
        combined_css = "\n".join(s.strip() for s in styles if s.strip())
        if combined_css:
            scoped_css = scope_css(combined_css, root_selector)
            unscoped = find_unscoped_selectors(scoped_css, root_selector)
            if unscoped:
                raise UnscopedCSSError(f"Unscoped CSS selectors detected: {unscoped}")
            parts.append(f"<style>\n{scoped_css}\n</style>")
    parts.append(inner_markup)
    if scripts:
        combined_js = "\n".join(s.strip() for s in scripts if s.strip())
        if combined_js:
            isolated_js = isolate_javascript(combined_js, scope_id)
            parts.append(f"<script>\n{isolated_js}\n</script>")
    parts.append("</div>\n")
    return "\n".join(parts)


def export_release(
    project_folder: str | Path,
    *,
    source_html_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build delivery/approved-homepage.html and delivery/ghl-ready.html after rights gate."""
    project_root = Path(project_folder).resolve()
    if not project_root.is_dir():
        raise ValueError("Project folder does not exist")

    demo = freeze_demo_metadata(project_root)
    if demo is not None and demo.is_fixture and demo.kind == "third_party_internal_demo":
        return {
            "status": "blocked_demo_fixture",
            "approved_built": False,
            "ghl_built": False,
            "offending_assets": [],
            "reason": "Third-party internal demo projects cannot be exported",
        }

    delivery_dir = project_root / "delivery"
    if delivery_dir.is_dir() and (delivery_dir / "release-manifest.json").is_file():
        from app.approval import ReleaseVerificationError, verify_release
        try:
            verify_release(project_root)
        except (ReleaseVerificationError, Exception) as exc:
            return {
                "status": "blocked_verification_failure",
                "approved_built": False,
                "ghl_built": False,
                "offending_assets": [],
                "reason": f"Delivery verification failed: {exc}",
            }

    if not delivery_dir.is_dir() and source_html_path is None:
        return {
            "status": "blocked_unapproved",
            "approved_built": False,
            "ghl_built": False,
            "offending_assets": [],
            "reason": "Approved release not found under delivery/",
        }

    approved_out = delivery_dir / "approved-homepage.html"
    ghl_out = delivery_dir / "ghl-ready.html"

    # Non-negotiable rule: Approved releases under delivery/ are immutable.
    if approved_out.exists() or ghl_out.exists():
        return {
            "status": "blocked_immutable_release",
            "approved_built": False,
            "ghl_built": False,
            "offending_assets": [],
            "reason": "Approved release already exists under delivery/ and is immutable",
        }

    if source_html_path is not None:
        html_file = Path(source_html_path).resolve()
    elif (delivery_dir / "standalone.html").is_file():
        html_file = delivery_dir / "standalone.html"
    elif (delivery_dir / "approved-homepage.html").is_file():
        html_file = delivery_dir / "approved-homepage.html"
    else:
        return {
            "status": "blocked_unapproved",
            "approved_built": False,
            "ghl_built": False,
            "offending_assets": [],
            "reason": "Approved source HTML not found under delivery/",
        }

    if not html_file.is_file():
        raise ValueError(f"Source HTML file not found: {html_file}")

    html = html_file.read_text(encoding="utf-8")
    if demo is not None and demo.is_fixture:
        if not has_demo_banner(html):
            return {
                "status": "blocked_demo_banner",
                "approved_built": False,
                "ghl_built": False,
                "offending_assets": [],
                "reason": "Fixture HTML is missing its required internal-demo banner",
            }
    elif contains_demo_banner(html):
        return {
            "status": "blocked_demo_mode_mismatch",
            "approved_built": False,
            "ghl_built": False,
            "offending_assets": [],
            "reason": (
                "Fixture banner is present but input.json does not classify "
                "this project as a fixture"
            ),
        }

    used_assets = extract_used_assets(html)
    offending = check_used_asset_rights(project_root, source_html_path=html_file)

    if offending:
        return {
            "status": "blocked_unconfirmed_rights",
            "approved_built": False,
            "ghl_built": False,
            "offending_assets": offending,
            "used_assets": used_assets,
        }

    manifest_index = _load_manifest_index(project_root)
    selected_manifest = [manifest_index[p] for p in used_assets if p in manifest_index]

    html_sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    scope_id = f"ev-ghl-{html_sha[:10]}"
    ghl_html = _build_ghl_snippet(
        html,
        scope_id,
        demo_kind=demo.kind if demo is not None and demo.is_fixture else None,
    )
    ghl_sha = hashlib.sha256(ghl_html.encode("utf-8")).hexdigest()

    retained_payload = {
        "approved": True,
        "assets": used_assets,
        **(
            {"demo": True, "kind": demo.kind, "reason": demo.reason}
            if demo is not None and demo.is_fixture
            else {}
        ),
    }
    release_payload = {
        "released_at": datetime.now(UTC).isoformat(),
        "approved_homepage_sha256": html_sha,
        "ghl_ready_sha256": ghl_sha,
        "used_assets": used_assets,
        **(
            {"demo": True, "kind": demo.kind, "reason": demo.reason}
            if demo is not None and demo.is_fixture
            else {}
        ),
    }

    _atomic_write_text(approved_out, html)
    _atomic_write_text(ghl_out, ghl_html)
    _atomic_write_text(
        delivery_dir / "retained-assets.json",
        json.dumps(retained_payload, indent=2),
    )
    _atomic_write_text(
        delivery_dir / "asset-manifest.json",
        json.dumps(selected_manifest, indent=2),
    )
    _atomic_write_text(
        delivery_dir / "release-manifest.json",
        json.dumps(release_payload, indent=2),
    )

    return {
        "status": "exported",
        "demo": bool(demo is not None and demo.is_fixture),
        "approved_built": True,
        "ghl_built": True,
        "offending_assets": [],
        "used_assets": used_assets,
        "outputs": {
            "approved_html": "delivery/approved-homepage.html",
            "ghl_html": "delivery/ghl-ready.html",
            "retained_assets": "delivery/retained-assets.json",
            "asset_manifest": "delivery/asset-manifest.json",
            "release_manifest": "delivery/release-manifest.json",
        },
        "checksums": {
            "delivery/approved-homepage.html": html_sha,
            "delivery/ghl-ready.html": ghl_sha,
        },
    }


def run_cli(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description="Export approved standalone HTML and GHL snippet after verifying asset rights."
    )
    parser.add_argument("--project", required=True, help="Path to project directory")
    parser.add_argument("--source-html", default=None, help="Optional path to source HTML")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return export_release(args.project, source_html_path=args.source_html)


def main() -> None:
    print(json.dumps(run_cli(), indent=2))


if __name__ == "__main__":
    main()

