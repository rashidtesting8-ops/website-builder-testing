"""Semantic HTML5/CSS/JS Website Generator for EditVista Pet Grooming Website Builder Engine.

Reads `design/creative-brief.json`, `design/brand.json`, `research/business-facts.json`,
and `design/image-selection.json` to synthesize semantic HTML5 (`<header>`, `<nav>`,
`<main>`, `<section>`, `<footer>`), responsive CSS with brand custom properties and
`clamp()` typography, and accessible vanilla JS (mobile navigation drawer + optional
before/after photo slider) with zero external CDN dependencies.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from app.art_director import (
    FALLBACK_NAVY_HEX,
    FALLBACK_SOFT_SAGE_HEX,
    FALLBACK_WARM_AMBER_HEX,
    generate_art_direction,
)
from app.fact_extractor import canonicalize_business_name
from app.reviser import compute_section_hashes

MAX_SECTION_HTML_BYTES: int = 500_000
FORBIDDEN_URL_PREFIXES: tuple[str, ...] = ("http://", "https://", "//", "javascript:", "data:")


def validate_section_html(name: str, new_html: str) -> None:
    """Validate revised section HTML markup for security and structural integrity."""
    if not name or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid section name: {name!r}")
    encoded = new_html.encode("utf-8")
    if len(encoded) > MAX_SECTION_HTML_BYTES:
        raise ValueError(
            f"Section HTML size exceeds cap of {MAX_SECTION_HTML_BYTES} bytes"
        )

    # Check for forbidden script tags (via regex first for quick catch)
    if re.search(r"<\s*script\b", new_html, re.IGNORECASE):
        raise ValueError("Script tags are forbidden in section HTML")

    soup = BeautifulSoup(new_html, "html.parser")

    # Double check soup for script tags
    if soup.find("script"):
        raise ValueError("Script tags are forbidden in section HTML")

    # Validate data-section attribute is present and matches the section name
    elements_with_section = soup.find_all(attrs={"data-section": True})
    if not elements_with_section:
        raise ValueError(
            f"Missing required data-section attribute (expected data-section='{name}')"
        )
    if not any(str(el.get("data-section")).strip() == name for el in elements_with_section):
        found_attrs = [el.get("data-section") for el in elements_with_section]
        raise ValueError(
            f"data-section attribute must match section name '{name}' (found: {found_attrs})"
        )

    # Validate no inline event handlers (attributes starting with 'on')
    # and no external URLs or unsafe schemes in attributes
    for el in soup.find_all(True):
        for attr_name, attr_val in el.attrs.items():
            if attr_name.lower().startswith("on"):
                raise ValueError(
                    f"Inline event handlers forbidden: found '{attr_name}' handler on <{el.name}>"
                )
            vals = attr_val if isinstance(attr_val, list) else [attr_val]
            for v in vals:
                v_str = str(v).strip().lower()
                if (
                    any(v_str.startswith(p) for p in FORBIDDEN_URL_PREFIXES)
                    or "http://" in v_str
                    or "https://" in v_str
                ):
                    raise ValueError(
                        "External URLs and unsafe schemes are forbidden in section HTML: "
                        f"{attr_name}='{v}'"
                    )

    # Check style tags for external URLs or unsafe schemes
    for style_tag in soup.find_all("style"):
        style_text = style_tag.get_text().lower()
        if any(p in style_text for p in ("http://", "https://", "//", "javascript:", "data:")):
            raise ValueError("External URLs and unsafe schemes are forbidden in section style tags")


def rebuild_index_from_sections(
    website_dir: str | Path,
    title: str | None = None,
) -> str:
    """Rebuild website/index.html strictly from per-section files under website/sections/."""
    wdir = Path(website_dir).resolve()
    sections_dir = wdir / "sections"
    if not sections_dir.is_dir():
        raise ValueError(f"Sections directory not found at {sections_dir}")

    section_files = list(sections_dir.glob("*.html"))
    sections_map: dict[str, str] = {}
    for p in section_files:
        sections_map[p.stem] = p.read_text(encoding="utf-8").strip()

    if "header" not in sections_map or "footer" not in sections_map:
        raise ValueError("Missing header or footer section file in sections directory")

    if not title:
        index_path = wdir / "index.html"
        if index_path.is_file():
            m = re.search(r"<title>(.*?)</title>", index_path.read_text(encoding="utf-8"), re.I)
            if m:
                title = m.group(1).strip()
        if not title:
            parent = wdir.parent
            for rel in ("design/creative-brief.json", "design/brand.json"):
                cand_file = parent / rel
                if cand_file.is_file():
                    try:
                        data = json.loads(cand_file.read_text(encoding="utf-8"))
                        if data.get("business_name"):
                            title = str(data["business_name"]).strip()
                            break
                    except Exception:
                        pass
        if not title:
            title = "Pet Grooming Salon"

    esc_title = html.escape(title)

    canonical_main_order = ["hero", "services", "gallery", "before-after", "contact"]
    alias_to_section = {
        "hero": "hero",
        "hero_editorial_showcase": "hero",
        "hero_with_instant_call_cta": "hero",
        "services": "services",
        "services_and_pricing": "services",
        "segregated_dog_and_cat_pricing_tabs": "services",
        "gallery": "gallery",
        "grooming_gallery": "gallery",
        "before-after": "before-after",
        "contact": "contact",
        "location_hours_and_booking_cta": "contact",
    }

    preferred_order: list[str] = []
    brief_cand = wdir.parent / "design" / "creative-brief.json"
    if brief_cand.is_file():
        try:
            b_data = json.loads(brief_cand.read_text(encoding="utf-8"))
            raw_sug = b_data.get("suggested_sections")
            if isinstance(raw_sug, list):
                for item in raw_sug:
                    mapped = alias_to_section.get(str(item).strip(), str(item).strip())
                    if (
                        mapped in sections_map
                        and mapped not in ("header", "footer")
                        and mapped not in preferred_order
                    ):
                        preferred_order.append(mapped)
        except Exception:
            pass

    for name in canonical_main_order:
        if name in sections_map and name not in preferred_order:
            preferred_order.append(name)

    other_sections = sorted(
        [
            k
            for k in sections_map.keys()
            if k not in preferred_order and k not in ("header", "footer")
        ]
    )
    all_main_names = preferred_order + other_sections
    main_body = "\n    ".join(sections_map[k] for k in all_main_names)

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc_title}</title>
  <link rel="stylesheet" href="css/styles.css">
</head>
<body>
  {sections_map['header']}
  <main id="top" class="ev-main">
    {main_body}
  </main>
  {sections_map['footer']}
  <script src="js/main.js"></script>
</body>
</html>
"""
    index_path = wdir / "index.html"
    _atomic_write_text(index_path, html_doc)
    return html_doc


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


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        return (27, 54, 93)
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    chans: list[float] = []
    for val in rgb:
        c = max(0, min(255, val)) / 255.0
        chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def _contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _ensure_wcag_aa_on_white(hex_color: str) -> str:
    """Ensure color has >= 4.5:1 contrast ratio against light editorial surfaces."""
    rgb = _hex_to_rgb(hex_color)
    if (
        _contrast_ratio(rgb, (255, 255, 255)) >= 4.7
        and _contrast_ratio(rgb, (243, 239, 234)) >= 4.6
    ):
        return hex_color.upper()
    return FALLBACK_NAVY_HEX


class GeneratedWebsiteBundle(BaseModel):
    """Summary of generated website files under `projects/<business-id>/website/`."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    index_html_path: Path
    css_path: Path
    js_path: Path
    pricing_needs_review: bool
    has_before_after_slider: bool
    section_hashes_path: Path | None = None
    section_files: dict[str, Path] = Field(default_factory=dict)
    section_css_files: dict[str, Path] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=_utc_now)


def _find_before_after_pair(project_root: Path) -> tuple[str, str] | None:
    """Inspect optimized or website assets for an authentic before/after image pair."""
    before_rel: str | None = None
    after_rel: str | None = None

    for sub in ("assets/optimized", "assets/website"):
        folder = project_root / sub
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if not f.is_file():
                continue
            lower = f.name.lower()
            rel = f"../{sub}/{f.name}"
            if "before" in lower and before_rel is None:
                before_rel = rel
            elif "after" in lower and after_rel is None:
                after_rel = rel

    if before_rel and after_rel and before_rel != after_rel:
        return before_rel, after_rel
    return None


def _build_base_css(primary_hex: str, secondary_hex: str, accent_hex: str) -> str:
    safe_primary = _ensure_wcag_aa_on_white(primary_hex)
    return f""":root {{
  --ev-primary: {safe_primary};
  --ev-secondary: {secondary_hex};
  --ev-accent: {accent_hex};
  --ev-surface: #FFFFFF;
  --ev-surface-alt: #F3EFEA;
  --ev-surface-dark: #0F1E36;
  --ev-bg: #FAF8F5;
  --ev-text: #0F172A;
  --ev-muted: #334155;
  --ev-border: #E2DCD3;
  --ev-font-display: "Playfair Display", "Cormorant Garamond", "Instrument Serif", Georgia, serif;
  --ev-font-sans: "Plus Jakarta Sans", "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --ev-radius-lg: 20px;
  --ev-radius-md: 12px;
  --ev-shadow-soft: 0 18px 42px -12px rgba(15, 23, 42, 0.08);
}}
* {{
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}}
body {{
  font-family: var(--ev-font-sans);
  background-color: var(--ev-bg);
  color: var(--ev-text);
  line-height: 1.65;
  overflow-x: hidden;
  -webkit-font-smoothing: antialiased;
}}
h1, h2, h3, .ev-serif {{
  font-family: var(--ev-font-display);
  letter-spacing: -0.015em;
}}
img {{
  max-width: 100%;
  height: auto;
  display: block;
}}
.ev-eyebrow {{
  display: inline-block;
  font-family: var(--ev-font-sans);
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--ev-primary);
  background-color: var(--ev-surface-alt);
  border: 1px solid var(--ev-border);
  padding: 0.35rem 0.85rem;
  border-radius: 999px;
  margin-bottom: 1rem;
}}
.cta, .btn {{
  display: inline-block;
  background-color: var(--ev-primary);
  color: #FFFFFF;
  text-decoration: none;
  font-family: var(--ev-font-sans);
  font-weight: 700;
  font-size: 0.95rem;
  letter-spacing: 0.03em;
  padding: 0.9rem 1.65rem;
  border-radius: 999px;
  border: none;
  cursor: pointer;
  box-shadow: 0 8px 20px -6px rgba(15, 23, 42, 0.25);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}}
.cta:hover, .btn:hover {{
  transform: translateY(-1px);
  box-shadow: 0 12px 24px -6px rgba(15, 23, 42, 0.32);
}}
.ev-btn-secondary {{
  display: inline-block;
  background-color: var(--ev-surface);
  color: var(--ev-primary);
  text-decoration: none;
  font-weight: 700;
  font-size: 0.95rem;
  padding: 0.85rem 1.5rem;
  border-radius: 999px;
  border: 1.5px solid var(--ev-primary);
}}
.ev-main {{
  max-width: 1240px;
  margin: 0 auto;
  padding: 2rem 1.25rem 4rem;
}}
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }}
}}
"""


def _build_header_css() -> str:
    return """.ev-header {
  background-color: var(--ev-surface);
  border-bottom: 1px solid var(--ev-border);
  padding: 1.1rem 1.25rem;
}
.ev-nav {
  max-width: 1240px;
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 1.25rem;
}
.ev-brand-wrap {
  display: inline-flex;
  align-items: center;
  gap: 0.85rem;
  text-decoration: none;
}
.ev-brand-logo {
  width: 44px;
  height: 44px;
  object-fit: cover;
  border-radius: 50%;
  border: 2px solid var(--ev-border);
}
.ev-brand-title {
  font-family: var(--ev-font-display);
  font-size: clamp(1.25rem, 2.2vw, 1.65rem);
  font-weight: 700;
  color: var(--ev-primary);
  text-decoration: none;
  letter-spacing: -0.01em;
}
.ev-nav-toggle {
  background-color: var(--ev-primary);
  color: #FFFFFF;
  border: none;
  border-radius: 999px;
  padding: 0.55rem 1rem;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
}
.ev-nav-drawer {
  display: flex;
  flex-wrap: wrap;
  gap: 1.5rem;
  list-style: none;
  align-items: center;
}
.ev-nav-drawer[data-collapsed="true"] {
  display: none;
}
.ev-nav-link {
  color: var(--ev-primary);
  text-decoration: none;
  font-weight: 600;
  font-size: 0.95rem;
  letter-spacing: 0.02em;
}
"""


def _build_hero_css() -> str:
    return """.ev-hero {
  background-color: var(--ev-surface);
  border: 1px solid var(--ev-border);
  border-radius: var(--ev-radius-lg);
  padding: clamp(1.75rem, 4vw, 3.5rem);
  margin-bottom: 2.75rem;
  box-shadow: var(--ev-shadow-soft);
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
  gap: clamp(1.5rem, 3.5vw, 3rem);
  align-items: center;
}
.ev-hero-content {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.ev-hero-title {
  font-family: var(--ev-font-display);
  font-size: clamp(2rem, 4vw, 3.25rem);
  color: var(--ev-primary);
  line-height: 1.12;
  margin-bottom: 1rem;
}
.ev-hero-subtitle {
  font-size: clamp(1.05rem, 1.5vw, 1.2rem);
  color: var(--ev-text);
  margin-bottom: 0.85rem;
  max-width: 58ch;
}
.ev-hero-value {
  font-size: 0.96rem;
  color: var(--ev-muted);
  border-left: 3px solid var(--ev-secondary);
  padding-left: 0.9rem;
  margin-bottom: 1.5rem;
  max-width: 54ch;
}
.ev-hero-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.85rem;
  align-items: center;
  margin-bottom: 1.5rem;
}
.ev-trust-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  width: 100%;
  padding-top: 1.1rem;
  border-top: 1px solid var(--ev-border);
}
.ev-trust-pill {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--ev-primary);
  background-color: var(--ev-surface-alt);
  padding: 0.35rem 0.75rem;
  border-radius: 8px;
}
.ev-hero-media {
  width: 100%;
  border-radius: 16px;
  overflow: hidden;
  aspect-ratio: 4 / 3;
  background-color: var(--ev-surface-alt);
  border: 1px solid var(--ev-border);
  box-shadow: 0 14px 32px -10px rgba(15, 23, 42, 0.14);
}
.ev-hero-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
"""


def _build_services_css() -> str:
    return """.ev-section {
  background-color: var(--ev-surface);
  border: 1px solid var(--ev-border);
  border-radius: var(--ev-radius-lg);
  padding: clamp(1.75rem, 3.5vw, 3rem);
  margin-bottom: 2.5rem;
  box-shadow: var(--ev-shadow-soft);
}
.ev-section-header {
  margin-bottom: 1.75rem;
  max-width: 65ch;
}
.ev-section-title {
  font-family: var(--ev-font-display);
  font-size: clamp(1.6rem, 3vw, 2.35rem);
  color: var(--ev-primary);
  margin-bottom: 0.5rem;
  line-height: 1.2;
}
.ev-section-lead {
  color: var(--ev-muted);
  font-size: 1.02rem;
}
.ev-menu-list {
  display: flex;
  flex-direction: column;
  gap: 1.15rem;
  margin-bottom: 1.5rem;
}
.ev-menu-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.65rem;
  padding: 1.1rem 1.25rem;
  background-color: var(--ev-bg);
  border: 1px solid var(--ev-border);
  border-radius: var(--ev-radius-md);
}
.ev-menu-leader {
  flex: 1 1 60px;
  border-bottom: 1px dashed var(--ev-border);
  margin: 0 0.75rem;
  min-width: 24px;
}
.ev-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr));
  gap: 1.35rem;
}
.ev-card {
  background-color: var(--ev-bg);
  border: 1px solid var(--ev-border);
  border-radius: var(--ev-radius-md);
  padding: 1.35rem;
}
.ev-card-title {
  font-family: var(--ev-font-display);
  font-size: 1.2rem;
  color: var(--ev-primary);
  margin-bottom: 0.45rem;
}
.ev-price-badge {
  display: inline-block;
  background-color: var(--ev-primary);
  color: #FFFFFF;
  font-weight: 700;
  font-size: 0.95rem;
  padding: 0.3rem 0.85rem;
  border-radius: 999px;
}
.ev-van-showcase {
  background-color: var(--ev-surface-dark);
  color: #FFFFFF;
  border-radius: var(--ev-radius-md);
  padding: clamp(1.35rem, 2.5vw, 2rem);
  margin-bottom: 1.75rem;
}
.ev-van-showcase h3 {
  color: #FFFFFF;
  font-size: 1.35rem;
  margin-bottom: 0.6rem;
}
.ev-van-showcase p {
  color: #F1F5F9;
  margin-bottom: 1rem;
}
.ev-van-features {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr));
  gap: 0.85rem;
  list-style: none;
}
.ev-van-features li {
  background-color: #1B365D;
  color: #FFFFFF;
  padding: 0.75rem 1rem;
  border-radius: 8px;
  font-weight: 600;
  font-size: 0.9rem;
}
.ev-species-showcase {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
  gap: 1.25rem;
  margin-bottom: 1.75rem;
}
.ev-species-card {
  background-color: var(--ev-surface-alt);
  border: 1px solid var(--ev-border);
  border-radius: var(--ev-radius-md);
  padding: 1.35rem;
}
.ev-species-badge {
  display: inline-block;
  background-color: var(--ev-primary);
  color: #FFFFFF;
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 0.25rem 0.7rem;
  border-radius: 999px;
  margin-bottom: 0.65rem;
}
"""


def _build_gallery_css() -> str:
    return """.ev-bento-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
  gap: 1.25rem;
}
.ev-bento-item {
  position: relative;
  border-radius: var(--ev-radius-md);
  overflow: hidden;
  border: 1px solid var(--ev-border);
  background-color: var(--ev-surface-alt);
}
.ev-gallery-img {
  width: 100%;
  aspect-ratio: 4 / 3;
  object-fit: cover;
  border-radius: var(--ev-radius-md);
}
.ev-bento-caption {
  padding: 0.75rem 1rem;
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--ev-primary);
  background-color: var(--ev-surface);
  border-top: 1px solid var(--ev-border);
}
"""


def _build_before_after_css() -> str:
    return """.ev-ba-slider {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr));
  gap: 1.25rem;
  align-items: center;
}
"""


def _build_contact_css() -> str:
    return """.ev-contact-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
  gap: 1.25rem;
}
"""


def _build_footer_css() -> str:
    return """.ev-footer {
  background-color: var(--ev-surface-dark);
  border-top: 3px solid var(--ev-secondary);
  padding: 2.5rem 1.25rem;
  text-align: center;
  color: #F8FAFC;
}
.ev-footer-inner {
  max-width: 1240px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.65rem;
}
.ev-footer-brand {
  font-family: var(--ev-font-display);
  font-size: 1.35rem;
  font-weight: 700;
  color: #FFFFFF;
}
"""


def _build_section_css_map(
    primary_hex: str, secondary_hex: str, accent_hex: str
) -> dict[str, str]:
    return {
        "base": _build_base_css(primary_hex, secondary_hex, accent_hex),
        "header": _build_header_css(),
        "hero": _build_hero_css(),
        "services": _build_services_css(),
        "gallery": _build_gallery_css(),
        "before-after": _build_before_after_css(),
        "contact": _build_contact_css(),
        "footer": _build_footer_css(),
    }


def _build_css(primary_hex: str, secondary_hex: str, accent_hex: str) -> str:
    css_map = _build_section_css_map(primary_hex, secondary_hex, accent_hex)
    parts: list[str] = []
    for name, css_text in css_map.items():
        parts.append(f"/* section: {name} */\n{css_text.strip()}\n/* endsection: {name} */")
    return "\n\n".join(parts) + "\n"


def _build_js(has_before_after: bool) -> str:
    slider_logic = ""
    if has_before_after:
        slider_logic = """
    var rangeInput = root.querySelector('[data-ev-ba-range]');
    var afterWrap = root.querySelector('[data-ev-ba-after]');
    if (rangeInput && afterWrap) {
      rangeInput.addEventListener('input', function(evt) {
        var target = evt.target;
        if (target && typeof target.value === 'string') {
          afterWrap.setAttribute('data-split', target.value);
        }
      });
    }"""

    return f"""var scopes = document.querySelectorAll('.ev-ghl-scope');
var roots = scopes.length > 0 ? Array.prototype.slice.call(scopes) : [document];
roots.forEach(function(root) {{
  var toggleBtn = root.querySelector('[data-ev-nav-toggle]');
  var navDrawer = root.querySelector('[data-ev-nav-drawer]');
  if (toggleBtn && navDrawer) {{
    toggleBtn.addEventListener('click', function() {{
      var expanded = toggleBtn.getAttribute('aria-expanded') === 'true';
      var nextState = !expanded;
      toggleBtn.setAttribute('aria-expanded', nextState ? 'true' : 'false');
      navDrawer.setAttribute('data-collapsed', nextState ? 'false' : 'true');
    }});
  }}{slider_logic}
}});
"""


def generate_website(project_folder: str | Path) -> GeneratedWebsiteBundle:
    """Synthesize semantic HTML5, responsive CSS, and isolated vanilla JS in website/."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name

    brand_path = project_root / "design" / "brand.json"
    brief_path = project_root / "design" / "creative-brief.json"
    if not brand_path.is_file() or not brief_path.is_file():
        generate_art_direction(project_root)

    brand_data: dict[str, Any] = {}
    if brand_path.is_file():
        brand_data = json.loads(brand_path.read_text(encoding="utf-8"))

    brief_data: dict[str, Any] = {}
    if brief_path.is_file():
        brief_data = json.loads(brief_path.read_text(encoding="utf-8"))

    facts_path = project_root / "research" / "business-facts.json"
    facts_list: list[dict[str, Any]] = []
    if facts_path.is_file():
        facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
        facts_list = list(facts_payload.get("facts", []))

    selection_path = project_root / "design" / "image-selection.json"
    selection_data: dict[str, Any] = {}
    if selection_path.is_file():
        selection_data = json.loads(selection_path.read_text(encoding="utf-8"))

    primary_hex = str(brand_data.get("primary_color", FALLBACK_NAVY_HEX))
    secondary_hex = str(brand_data.get("secondary_color", FALLBACK_WARM_AMBER_HEX))
    accent_hex = str(brand_data.get("accent_color", FALLBACK_SOFT_SAGE_HEX))
    configured_name = str(
        brief_data.get("business_name") or brand_data.get("business_name") or project_id
    )
    business_name_facts = [
        fact for fact in facts_list if str(fact.get("category", "")) == "business_name"
    ]
    verified_names = {
        canonicalize_business_name(str(fact.get("claim_value", "")))
        for fact in business_name_facts
        if bool(fact.get("is_verified", fact.get("verified", False)))
        and str(fact.get("status", "")) == "verified"
        and canonicalize_business_name(str(fact.get("claim_value", "")))
    }
    if len(verified_names) == 1:
        business_name = verified_names.pop()
    elif business_name_facts:
        business_name = "Pet Grooming Salon"
    else:
        business_name = canonicalize_business_name(configured_name) or "Pet Grooming Salon"

    # Extract strictly verified facts
    phone: str | None = None
    address: str | None = None
    hours: str | None = None
    policy: str | None = None
    verified_prices: list[tuple[str, str]] = []
    seen_verified_prices: set[tuple[str, str]] = set()
    verified_services: list[str] = []
    has_unverified_pricing = False

    for fact in facts_list:
        category = str(fact.get("category", ""))
        claim_key = str(fact.get("claim_key", ""))
        claim_val = str(fact.get("claim_value", "")).strip()
        is_verified = bool(fact.get("is_verified", False))
        status = str(fact.get("status", "needs_review"))

        if category == "pricing":
            if is_verified and status == "verified":
                if " - " in claim_val:
                    svc_name, price_str = claim_val.split(" - ", 1)
                elif ": " in claim_val:
                    svc_name, price_str = claim_val.split(": ", 1)
                else:
                    svc_name = claim_key.removeprefix("price_").replace("_", " ").title()
                    price_str = claim_val
                price_pair = (svc_name.strip(), price_str.strip())
                normalized_price_pair = (
                    price_pair[0].casefold(),
                    price_pair[1].casefold(),
                )
                if normalized_price_pair not in seen_verified_prices:
                    verified_prices.append(price_pair)
                    seen_verified_prices.add(normalized_price_pair)
            else:
                has_unverified_pricing = True
            continue

        if not is_verified or status != "verified":
            continue

        if category == "phone" and phone is None:
            phone = claim_val
        elif category == "address" and address is None:
            address = claim_val
        elif category == "hours" and hours is None:
            hours = claim_val
        elif category in ("cancellation_policy", "policy") and policy is None:
            policy = claim_val
        elif (
            category in ("services", "canine_services", "feline_services")
            and claim_val not in verified_services
        ):
            verified_services.append(claim_val)

    priced_service_names = {label.casefold() for label, _ in verified_prices}
    verified_services = [
        service for service in verified_services if service.casefold() not in priced_service_names
    ]
    pricing_needs_review = has_unverified_pricing or not verified_prices

    # Resolve hero and gallery image paths relative to website/index.html
    def _resolve_asset_rel(entry: dict[str, Any]) -> str | None:
        rel_val = entry.get("relative_path")
        if isinstance(rel_val, str) and rel_val.strip():
            cand = project_root / rel_val.strip()
            if cand.is_file():
                return f"../{rel_val.strip().lstrip('/')}"
        fn_val = entry.get("filename")
        if isinstance(fn_val, str) and fn_val.strip():
            for sub in ("assets/optimized", "assets/website"):
                cand = project_root / sub / fn_val.strip()
                if cand.is_file():
                    return f"../{sub}/{fn_val.strip()}"
        return None

    hero_obj = selection_data.get("hero_image")
    hero_src: str | None = None
    hero_obj_pos = "50% 30%"
    if isinstance(hero_obj, dict):
        hero_src = _resolve_asset_rel(hero_obj)
        if hero_src:
            hero_obj_pos = str(hero_obj.get("object_position", "50% 30%"))

    gallery_items: list[tuple[str, str]] = []
    for item in selection_data.get("grooming_gallery", []):
        if isinstance(item, dict):
            resolved_rel = _resolve_asset_rel(item)
            if resolved_rel:
                gallery_items.append(
                    (resolved_rel, str(item.get("object_position", "50% 30%")))
                )

    selected_images = [
        item
        for item in [hero_obj, *selection_data.get("grooming_gallery", [])]
        if isinstance(item, dict) and _resolve_asset_rel(item)
    ]
    manifest_path = project_root / "research" / "asset-manifest.json"
    manifest_payload: Any = []
    if manifest_path.is_file():
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(manifest_payload, dict):
        manifest_entries = manifest_payload.get("assets", [])
    else:
        manifest_entries = manifest_payload

    def _is_unconfirmed_image(image: dict[str, Any]) -> bool:
        filename = str(image.get("filename", "")).casefold()
        sha256 = str(image.get("sha256", "")).casefold()
        matching_entries = [
            entry
            for entry in manifest_entries
            if isinstance(entry, dict)
            and (
                (
                    filename
                    and Path(str(entry.get("file", ""))).name.casefold() == filename
                )
                or (sha256 and str(entry.get("sha256", "")).casefold() == sha256)
            )
        ]
        return not matching_entries or not all(
            entry.get("rights_confirmed_for_preview") is True
            for entry in matching_entries
        )

    rights_notice_html = ""
    if any(_is_unconfirmed_image(image) for image in selected_images):
        rights_notice_html = (
            '<p class="ev-asset-rights-note" role="note">'
            "Local preview only. Photo reuse rights have not been confirmed; "
            "do not approve or publish."
            "</p>"
        )

    before_after = _find_before_after_pair(project_root)

    esc_name = html.escape(business_name)
    digits = "".join(c for c in phone if c.isdigit() or c == "+") if phone else ""
    cta_href = f"tel:{digits}" if digits else "#contact"
    cta_text = f"Call {html.escape(phone)}" if phone else "Book Grooming Appointment"

    # Extract taxonomy & editorial copy from creative-brief.json
    raw_tax = brief_data.get("taxonomy")
    tax_obj: dict[str, Any] = raw_tax if isinstance(raw_tax, dict) else {}
    is_mobile = bool(tax_obj.get("is_mobile_groomer", False))
    serves_dogs = bool(tax_obj.get("serves_dogs", True))
    serves_cats = bool(tax_obj.get("serves_cats", False))

    if is_mobile:
        archetype = "mobile"
        eyebrow_text = "Mobile Grooming"
        trust_pills = ""
    elif serves_dogs and serves_cats:
        archetype = "multi-species"
        eyebrow_text = "Dog &amp; Cat Grooming"
        trust_pills = ""
    else:
        archetype = "salon"
        eyebrow_text = "Pet Grooming"
        trust_pills = ""

    if is_mobile:
        default_headline = f"{business_name} — Mobile Grooming"
        default_tagline = "Mobile grooming services for pets."
    elif serves_dogs and serves_cats:
        default_headline = f"{business_name} — Dog and Cat Grooming"
        default_tagline = "Grooming services for dogs and cats."
    elif serves_cats:
        default_headline = f"{business_name} — Cat Grooming"
        default_tagline = "Grooming services for cats."
    elif serves_dogs:
        default_headline = f"{business_name} — Dog Grooming"
        default_tagline = "Grooming services for dogs."
    else:
        default_headline = f"{business_name} — Pet Grooming"
        default_tagline = "Pet grooming services."

    raw_headline = str(brief_data.get("editorial_headline") or "").strip()
    name_match = re.search(re.escape(business_name), raw_headline, re.I)
    if name_match:
        prefix = raw_headline[: name_match.start()]
        if prefix and canonicalize_business_name(prefix):
            raw_headline = default_headline
        else:
            raw_headline = raw_headline[name_match.start() :].strip(" \t-–—:|")
    else:
        raw_headline = default_headline

    raw_tagline = str(brief_data.get("editorial_tagline") or "").strip()
    if (
        not raw_tagline
        or not any(k in raw_tagline.lower() for k in ("pet grooming", "grooming services"))
    ):
        raw_tagline = default_tagline

    raw_value_prop = str(brief_data.get("editorial_value_prop") or "").strip()
    if (
        not raw_value_prop
        or raw_value_prop.casefold() == raw_tagline.casefold()
        or any(
            bw in raw_value_prop.lower()
            for bw in (
                "certified",
                "master",
                "loving",
                "patient",
                "luxury",
                "bespoke",
                "organic",
                "botanical",
                "cage-free",
                "award-winning",
                "spa suites",
                "quiet hours",
                "hydro-massage",
                "zero kennel",
            )
        )
    ):
        raw_value_prop = ""

    esc_headline = html.escape(raw_headline)
    esc_tagline = html.escape(raw_tagline)
    esc_value_prop = html.escape(raw_value_prop) if raw_value_prop else ""

    # Resolve verified brand logo if present on disk
    logo_rel_raw = brand_data.get("logo_path")
    logo_img_html = ""
    if isinstance(logo_rel_raw, str) and logo_rel_raw.strip():
        cand_logo = project_root / logo_rel_raw.strip()
        if cand_logo.is_file():
            logo_src = f"../{logo_rel_raw.strip().lstrip('/')}"
            logo_img_html = (
                f'<img class="ev-brand-logo" src="{html.escape(logo_src)}" '
                f'alt="{esc_name} Logo">'
            )

    hero_media_html = ""
    if hero_src:
        hero_media_html = (
            '<div class="ev-hero-media">'
            f'<img class="ev-hero-img" src="{html.escape(hero_src)}" '
            f'alt="{esc_name} photo 1" '
            f'style="object-position: {html.escape(hero_obj_pos)};">'
            "</div>"
        )

    # Archetype-specific feature banner inside Services section
    archetype_showcase_html = ""
    if archetype == "mobile":
        archetype_showcase_html = (
            '<div class="ev-van-showcase">\n'
            '  <h3 class="ev-card-title">Mobile Grooming — Driveway Service</h3>\n'
            '  <p>Mobile pet grooming services.</p>\n'
            '</div>\n'
        )
    elif archetype == "multi-species":
        archetype_showcase_html = (
            '<div class="ev-species-showcase">\n'
            '  <div class="ev-species-card" data-species-tab="dogs">\n'
            '    <span class="ev-species-badge">Canine</span>\n'
            '    <h3 class="ev-card-title">Dog Grooming</h3>\n'
            '    <p>Grooming services for dogs.</p>\n'
            '  </div>\n'
            '  <div class="ev-species-card" data-species-tab="cats">\n'
            '    <span class="ev-species-badge">Feline</span>\n'
            '    <h3 class="ev-card-title">Cat Grooming</h3>\n'
            '    <p>Grooming services for cats.</p>\n'
            '  </div>\n'
            '</div>\n'
        )

    # Build Services & Pricing section (Luxury Editorial Spa Menu)
    if verified_prices:
        menu_rows = "".join(
            f'<div class="ev-menu-item ev-card">'
            f'<h3 class="ev-card-title">{html.escape(label)}</h3>'
            f'<span class="ev-menu-leader" aria-hidden="true"></span>'
            f'<span class="ev-price-badge">{html.escape(price)}</span>'
            f"</div>"
            for label, price in verified_prices
        )
        svc_rows = "".join(
            f'<div class="ev-card"><h3 class="ev-card-title">{html.escape(svc)}</h3></div>'
            for svc in verified_services
        )
        pricing_section_html = (
            f"{archetype_showcase_html}"
            f'<div class="ev-menu-list">{menu_rows}</div>'
            f"{f'<div class=\"ev-grid\">{svc_rows}</div>' if svc_rows else ''}"
        )
    else:
        svc_cards = "".join(
            f'<div class="ev-menu-item ev-card">'
            f'<h3 class="ev-card-title">{html.escape(svc)}</h3>'
            f'<span class="ev-menu-leader" aria-hidden="true"></span>'
            f'<span class="ev-price-badge">Custom Quote</span>'
            f"</div>"
            for svc in verified_services
        )
        pricing_section_html = (
            f"{archetype_showcase_html}"
            f"{f'<div class=\"ev-menu-list\">{svc_cards}</div>' if svc_cards else ''}"
            '<div class="ev-card" style="margin-top: 1rem;">'
            '<h3 class="ev-card-title">Custom Breed &amp; Coat Pricing</h3>'
            "<p>Contact our grooming team directly to receive a quote for your pet.</p>"
            f'<p style="margin-top: 0.85rem;"><a class="cta btn" href="{html.escape(cta_href)}">'
            "Inquire for Pricing / Request Custom Quote</a></p>"
            "</div>"
        )

    # Build Gallery section (Editorial Bento Portfolio)
    gallery_html = ""
    if gallery_items:
        imgs_markup = "".join(
            f'<figure class="ev-bento-item'
            f'{" ev-bento-item--featured" if idx == 1 else ""} ev-card">'
            f'<img class="ev-gallery-img" src="{html.escape(src)}" '
            f'alt="{esc_name} photo {idx}" loading="lazy" '
            f'style="object-position: {html.escape(pos)};">'
            f'<figcaption class="ev-bento-caption">{esc_name}</figcaption>'
            f"</figure>"
            for idx, (src, pos) in enumerate(gallery_items, start=1)
        )
        gallery_html = (
            '<section id="gallery" class="ev-section" data-section="gallery">\n'
            '  <div class="ev-section-header">\n'
            '    <span class="ev-eyebrow">Gallery</span>\n'
            '    <h2 class="ev-section-title">Grooming Gallery</h2>\n'
            '  </div>\n'
            f'  <div class="ev-bento-grid">{imgs_markup}</div>\n'
            "</section>"
        )

    # Optional Before/After interactive slider section
    ba_html = ""
    if before_after is not None:
        b_src, a_src = before_after
        ba_html = (
            '<section id="before-after" class="ev-section" data-section="before-after">\n'
            '  <div class="ev-section-header">\n'
            '    <span class="ev-eyebrow">Coat Transformation</span>\n'
            '    <h2 class="ev-section-title">Before &amp; After Transformation</h2>\n'
            '  </div>\n'
            '  <div class="ev-ba-slider" data-ev-ba-slider="true">\n'
            '    <div class="ev-card">\n'
            '      <h3 class="ev-card-title">Before Groom</h3>\n'
            f'      <img class="ev-gallery-img" src="{html.escape(b_src)}" alt="Before Grooming">\n'
            '    </div>\n'
            '    <div class="ev-card" data-ev-ba-after="true" data-split="50">\n'
            '      <h3 class="ev-card-title">After Groom</h3>\n'
            f'      <img class="ev-gallery-img" src="{html.escape(a_src)}" alt="After Grooming">\n'
            '    </div>\n'
            '  </div>\n'
            '  <div style="margin-top: 1rem;">\n'
            '    <label for="ev-ba-range">Compare Transformation: </label>\n'
            '    <input id="ev-ba-range" type="range" min="0" max="100" '
            'value="50" data-ev-ba-range="true">\n'
            '  </div>\n'
            "</section>"
        )

    # Contact, Hours & Policy details
    contact_cards: list[str] = []
    if phone:
        contact_cards.append(
            f'<div class="ev-card"><h3 class="ev-card-title">Phone</h3>'
            f"<p>{html.escape(phone)}</p></div>"
        )
    if address:
        contact_cards.append(
            f'<div class="ev-card"><h3 class="ev-card-title">Address</h3>'
            f"<p>{html.escape(address)}</p></div>"
        )
    if hours:
        contact_cards.append(
            f'<div class="ev-card"><h3 class="ev-card-title">Operating Hours</h3>'
            f"<p>{html.escape(hours)}</p></div>"
        )
    if policy:
        contact_cards.append(
            f'<div class="ev-card"><h3 class="ev-card-title">Appointment Policy</h3>'
            f"<p>{html.escape(policy)}</p></div>"
        )
    if not contact_cards:
        contact_cards.append(
            f'<div class="ev-card"><h3 class="ev-card-title">Contact {esc_name}</h3>'
            "<p>Reach out to schedule your next grooming appointment.</p></div>"
        )

    contact_html = (
        '<section id="contact" class="ev-section" data-section="contact">\n'
        '  <div class="ev-section-header">\n'
        '    <span class="ev-eyebrow">Contact</span>\n'
        '    <h2 class="ev-section-title">Location, Hours &amp; Policies</h2>\n'
        '  </div>\n'
        '  <div class="ev-grid">\n'
        f'    {"".join(contact_cards)}\n'
        '  </div>\n'
        "</section>"
    )

    header_html = (
        '<header class="ev-header" data-section="header">\n'
        '  <nav class="ev-nav" aria-label="Primary Navigation">\n'
        '    <div class="ev-brand-wrap">\n'
        f'      {logo_img_html}\n'
        f'      <a class="ev-brand-title" href="#top">{esc_name}</a>\n'
        '    </div>\n'
        '    <button class="ev-nav-toggle" type="button" aria-expanded="true"\n'
        '      aria-controls="ev-nav-drawer" data-ev-nav-toggle="true">Menu</button>\n'
        '    <ul id="ev-nav-drawer" class="ev-nav-drawer"\n'
        '      data-ev-nav-drawer="true" data-collapsed="false">\n'
        '      <li><a class="ev-nav-link" href="#services">Services</a></li>\n'
        '      <li><a class="ev-nav-link" href="#gallery">Gallery</a></li>\n'
        '      <li><a class="ev-nav-link" href="#contact">Contact</a></li>\n'
        '    </ul>\n'
        '  </nav>\n'
        "</header>"
    )

    value_prop_html = (
        f'    <p class="ev-hero-value">{esc_value_prop}</p>\n' if esc_value_prop else ""
    )
    trust_strip_html = (
        f'    <div class="ev-trust-strip">{trust_pills}</div>\n' if trust_pills else ""
    )

    hero_html = (
        f'<section id="hero" class="ev-hero" data-section="hero" data-archetype="{archetype}">\n'
        '  <div class="ev-hero-content">\n'
        f'    <span class="ev-eyebrow">{eyebrow_text}</span>\n'
        f'    <h1 class="ev-hero-title">{esc_headline}</h1>\n'
        f'    <p class="ev-hero-subtitle">{esc_tagline}</p>\n'
        f'{value_prop_html}'
        '    <div class="ev-hero-actions">\n'
        f'      <a class="cta btn" href="{html.escape(cta_href)}">{cta_text}</a>\n'
        '      <a class="ev-btn-secondary" href="#services">Explore Services</a>\n'
        '    </div>\n'
        f'{trust_strip_html}'
        '  </div>\n'
        f'  {hero_media_html}\n'
        f'  {rights_notice_html}\n'
        "</section>"
    )

    services_html = (
        '<section id="services" class="ev-section" '
        f'data-section="services" data-archetype="{archetype}">\n'
        '  <div class="ev-section-header">\n'
        '    <span class="ev-eyebrow">Services</span>\n'
        '    <h2 class="ev-section-title">Grooming Services &amp; Pricing</h2>\n'
        '  </div>\n'
        f'  {pricing_section_html}\n'
        "</section>"
    )

    footer_html = (
        '<footer class="ev-footer" data-section="footer">\n'
        '  <div class="ev-footer-inner">\n'
        f'    <p>&copy; {esc_name}</p>\n'
        '  </div>\n'
        "</footer>"
    )

    sections_map: dict[str, str] = {
        "header": header_html,
        "hero": hero_html,
        "services": services_html,
    }
    if gallery_html:
        sections_map["gallery"] = gallery_html
    if ba_html:
        sections_map["before-after"] = ba_html
    sections_map["contact"] = contact_html
    sections_map["footer"] = footer_html

    website_dir = project_root / "website"
    sections_dir = website_dir / "sections"
    css_dir = website_dir / "css"
    js_dir = website_dir / "js"
    sections_dir.mkdir(parents=True, exist_ok=True)
    css_dir.mkdir(parents=True, exist_ok=True)
    js_dir.mkdir(parents=True, exist_ok=True)

    # 1. Output per-section files under website/sections/<name>.html
    section_files: dict[str, Path] = {}
    for name, s_html in sections_map.items():
        s_path = sections_dir / f"{name}.html"
        _atomic_write_text(s_path, s_html.strip() + "\n")
        section_files[name] = s_path

    # 2. Output per-section css under website/css/<name>.css and combined css/styles.css
    section_css_map = _build_section_css_map(primary_hex, secondary_hex, accent_hex)
    section_css_files: dict[str, Path] = {}
    for css_name, css_text in section_css_map.items():
        c_path = css_dir / f"{css_name}.css"
        _atomic_write_text(c_path, css_text.strip() + "\n")
        section_css_files[css_name] = c_path

    combined_css = _build_css(primary_hex, secondary_hex, accent_hex)
    css_path = css_dir / "styles.css"
    _atomic_write_text(css_path, combined_css)

    # 3. Assemble into website/index.html strictly from section files
    html_doc = rebuild_index_from_sections(website_dir, title=business_name)

    js_content = _build_js(has_before_after=before_after is not None)
    index_path = website_dir / "index.html"
    js_path = js_dir / "main.js"

    _atomic_write_text(js_path, js_content)

    # 4. Compute section hashes using ONE unified compute_section_hashes
    section_hashes = compute_section_hashes(html_doc, section_css_map)
    hashes_path = website_dir / "section-hashes.json"
    _atomic_write_text(hashes_path, json.dumps(section_hashes, indent=2))

    return GeneratedWebsiteBundle(
        project_id=project_id,
        index_html_path=index_path,
        css_path=css_path,
        js_path=js_path,
        pricing_needs_review=pricing_needs_review,
        has_before_after_slider=before_after is not None,
        section_hashes_path=hashes_path,
        section_files=section_files,
        section_css_files=section_css_files,
    )


def revise_section(
    name: str,
    new_html: str,
    project_folder: str | Path | None = None,
) -> dict[str, str]:
    """Surgically replace ONLY the named section in sections/<name>.html and rebuild index.html."""
    if project_folder is not None:
        cand = Path(project_folder).resolve()
        website_dir = cand / "website" if (cand / "website").is_dir() else cand
    else:
        website_dir = Path("./website").resolve()

    if not website_dir.is_dir():
        raise ValueError(f"Website directory not found at {website_dir}")

    sections_dir = website_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    # 1. Validate new_html (no script/inline handlers/external URLs, data-section present, size cap)
    validate_section_html(name, new_html)

    cleaned_new = new_html.strip()

    # 2. Update website/sections/<name>.html
    target_sec_file = sections_dir / f"{name}.html"
    _atomic_write_text(target_sec_file, cleaned_new + "\n")

    # 3. Always rebuild website/index.html from section files
    rebuilt_doc = rebuild_index_from_sections(website_dir)

    # 4. Recompute and write website/section-hashes.json using ONE compute_section_hashes
    section_hashes = compute_section_hashes(rebuilt_doc, project_folder=website_dir)

    hashes_path = website_dir / "section-hashes.json"
    _atomic_write_text(hashes_path, json.dumps(section_hashes, indent=2))
    return section_hashes
