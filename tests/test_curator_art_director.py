"""Unit tests for Visual Curator (app/curator.py) and Art Director (app/art_director.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.art_director import generate_art_direction
from app.curator import (
    CSS_PERCENT_RE,
    CuratorHeuristicObservation,
    curate_project_images,
)
from PIL import Image, ImageDraw

from engine.llm.adapter import BudgetExceeded

CSS_COORD_PATTERN = re.compile(r"^\d{1,3}(?:\.\d+)?%\s+\d{1,3}(?:\.\d+)?%$")


def _create_image(
    path: Path,
    *,
    width: int = 1200,
    height: int = 800,
    bg_color: tuple[int, int, int] = (45, 85, 130),
    subject_box: tuple[int, int, int, int] | None = None,
    subject_color: tuple[int, int, int] = (230, 175, 95),
    add_sharp_edges: bool = True,
    add_banner_text: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    if subject_box is not None:
        draw.rectangle(subject_box, fill=subject_color, outline=(255, 255, 255), width=4)

    if add_sharp_edges:
        # Draw high-contrast texture grid inside subject region for high edge sharpness
        x0, y0, x1, y1 = subject_box or (width // 4, height // 6, 3 * width // 4, 2 * height // 3)
        for x in range(x0, x1, 12):
            draw.line([(x, y0), (x, y1)], fill=(20, 20, 20), width=2)
        for y in range(y0, y1, 12):
            draw.line([(x0, y), (x1, y)], fill=(245, 245, 245), width=2)

    if add_banner_text:
        # Draw high-contrast top promotional banner strip with alternating vertical strokes
        banner_h = max(40, height // 6)
        draw.rectangle((0, 0, width, banner_h), fill=(255, 255, 0))
        for x in range(20, width - 20, 8):
            draw.rectangle((x, 10, x + 4, banner_h - 10), fill=(0, 0, 0))

    img.save(path, format="JPEG", quality=92)
    return path


def test_curator_excludes_low_res_and_baked_in_text_hero_candidates(tmp_path: Path) -> None:
    """Hero candidates with < 800px width or baked-in promotional text must be excluded."""
    project_root = tmp_path / "projects" / "paws-salon"
    opt_dir = project_root / "assets" / "optimized"
    thumb_dir = project_root / "assets" / "thumbnails"

    # 1. Valid high-res hero image with subject positioned in upper-center
    _create_image(
        opt_dir / "hero_dog_portrait.jpg",
        width=1400,
        height=900,
        subject_box=(450, 100, 950, 450),
    )

    # 2. Low-resolution candidate (< 800px width)
    _create_image(
        opt_dir / "tiny_dog.jpg",
        width=640,
        height=480,
    )

    # 3. Candidate with baked-in promotional text / discount banner
    _create_image(
        opt_dir / "promo_50_off_banner.jpg",
        width=1200,
        height=800,
        add_banner_text=True,
    )

    # 4. Four unique verified grooming gallery photos
    for idx in range(1, 5):
        _create_image(
            opt_dir / f"groom_portfolio_{idx}.jpg",
            width=1000,
            height=800,
            bg_color=(40 + idx * 15, 90, 120),
            subject_box=(250, 150, 750, 600),
        )

    # 5. Salon facility / van candidate
    _create_image(
        opt_dir / "salon_interior_facility.jpg",
        width=1200,
        height=800,
        bg_color=(80, 110, 95),
        subject_box=(200, 200, 1000, 650),
    )

    # Contact sheet
    _create_image(thumb_dir / "contact-sheet.jpg", width=1000, height=600)

    def mock_lvm_adapter(image_path: Path, _img: Image.Image) -> CuratorHeuristicObservation:
        name = image_path.name.lower()
        if "promo" in name or "banner" in name:
            return CuratorHeuristicObservation(
                pet_visible=True,
                has_embedded_text=True,
                sharpness_score=0.85,
                role_hint="hero",
                focal_x_pct=50.0,
                focal_y_pct=30.0,
                notes="Detected baked-in promotional discount text",
            )
        if "salon" in name or "van" in name or "facility" in name:
            return CuratorHeuristicObservation(
                pet_visible=False,
                has_embedded_text=False,
                sharpness_score=0.88,
                role_hint="facility_or_van",
                focal_x_pct=50.0,
                focal_y_pct=50.0,
            )
        return CuratorHeuristicObservation(
            pet_visible=True,
            has_embedded_text=False,
            sharpness_score=0.92 if "hero" in name else 0.80,
            role_hint="hero" if "hero" in name else "gallery",
            focal_x_pct=50.0,
            focal_y_pct=28.0,
        )

    selection = curate_project_images(project_root, lvm_adapter=mock_lvm_adapter)

    out_file = project_root / "design" / "image-selection.json"
    assert out_file.is_file()

    excluded_map = {item.filename: item.reasons for item in selection.excluded_images}
    assert "tiny_dog.jpg" in excluded_map
    assert any("low_resolution" in r for r in excluded_map["tiny_dog.jpg"])

    assert "promo_50_off_banner.jpg" in excluded_map
    assert any("embedded_text" in r for r in excluded_map["promo_50_off_banner.jpg"])

    # Verify hero_image was selected and is neither excluded file
    assert selection.hero_image is not None
    assert selection.hero_image.filename == "hero_dog_portrait.jpg"

    # Verify minimum 4 unique verified photos in grooming_gallery
    assert len(selection.grooming_gallery) >= 4

    # Verify salon_facility_or_van is populated
    assert selection.salon_facility_or_van is not None
    assert selection.salon_facility_or_van.filename == "salon_interior_facility.jpg"

    # Strictly prevent duplicate image assignment across different sections
    assigned_filenames: list[str] = []
    if selection.hero_image:
        assigned_filenames.append(selection.hero_image.filename)
    assigned_filenames.extend(g.filename for g in selection.grooming_gallery)
    if selection.salon_facility_or_van:
        assigned_filenames.append(selection.salon_facility_or_van.filename)

    assert len(assigned_filenames) == len(set(assigned_filenames))


def test_curator_focal_coordinates_are_valid_css_percentages(tmp_path: Path) -> None:
    """Focal coordinates must format as valid CSS object-position percentages (e.g. '50% 30%')."""
    project_root = tmp_path / "projects" / "focal-test"
    opt_dir = project_root / "assets" / "optimized"

    _create_image(
        opt_dir / "hero_upper_face.jpg",
        width=1600,
        height=1000,
        bg_color=(25, 25, 30),
        subject_box=(600, 80, 1000, 360),
    )
    for idx in range(1, 5):
        _create_image(
            opt_dir / f"dog_gallery_{idx}.jpg",
            width=1200,
            height=900,
            subject_box=(300, 120, 900, 500),
        )

    selection = curate_project_images(project_root)

    assert selection.hero_image is not None
    assert CSS_COORD_PATTERN.match(selection.hero_image.object_position)
    assert CSS_PERCENT_RE.match(selection.hero_image.focal_x)
    assert CSS_PERCENT_RE.match(selection.hero_image.focal_y)

    for item in selection.grooming_gallery:
        assert CSS_COORD_PATTERN.match(item.object_position)
        assert 0.0 <= item.focal_point_x_pct <= 100.0
        assert 0.0 <= item.focal_point_y_pct <= 100.0


def test_art_director_mobile_and_cat_dog_wireframes_and_brand_colors(tmp_path: Path) -> None:
    """Art Director extracts brand palette (or fallback) and builds mobile & cat+dog wireframes."""
    # Case 1: Mobile Groomer with logo
    mobile_root = tmp_path / "projects" / "zoom-mobile-groom"
    (mobile_root / "research").mkdir(parents=True, exist_ok=True)
    (mobile_root / "design").mkdir(parents=True, exist_ok=True)

    logo_path = _create_image(
        mobile_root / "assets" / "website" / "logo.jpg",
        width=400,
        height=200,
        bg_color=(28, 64, 118),
        subject_box=(50, 40, 350, 160),
        subject_color=(217, 138, 43),
        add_sharp_edges=False,
    )

    mobile_facts = {
        "project_id": "zoom-mobile-groom",
        "extracted_at": "2026-11-05T12:00:00Z",
        "prompt_injection_warnings": [],
        "facts": [
            {
                "fact_id": "fact-1",
                "project_id": "zoom-mobile-groom",
                "category": "business_name",
                "claim_key": "business_name",
                "claim_value": "Zoom Mobile Dog Grooming Van",
                "source_url": "https://zoomgroom.example/index",
                "source_text_snippet": "Zoom Mobile Dog Grooming Van comes to your driveway",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
            {
                "fact_id": "fact-2",
                "project_id": "zoom-mobile-groom",
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(512) 555-0199",
                "source_url": "https://zoomgroom.example/index",
                "source_text_snippet": "Call (512) 555-0199",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
        ],
    }
    (mobile_root / "research" / "business-facts.json").write_text(
        json.dumps(mobile_facts),
        encoding="utf-8",
    )

    brand_mobile, brief_mobile = generate_art_direction(
        mobile_root,
        logo_path=logo_path,
    )

    assert (mobile_root / "design" / "brand.json").is_file()
    assert (mobile_root / "design" / "creative-brief.json").is_file()
    assert brand_mobile.primary_color.startswith("#")
    assert brand_mobile.secondary_color.startswith("#")
    assert brief_mobile.taxonomy.is_mobile_groomer is True
    assert brief_mobile.wireframe.feature_service_radius_map is True
    assert brief_mobile.wireframe.feature_self_contained_van_badge is True
    assert brief_mobile.wireframe.instant_call_cta is True

    # Case 2: Cat + Dog Salon without logo (accessible fallback: navy, warm amber, soft sage)
    salon_root = tmp_path / "projects" / "whiskers-and-wags"
    (salon_root / "research").mkdir(parents=True, exist_ok=True)
    salon_facts = {
        "project_id": "whiskers-and-wags",
        "extracted_at": "2026-11-05T12:00:00Z",
        "prompt_injection_warnings": [],
        "facts": [
            {
                "fact_id": "fact-10",
                "project_id": "whiskers-and-wags",
                "category": "business_name",
                "claim_key": "business_name",
                "claim_value": "Whiskers & Wags Cat and Dog Grooming Salon",
                "source_url": "https://whiskerswags.example/",
                "source_text_snippet": "Whiskers & Wags Cat and Dog Grooming Salon",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
            {
                "fact_id": "fact-11",
                "project_id": "whiskers-and-wags",
                "category": "services",
                "claim_key": "service_feline_lion_cut",
                "claim_value": "Feline Lion Cut & Cat Bath",
                "source_url": "https://whiskerswags.example/services",
                "source_text_snippet": "Feline Lion Cut & Cat Bath - $95",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
            {
                "fact_id": "fact-12",
                "project_id": "whiskers-and-wags",
                "category": "services",
                "claim_key": "service_full_dog_groom",
                "claim_value": "Full Dog Groom & Deshedding",
                "source_url": "https://whiskerswags.example/services",
                "source_text_snippet": "Full Dog Groom & Deshedding - $85",
                "is_verified": True,
                "status": "verified",
                "last_checked_at": "2026-11-05T12:00:00Z",
            },
        ],
    }
    (salon_root / "research" / "business-facts.json").write_text(
        json.dumps(salon_facts),
        encoding="utf-8",
    )

    brand_salon, brief_salon = generate_art_direction(salon_root)

    assert brand_salon.used_fallback_palette is True
    assert brand_salon.palette_names == ["navy", "warm_amber", "soft_sage"]
    assert brief_salon.taxonomy.serves_dogs is True
    assert brief_salon.taxonomy.serves_cats is True
    assert brief_salon.wireframe.segregated_dog_cat_pricing_tabs is True
    assert "dogs" in brief_salon.wireframe.pricing_tabs
    assert "cats" in brief_salon.wireframe.pricing_tabs


def _create_art_direction_project(project_root: Path) -> None:
    (project_root / "research").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps(
            {
                "project_id": project_root.name,
                "facts": [
                    {
                        "category": "business_name",
                        "claim_value": "Paws and Polish",
                        "is_verified": True,
                        "status": "verified",
                    },
                    {
                        "category": "services",
                        "claim_value": "Dog grooming",
                        "is_verified": True,
                        "status": "verified",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


class _RaisingCompletionAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, **_kwargs: object) -> object:
        raise self.error


def test_art_director_propagates_budget_exceeded(tmp_path: Path) -> None:
    project_root = tmp_path / "budget-limited-art-direction"
    _create_art_direction_project(project_root)

    with pytest.raises(BudgetExceeded, match="call cap"):
        generate_art_direction(
            project_root,
            llm_adapter=_RaisingCompletionAdapter(BudgetExceeded("call cap reached")),
        )


def test_art_director_records_warning_on_provider_error_and_keeps_defaults(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "provider-error-art-direction"
    _create_art_direction_project(project_root)
    adapter = _RaisingCompletionAdapter(RuntimeError("provider unavailable"))

    _brand, brief = generate_art_direction(project_root, llm_adapter=adapter)
    saved_brief = json.loads(
        (project_root / "design" / "creative-brief.json").read_text(encoding="utf-8")
    )

    assert brief.editorial_headline == "Paws and Polish — Dog Grooming"
    assert brief.llm_used is False
    assert any("provider unavailable" in warning for warning in brief.warnings)
    assert saved_brief["llm_used"] is False
    assert saved_brief["warnings"] == brief.warnings


def test_art_director_prefers_verified_business_name_over_title_variant(
    tmp_path: Path,
) -> None:
    """The creative brief must use the verified business name, not a needs_review title variant.

    Regression: _detect_taxonomy used to take the LAST business_name fact regardless of
    status, so junk page-title variants ('Location | <name>') reached the brief/headline.
    """
    verified_fact = {
        "fact_id": "fact-verified-name",
        "project_id": "hachi-style",
        "category": "business_name",
        "claim_key": "business_name",
        "claim_value": "Hachi & Co",
        "source_url": "https://hachi.example.com/",
        "source_text_snippet": "Hachi & Co",
        "source_kind": "json_ld",
        "is_verified": True,
        "status": "verified",
        "last_checked_at": "2026-11-05T12:00:00Z",
    }
    junk_fact = {
        "fact_id": "fact-title-variant",
        "project_id": "hachi-style",
        "category": "business_name",
        "claim_key": "business_name",
        "claim_value": "Location | Hachi & Co",
        "source_url": "https://hachi.example.com/location",
        "source_text_snippet": "Location | Hachi & Co",
        "source_kind": "regex",
        "is_verified": False,
        "status": "needs_review",
        "last_checked_at": "2026-11-05T12:00:00Z",
    }

    for order_name, facts in (
        ("verified-first", [verified_fact, junk_fact]),
        ("verified-last", [junk_fact, verified_fact]),
    ):
        project_root = tmp_path / f"hachi-style-{order_name}"
        (project_root / "research").mkdir(parents=True, exist_ok=True)
        payload = {
            "project_id": project_root.name,
            "extracted_at": "2026-11-05T12:00:00Z",
            "prompt_injection_warnings": [],
            "facts": facts,
        }
        (project_root / "research" / "business-facts.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        _brand, brief = generate_art_direction(project_root)

        assert brief.business_name == "Hachi & Co", order_name
        assert brief.editorial_headline is not None
        assert brief.editorial_headline.startswith("Hachi & Co"), order_name
        assert (project_root / "design" / "creative-brief.json").is_file()
