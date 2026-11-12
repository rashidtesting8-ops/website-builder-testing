"""Tests for section-based generator output and targeted revise_section function."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.generator import generate_website, revise_section
from app.qa_engine import _serve_project_directory
from playwright.sync_api import sync_playwright


def _scaffold_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "paws-section-test"
    (project_root / "research").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)

    facts = {
        "project_id": "paws-section-test",
        "extracted_at": "2026-11-06T12:00:00Z",
        "prompt_injection_warnings": [],
        "facts": [
            {
                "fact_id": "f1",
                "project_id": "paws-section-test",
                "category": "business_name",
                "claim_key": "business_name",
                "claim_value": "Paws & Whiskers Salon",
                "source_url": "https://paws.example",
                "source_text_snippet": "Paws & Whiskers Salon",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-06T12:00:00Z",
            },
            {
                "fact_id": "f2",
                "project_id": "paws-section-test",
                "category": "pricing",
                "claim_key": "price_full_groom",
                "claim_value": "$75.00",
                "source_url": "https://paws.example",
                "source_text_snippet": "Full Groom - $75.00",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-06T12:00:00Z",
            },
            {
                "fact_id": "f3",
                "project_id": "paws-section-test",
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(512) 555-0123",
                "source_url": "https://paws.example",
                "source_text_snippet": "(512) 555-0123",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-06T12:00:00Z",
            },
        ],
    }
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps(facts, indent=2), encoding="utf-8"
    )

    brand = {
        "project_id": "paws-section-test",
        "primary_color": "#1B365D",
        "secondary_color": "#D98A2B",
        "accent_color": "#8FA998",
        "business_name": "Paws & Whiskers Salon",
    }
    (project_root / "design" / "brand.json").write_text(
        json.dumps(brand, indent=2), encoding="utf-8"
    )

    brief = {
        "project_id": "paws-section-test",
        "business_name": "Paws & Whiskers Salon",
        "service_type": "salon",
    }
    (project_root / "design" / "creative-brief.json").write_text(
        json.dumps(brief, indent=2), encoding="utf-8"
    )

    return project_root


def test_generator_outputs_per_section_files_and_section_hashes(tmp_path: Path) -> None:
    project_root = _scaffold_project(tmp_path)
    bundle = generate_website(project_root)

    website_dir = project_root / "website"
    sections_dir = website_dir / "sections"
    css_dir = website_dir / "css"
    hashes_file = website_dir / "section-hashes.json"

    assert bundle.index_html_path.is_file()
    assert sections_dir.is_dir()
    assert css_dir.is_dir()
    assert hashes_file.is_file()

    # Core sections must exist under website/sections/<name>.html
    expected_sections = ["header", "hero", "services", "contact", "footer"]
    for name in expected_sections:
        sec_html_path = sections_dir / f"{name}.html"
        assert sec_html_path.is_file(), f"Missing section file: {sec_html_path}"
        content = sec_html_path.read_text(encoding="utf-8")
        assert f'data-section="{name}"' in content, f"data-section='{name}' missing in {name}.html"

        sec_css_path = css_dir / f"{name}.css"
        assert sec_css_path.is_file(), f"Missing section CSS: {sec_css_path}"

    # Assembled index.html must contain all section markup and data-section attributes
    index_html = bundle.index_html_path.read_text(encoding="utf-8")
    for name in expected_sections:
        assert f'data-section="{name}"' in index_html

    # Check section-hashes.json content
    hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
    for name in expected_sections:
        assert name in hashes
        assert len(hashes[name]) == 64


def test_generator_drops_page_title_prefix_from_business_headline(tmp_path: Path) -> None:
    project_root = _scaffold_project(tmp_path)
    brief_path = project_root / "design" / "creative-brief.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    brief["business_name"] = "Prices & services – Paws & Whiskers Salon"
    brief["editorial_headline"] = (
        "Prices & services – Paws & Whiskers Salon — Dog Grooming"
    )
    brief["editorial_tagline"] = "Grooming services for dogs."
    brief["editorial_value_prop"] = "Grooming services for dogs."
    brief_path.write_text(json.dumps(brief), encoding="utf-8")

    generate_website(project_root)

    index_html = (project_root / "website" / "index.html").read_text(encoding="utf-8")
    hero_html = (
        (project_root / "website" / "sections" / "hero.html").read_text(encoding="utf-8")
    )
    assert "<title>Paws &amp; Whiskers Salon</title>" in index_html
    assert "Prices &amp; services" not in hero_html
    assert "Paws &amp; Whiskers Salon — Dog Grooming" in hero_html
    assert hero_html.count("Grooming services for dogs.") == 1


def test_generator_warns_when_selected_assets_have_unconfirmed_rights(
    tmp_path: Path,
) -> None:
    project_root = _scaffold_project(tmp_path)
    asset_path = project_root / "assets" / "optimized" / "hero.jpg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"local test image")
    asset_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()

    (project_root / "research" / "asset-manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "assets/optimized/hero.jpg",
                    "source_url": "https://example.test/hero.jpg",
                    "sha256": asset_sha256,
                    "captured_at": "2026-11-12T00:00:00Z",
                    "rights_confirmed_for_preview": False,
                },
                {
                    "file": "assets/optimized/hero.jpg",
                    "source_url": "https://other.example.test/hero.jpg",
                    "sha256": asset_sha256,
                    "captured_at": "2026-11-12T00:00:00Z",
                    "rights_confirmed_for_preview": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    (project_root / "design" / "image-selection.json").write_text(
        json.dumps(
            {
                "hero_image": {
                    "filename": "hero.jpg",
                    "relative_path": "assets/optimized/hero.jpg",
                },
                "grooming_gallery": [],
            }
        ),
        encoding="utf-8",
    )

    generate_website(project_root)

    index_html = (project_root / "website" / "index.html").read_text(encoding="utf-8")
    assert "Local preview only. Photo reuse rights have not been confirmed" in index_html


def test_generator_renders_duplicate_verified_pricing_once(tmp_path: Path) -> None:
    project_root = _scaffold_project(tmp_path)
    facts_path = project_root / "research" / "business-facts.json"
    facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
    duplicate = dict(facts_payload["facts"][1])
    duplicate["fact_id"] = "f2-duplicate"
    duplicate["source_url"] = "https://paws.example/prices"
    facts_payload["facts"].append(duplicate)
    service = dict(facts_payload["facts"][1])
    service["fact_id"] = "f3-service"
    service["category"] = "services"
    service["claim_key"] = "service_full_groom"
    service["claim_value"] = "Full Groom"
    facts_payload["facts"].append(service)
    facts_path.write_text(json.dumps(facts_payload), encoding="utf-8")

    generate_website(project_root)

    services_html = (project_root / "website" / "sections" / "services.html").read_text(
        encoding="utf-8"
    )
    assert services_html.count("$75.00") == 1
    assert services_html.count("Full Groom") == 1


def test_revise_section_modifies_only_target_section_leaving_others_unchanged(
    tmp_path: Path,
) -> None:
    project_root = _scaffold_project(tmp_path)
    generate_website(project_root)

    website_dir = project_root / "website"
    hashes_file = website_dir / "section-hashes.json"
    initial_hashes = json.loads(hashes_file.read_text(encoding="utf-8"))

    # Revise ONLY the 'hero' section
    new_hero_html = (
        '<section id="hero" class="ev-hero" data-section="hero">'
        '<div class="ev-hero-revised">'
        '<h1 class="ev-hero-title">Bespoke Paws &amp; Whiskers</h1>'
        '<p class="ev-hero-subtitle">Award-winning compassionate pet styling.</p>'
        '<a class="cta btn" href="#contact">Book Today</a>'
        '</div>'
        '</section>'
    )

    updated_hashes = revise_section(
        "hero",
        new_hero_html,
        project_folder=project_root,
    )

    # 1. Target section 'hero' hash MUST change
    assert updated_hashes["hero"] != initial_hashes["hero"]

    # 2. Hero file on disk must match new content
    hero_file = website_dir / "sections" / "hero.html"
    assert "Bespoke Paws &amp; Whiskers" in hero_file.read_text(encoding="utf-8")

    # 3. Assembled index.html must contain the revised hero
    index_html = (website_dir / "index.html").read_text(encoding="utf-8")
    assert "Bespoke Paws &amp; Whiskers" in index_html

    # 4. CRITICAL INVARIANT: All other section hashes MUST stay strictly identical
    for section_name, original_hash in initial_hashes.items():
        if section_name != "hero":
            assert updated_hashes[section_name] == original_hash, (
                f"Section '{section_name}' hash unexpectedly changed from {original_hash} "
                f"to {updated_hashes[section_name]}"
            )


def test_revise_section_validates_new_html_rejects_unsafe_markup_and_size(
    tmp_path: Path,
) -> None:
    project_root = _scaffold_project(tmp_path)
    generate_website(project_root)

    # 1. Missing data-section attribute
    with pytest.raises(ValueError, match="data-section"):
        revise_section("hero", "<section id='hero'><h1>No attr</h1></section>", project_root)

    # 2. Contains script tags
    with pytest.raises(ValueError, match="[Ss]cript"):
        revise_section(
            "hero",
            '<section id="hero" data-section="hero"><script>alert(1)</script></section>',
            project_root,
        )

    # 3. Contains inline event handlers (e.g. onerror or onclick)
    with pytest.raises(ValueError, match="[Hh]andler|inline|event"):
        revise_section(
            "hero",
            '<section id="hero" data-section="hero"><img src="x" onerror="alert(1)"></section>',
            project_root,
        )

    # 4. Contains external URLs
    with pytest.raises(ValueError, match="[Ee]xternal|[Uu][Rr][Ll]"):
        revise_section(
            "hero",
            '<section id="hero" data-section="hero"><img src="https://evil.example/dog.jpg"></section>',
            project_root,
        )

    # 5. Exceeds size cap
    huge_html = (
        f'<section id="hero" data-section="hero"><p>{"A" * 600_000}</p></section>'
    )
    with pytest.raises(ValueError, match="[Ss]ize|[Cc]ap|large"):
        revise_section("hero", huge_html, project_root)


def test_revise_section_always_rebuilds_index_from_section_files(tmp_path: Path) -> None:
    project_root = _scaffold_project(tmp_path)
    generate_website(project_root)
    website_dir = project_root / "website"
    index_file = website_dir / "index.html"

    # Manually delete index.html to prove revise_section rebuilds it from section files
    index_file.unlink()
    assert not index_file.exists()

    new_hero = (
        '<section id="hero" class="ev-hero" data-section="hero">'
        '<h1>Rebuilt Hero</h1>'
        '</section>'
    )
    revise_section("hero", new_hero, project_root)

    assert index_file.is_file()
    index_text = index_file.read_text(encoding="utf-8")
    assert "Rebuilt Hero" in index_text
    assert 'data-section="header"' in index_text
    assert 'data-section="services"' in index_text
    assert 'data-section="footer"' in index_text


def test_playwright_render_1440_and_390_section_screenshots_pixel_identical_except_hero(
    tmp_path: Path,
) -> None:
    project_root = _scaffold_project(tmp_path)

    with _serve_project_directory(project_root) as port:
        url = f"http://127.0.0.1:{port}/website/index.html"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in (1440, 390):
                # Clean generation for each viewport test
                generate_website(project_root)
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    device_scale_factor=1,
                )
                page = context.new_page()
                page.goto(url, wait_until="load")
                page.wait_for_timeout(200)

                # Capture initial screenshot for each section
                before_shots: dict[str, bytes] = {}
                sections = page.locator("[data-section]").all()
                assert len(sections) >= 4, "Expected at least header, hero, services, footer"
                for sec in sections:
                    sname = str(sec.get_attribute("data-section"))
                    before_shots[sname] = sec.screenshot(animations="disabled")

                hero_box = page.locator('[data-section="hero"]').bounding_box()
                assert hero_box is not None
                h = hero_box["height"]

                # Perform targeted revision on ONLY 'hero' matching bounding height
                # to avoid subpixel layout shifts on downstream sections
                new_hero = (
                    f'<section id="hero" class="ev-hero" data-section="hero" '
                    f'style="height: {h}px; box-sizing: border-box; background-color: #0d233a;">'
                    f'<div class="ev-hero-content">'
                    f'<h1 class="ev-hero-title">Royal Canine Styling</h1>'
                    f'<p class="ev-hero-subtitle">Premium spa experience for pets.</p>'
                    f'<a class="cta btn" href="#contact">Book Today</a>'
                    f'</div>'
                    f'</section>'
                )
                revise_section("hero", new_hero, project_root)

                # Re-render page
                page.reload(wait_until="load")
                page.wait_for_timeout(200)

                after_shots: dict[str, bytes] = {}
                for sec in page.locator("[data-section]").all():
                    sname = str(sec.get_attribute("data-section"))
                    after_shots[sname] = sec.screenshot(animations="disabled")

                # Hero MUST differ visually
                assert after_shots["hero"] != before_shots["hero"], (
                    f"Hero section did not differ at {width}px viewport!"
                )

                # Every other section screenshot MUST be pixel-identical
                for sname, initial_bytes in before_shots.items():
                    if sname != "hero":
                        assert after_shots[sname] == initial_bytes, (
                            f"Section '{sname}' had unexpected visual diff at {width}px viewport!"
                        )

                context.close()
            browser.close()
