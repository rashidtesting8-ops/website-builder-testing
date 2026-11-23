"""TDD tests for Luxury Editorial Website Design System & Archetype Differentiation.

Verifies that `app/art_director.py` and `app/generator.py` produce distinct,
agency-grade luxury editorial websites across business archetypes (Classic Luxury Salon,
Mobile Grooming Fleet, and Multi-Species Dog & Cat Spa) without generic AI-slop
repetitive card boxes.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.art_director import generate_art_direction
from app.generator import generate_website
from PIL import Image


def _write_test_image(path: Path, color: tuple[int, int, int] = (27, 54, 93)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (640, 480), color=color)
    img.save(path, format="JPEG", quality=90)


def _seed_project(
    project_root: Path,
    *,
    business_name: str,
    facts: list[dict[str, object]],
    with_logo: bool = True,
) -> None:
    research_dir = project_root / "research"
    design_dir = project_root / "design"
    opt_dir = project_root / "assets" / "optimized"
    web_dir = project_root / "assets" / "website"
    research_dir.mkdir(parents=True, exist_ok=True)
    design_dir.mkdir(parents=True, exist_ok=True)
    opt_dir.mkdir(parents=True, exist_ok=True)
    web_dir.mkdir(parents=True, exist_ok=True)

    if with_logo:
        _write_test_image(web_dir / "brand-logo.jpg", (27, 54, 93))
    _write_test_image(opt_dir / "hero_01.webp", (35, 65, 100))
    _write_test_image(opt_dir / "gallery_01.webp", (180, 130, 80))
    _write_test_image(opt_dir / "gallery_02.webp", (110, 145, 130))
    _write_test_image(opt_dir / "gallery_03.webp", (90, 110, 140))

    all_facts = [
        {
            "category": "business_name",
            "claim_key": "name",
            "claim_value": business_name,
            "is_verified": True,
            "status": "verified",
        },
        *facts,
    ]
    (research_dir / "business-facts.json").write_text(
        json.dumps({"project_id": project_root.name, "facts": all_facts}, indent=2),
        encoding="utf-8",
    )
    (design_dir / "image-selection.json").write_text(
        json.dumps(
            {
                "project_id": project_root.name,
                "hero_image": {
                    "filename": "hero_01.webp",
                    "relative_path": "assets/optimized/hero_01.webp",
                    "object_position": "50% 28%",
                },
                "grooming_gallery": [
                    {
                        "filename": "gallery_01.webp",
                        "relative_path": "assets/optimized/gallery_01.webp",
                        "object_position": "50% 35%",
                    },
                    {
                        "filename": "gallery_02.webp",
                        "relative_path": "assets/optimized/gallery_02.webp",
                        "object_position": "50% 40%",
                    },
                    {
                        "filename": "gallery_03.webp",
                        "relative_path": "assets/optimized/gallery_03.webp",
                        "object_position": "50% 30%",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_luxury_editorial_design_tokens_and_typography(tmp_path: Path) -> None:
    """CSS must define editorial serif + sans typography tokens and warm luxury surfaces."""
    proj = tmp_path / "pup-palace-salon"
    _seed_project(
        proj,
        business_name="Pup Palace Grooming Salon",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(305) 555-0192",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "pricing",
                "claim_key": "price_full_spa_groom",
                "claim_value": "$95",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )

    generate_art_direction(proj)
    bundle = generate_website(proj)

    css_text = bundle.css_path.read_text(encoding="utf-8")
    # Must define luxury editorial font stacks (serif display + geometric sans)
    assert "--ev-font-display:" in css_text
    assert "Playfair Display" in css_text or "Cormorant Garamond" in css_text
    assert "--ev-font-sans:" in css_text
    # Must include reduced-motion accessibility support
    assert "@media (prefers-reduced-motion: reduce)" in css_text
    # Must include editorial spa menu & bento portfolio classes
    assert ".ev-menu-item" in css_text
    assert ".ev-bento-grid" in css_text


def test_nav_menu_toggle_is_hidden_on_desktop(tmp_path: Path) -> None:
    """The mobile 'Menu' pill must not render at desktop widths.

    Regression: `.ev-nav-toggle` had no media query, so desktop showed BOTH the
    Menu pill and the expanded link row (Services/Gallery/Contact) -- a
    contradictory affordance. The toggle belongs to the mobile breakpoint only.
    """
    import re

    proj = tmp_path / "nav-toggle-desktop"
    _seed_project(
        proj,
        business_name="Pup Palace Grooming Salon",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(305) 555-0192",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(proj)
    bundle = generate_website(proj)
    css_text = bundle.css_path.read_text(encoding="utf-8")

    # A desktop media query (min-width above the 640px mobile breakpoint) must
    # hide `.ev-nav-toggle`, allowing one level of nested rule braces.
    matches = re.findall(
        r"@media\s*\(\s*min-width:\s*(\d+)px\s*\)\s*\{"
        r"(?:[^{}]|\{[^{}]*\})*?"
        r"\.ev-nav-toggle\s*\{[^{}]*display:\s*none",
        css_text,
        flags=re.DOTALL,
    )
    assert matches, "Menu toggle must be hidden by a desktop (min-width) media query"
    assert any(int(px) > 640 for px in matches), "desktop query must exceed 640px"

    # The same desktop query must keep the link drawer visible so hiding the
    # toggle never leaves desktop with no navigation at all.
    drawer_visible = re.search(
        r"@media\s*\(\s*min-width:\s*\d+px\s*\)\s*\{"
        r"(?:[^{}]|\{[^{}]*\})*?"
        r"\.ev-nav-drawer(?:\[data-collapsed[^\]]*\])?\s*\{[^{}]*display:\s*flex",
        css_text,
        flags=re.DOTALL,
    )
    assert drawer_visible, "desktop must force the nav drawer visible"


def test_hero_proof_rating_renders_as_stars(tmp_path: Path) -> None:
    """A rating proof keeps its exact source text and gains a decorative star run.

    The proof '100+ 5* reviews' is a verified fact; the source string must reach
    the page verbatim (fidelity), while a decorative aria-hidden star run is
    prepended for visual weight. The accent adds no claim and alters no text.
    """
    import re

    proj = tmp_path / "proof-stars"
    _seed_project(
        proj,
        business_name="Pup Palace Grooming Salon",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(305) 555-0192",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "proof_point",
                "claim_value": "100+ 5* reviews",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(proj)
    bundle = generate_website(proj)
    html_text = bundle.index_html_path.read_text(encoding="utf-8")

    m = re.search(r'<p class="ev-hero-value">(.*?)</p>', html_text, re.DOTALL)
    assert m, "hero value prop should render"
    val = m.group(1)

    # A decorative star run is present and marked aria-hidden (presentational).
    assert "ev-stars" in val, "rating proof must render a star accent"
    assert 'aria-hidden="true"' in val, "star accent must be presentational only"
    assert "★★★★★" in val, "a 5-star claim must show five star glyphs"
    # The exact source claim reaches the page verbatim (source fidelity).
    assert "100+ 5* reviews" in val, "source claim must stay verbatim"


def test_non_rating_proof_renders_verbatim_without_stars(tmp_path: Path) -> None:
    """A non-rating proof point (e.g. 'Over 1000 clients') must never gain stars."""
    import re

    proj = tmp_path / "proof-plain"
    _seed_project(
        proj,
        business_name="Pup Palace Grooming Salon",
        facts=[
            {
                "category": "proof_point",
                "claim_value": "Over 1000 clients",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(proj)
    bundle = generate_website(proj)
    html_text = bundle.index_html_path.read_text(encoding="utf-8")

    m = re.search(r'<p class="ev-hero-value">(.*?)</p>', html_text, re.DOTALL)
    assert m, "hero value prop should render"
    val = m.group(1)
    assert "Over 1000 clients" in val
    assert "ev-stars" not in val, "non-rating proof must not render stars"


def test_single_proof_point_renders_exactly_once(tmp_path: Path) -> None:
    """A lone proof must render exactly once -- never in both hero and strip.

    Regression: the hero value line (stuffed with `proof_values[0]`) and the
    'Why pet parents choose us' strip both rendered the same proof, so the stat
    appeared twice on the page. The hero showcases the primary proof; the strip
    must exclude it.
    """
    proj = tmp_path / "proof-single-once"
    _seed_project(
        proj,
        business_name="Pup Palace Grooming Salon",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(305) 555-0192",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "proof_point",
                "claim_value": "100+ 5* reviews",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(proj)
    bundle = generate_website(proj)
    html_text = bundle.index_html_path.read_text(encoding="utf-8")

    assert html_text.count("100+ 5* reviews") == 1, (
        "a single proof must render exactly once, not in both hero and strip"
    )


def test_multiple_proofs_split_hero_and_strip_without_overlap(tmp_path: Path) -> None:
    """With several proofs the hero shows the primary one and the strip the rest.

    Each proof must appear exactly once across the whole page -- no stat may be
    repeated between the hero value line and the proof strip.
    """
    proj = tmp_path / "proof-multi-split"
    _seed_project(
        proj,
        business_name="Pup Palace Grooming Salon",
        facts=[
            {
                "category": "proof_point",
                "claim_value": "100+ 5* reviews",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "proof_point",
                "claim_value": "Over 1000 clients",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(proj)
    bundle = generate_website(proj)
    html_text = bundle.index_html_path.read_text(encoding="utf-8")

    assert html_text.count("100+ 5* reviews") == 1, "primary proof must appear once"
    assert html_text.count("Over 1000 clients") == 1, "supporting proof must appear once"


def test_archetype_layout_differentiation_across_three_taxonomies(tmp_path: Path) -> None:
    """Salon, Mobile Van, and Multi-Species Spa must generate distinct structural HTML."""
    # 1. Classic Luxury Salon
    salon_dir = tmp_path / "classic-salon"
    _seed_project(
        salon_dir,
        business_name="L'Atelier Canine Salon",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(212) 555-0144",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "pricing",
                "claim_key": "price_signature_bath_and_scissoring",
                "claim_value": "$115",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(salon_dir)
    salon_bundle = generate_website(salon_dir)
    salon_html = salon_bundle.index_html_path.read_text(encoding="utf-8")

    # 2. Mobile Grooming Van Fleet
    mobile_dir = tmp_path / "mobile-fleet"
    _seed_project(
        mobile_dir,
        business_name="Velvet Paws Mobile Grooming Van",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(404) 555-0188",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "services",
                "claim_key": "service_driveway",
                "claim_value": "Doorstep Heated Hydro-Bath & Full Groom",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(mobile_dir)
    mobile_bundle = generate_website(mobile_dir)
    mobile_html = mobile_bundle.index_html_path.read_text(encoding="utf-8")

    # 3. Multi-Species Dog & Cat Spa
    multi_dir = tmp_path / "multi-species-spa"
    _seed_project(
        multi_dir,
        business_name="Whiskers & Wags Feline and Canine Spa",
        facts=[
            {
                "category": "services",
                "claim_key": "service_dog",
                "claim_value": "Canine Breed-Standard Styling",
                "is_verified": True,
                "status": "verified",
            },
            {
                "category": "services",
                "claim_key": "service_cat",
                "claim_value": "Feline Plush Comb-Out & Lion Trim",
                "is_verified": True,
                "status": "verified",
            },
        ],
    )
    generate_art_direction(multi_dir)
    multi_bundle = generate_website(multi_dir)
    multi_html = multi_bundle.index_html_path.read_text(encoding="utf-8")

    # Verify Classic Salon uses editorial spa menu with dotted leaders and salon archetype
    assert 'data-archetype="salon"' in salon_html
    assert "ev-menu-item" in salon_html
    assert "ev-menu-leader" in salon_html
    assert "ev-bento-grid" in salon_html

    # Verify Mobile Groomer uses mobile fleet showcase and self-contained van badge
    assert 'data-archetype="mobile"' in mobile_html
    assert "ev-van-showcase" in mobile_html
    assert "Self-Contained" in mobile_html or "Driveway" in mobile_html

    # Verify Multi-Species Spa renders dedicated Canine & Feline species zones/badges
    assert 'data-archetype="multi-species"' in multi_html
    assert "ev-species-showcase" in multi_html
    assert "Feline" in multi_html and "Canine" in multi_html


def test_real_logo_and_editorial_brief_copy_rendered_in_website(tmp_path: Path) -> None:
    """Generator must render verified logo image in header and editorial copy from brief."""
    proj = tmp_path / "bespoke-salon"
    _seed_project(
        proj,
        business_name="Maison Bark Botanical Grooming",
        facts=[
            {
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(310) 555-0199",
                "is_verified": True,
                "status": "verified",
            },
        ],
        with_logo=True,
    )

    _, brief = generate_art_direction(proj)
    # Even without LLM adapter, art_director must synthesize non-empty editorial copy
    assert brief.editorial_headline
    assert brief.editorial_tagline
    assert brief.editorial_value_prop

    bundle = generate_website(proj)
    html_text = bundle.index_html_path.read_text(encoding="utf-8")

    # Header must render the actual brand logo image when present
    assert "ev-brand-logo" in html_text
    assert "../assets/website/brand-logo.jpg" in html_text
    # Hero must render the editorial headline and tagline from creative-brief.json
    assert brief.editorial_headline in html_text
    assert brief.editorial_tagline in html_text
