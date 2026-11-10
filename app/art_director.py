"""Brand Analyst & Art Director for the EditVista Pet Grooming Website Builder Engine.

Consumes `research/business-facts.json`, `design/image-selection.json`, and logo provenance
to extract primary/secondary brand colors (or fall back to the accessible pet-care palette:
navy, warm amber, soft sage) and determine bespoke layout wireframe structure based on
business taxonomy (mobile groomer vs. cat + dog salon).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db import AssetProvenanceRecord

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Accessible pet-care fallback palette: navy, warm amber, soft sage
FALLBACK_NAVY_HEX = "#1B365D"
FALLBACK_WARM_AMBER_HEX = "#D98A2B"
FALLBACK_SOFT_SAGE_HEX = "#8FA998"
FALLBACK_PALETTE_NAMES = ["navy", "warm_amber", "soft_sage"]

MOBILE_KEYWORDS = (
    "mobile",
    "van",
    "house call",
    "house-call",
    "curbside",
    "driveway",
    "at your door",
)
CAT_KEYWORDS = (
    "cat",
    "cats",
    "feline",
    "kitten",
    "lion cut",
    "whiskers",
)
DOG_KEYWORDS = (
    "dog",
    "dogs",
    "canine",
    "puppy",
    "puppies",
    "paws",
    "deshed",
    "de-shed",
)


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


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _is_near_neutral(rgb: tuple[int, int, int]) -> bool:
    """Filter out near-white or near-black background pixels when sampling logo colors."""
    r, g, b = rgb
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return brightness > 235 or brightness < 20


def _color_distance(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    return float(((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2) ** 0.5)


def extract_colors_from_logo(logo_file: Path) -> tuple[str, str, str] | None:
    """Extract primary, secondary, and accent hex colors from a verified logo image."""
    if not logo_file.is_file():
        return None

    with Image.open(logo_file) as img:
        small = img.convert("RGB").resize((64, 64))
        quantized = small.quantize(colors=8)
        palette = quantized.getpalette()
        color_counts = quantized.getcolors()

    if not palette or not color_counts:
        return None

    ranked_rgbs: list[tuple[int, int, int]] = []
    for _count, idx in sorted(color_counts, key=lambda pair: pair[0], reverse=True):
        if not isinstance(idx, int):
            continue
        base = idx * 3
        if base + 2 >= len(palette):
            continue
        rgb = (int(palette[base]), int(palette[base + 1]), int(palette[base + 2]))
        if not _is_near_neutral(rgb):
            ranked_rgbs.append(rgb)

    if not ranked_rgbs:
        return None

    primary_rgb = ranked_rgbs[0]
    secondary_rgb = FALLBACK_WARM_AMBER_HEX
    for candidate in ranked_rgbs[1:]:
        if _color_distance(primary_rgb, candidate) >= 45.0:
            secondary_rgb = _rgb_to_hex(candidate)
            break

    primary_hex = _rgb_to_hex(primary_rgb)
    secondary_hex = secondary_rgb if isinstance(secondary_rgb, str) else _rgb_to_hex(secondary_rgb)
    return primary_hex, secondary_hex, FALLBACK_SOFT_SAGE_HEX


class BrandIdentity(BaseModel):
    """Pydantic v2 contract persisted to `projects/<business-id>/design/brand.json`."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    business_name: str
    logo_path: str | None = None
    primary_color: str = FALLBACK_NAVY_HEX
    secondary_color: str = FALLBACK_WARM_AMBER_HEX
    accent_color: str = FALLBACK_SOFT_SAGE_HEX
    palette_names: list[str] = Field(default_factory=lambda: list(FALLBACK_PALETTE_NAMES))
    used_fallback_palette: bool = True
    generated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("primary_color", "secondary_color", "accent_color")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        if not HEX_COLOR_RE.match(v):
            raise ValueError(f"Color must be a 6-digit hex code: {v}")
        return v.upper()


class BusinessTaxonomy(BaseModel):
    """Detected pet grooming business taxonomy from verified facts."""

    model_config = ConfigDict(extra="forbid")

    is_mobile_groomer: bool = False
    is_salon: bool = True
    serves_dogs: bool = True
    serves_cats: bool = False


class WireframeStructure(BaseModel):
    """Bespoke layout wireframe flags and ordered section hierarchy."""

    model_config = ConfigDict(extra="forbid")

    feature_service_radius_map: bool = False
    feature_self_contained_van_badge: bool = False
    instant_call_cta: bool = False
    segregated_dog_cat_pricing_tabs: bool = False
    pricing_tabs: list[str] = Field(default_factory=lambda: ["dogs"])
    ordered_sections: list[str] = Field(default_factory=list)


class CreativeBrief(BaseModel):
    """Pydantic v2 contract persisted to `projects/<business-id>/design/creative-brief.json`."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    business_name: str
    taxonomy: BusinessTaxonomy
    brand: BrandIdentity
    wireframe: WireframeStructure
    hero_image_filename: str | None = None
    hero_object_position: str | None = None
    gallery_filenames: list[str] = Field(default_factory=list)
    facility_or_van_filename: str | None = None
    primary_cta_label: str
    primary_cta_target: str | None = None
    editorial_headline: str | None = None
    editorial_tagline: str | None = None
    editorial_value_prop: str | None = None
    suggested_sections: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utc_now)


def _detect_taxonomy(
    facts: list[dict[str, Any]],
    project_id: str,
) -> tuple[str, str | None, BusinessTaxonomy]:
    business_name = project_id
    phone: str | None = None
    corpus_parts: list[str] = [project_id.replace("-", " ")]

    for fact in facts:
        category = str(fact.get("category", ""))
        key = str(fact.get("claim_key", ""))
        val = str(fact.get("claim_value", ""))
        snippet = str(fact.get("source_text_snippet", ""))
        if category == "business_name" and val.strip():
            business_name = val.strip()
        if (category == "phone" or key == "phone") and val.strip() and phone is None:
            phone = val.strip()
        corpus_parts.extend([key, val, snippet])

    corpus = " ".join(corpus_parts).lower()
    is_mobile = any(kw in corpus for kw in MOBILE_KEYWORDS)
    serves_cats = any(kw in corpus for kw in CAT_KEYWORDS)
    serves_dogs = any(kw in corpus for kw in DOG_KEYWORDS) or not serves_cats

    taxonomy = BusinessTaxonomy(
        is_mobile_groomer=is_mobile,
        is_salon=not is_mobile,
        serves_dogs=serves_dogs,
        serves_cats=serves_cats,
    )
    return business_name, phone, taxonomy


def _build_wireframe(taxonomy: BusinessTaxonomy) -> WireframeStructure:
    if taxonomy.is_mobile_groomer:
        sections = [
            "hero_with_instant_call_cta",
            "self_contained_water_and_van_badge",
            "service_radius_map",
            "services_and_pricing",
            "grooming_gallery",
            "cancellation_policy_and_faq",
            "footer_instant_call_cta",
        ]
    elif taxonomy.serves_dogs and taxonomy.serves_cats:
        sections = [
            "hero_editorial_showcase",
            "segregated_dog_and_cat_pricing_tabs",
            "grooming_gallery",
            "salon_facility_showcase",
            "cancellation_policy_and_faq",
            "location_hours_and_booking_cta",
        ]
    else:
        sections = [
            "hero_editorial_showcase",
            "services_and_pricing",
            "grooming_gallery",
            "salon_facility_showcase",
            "cancellation_policy_and_faq",
            "location_hours_and_booking_cta",
        ]

    segregated_tabs = taxonomy.serves_dogs and taxonomy.serves_cats
    pricing_tabs: list[str] = []
    if taxonomy.serves_dogs:
        pricing_tabs.append("dogs")
    if taxonomy.serves_cats:
        pricing_tabs.append("cats")

    return WireframeStructure(
        feature_service_radius_map=taxonomy.is_mobile_groomer,
        feature_self_contained_van_badge=taxonomy.is_mobile_groomer,
        instant_call_cta=taxonomy.is_mobile_groomer,
        segregated_dog_cat_pricing_tabs=segregated_tabs,
        pricing_tabs=pricing_tabs,
        ordered_sections=sections,
    )


def _synthesize_editorial_copy(
    business_name: str,
    taxonomy: BusinessTaxonomy,
    wireframe: WireframeStructure,
) -> tuple[str, str, str, list[str]]:
    """Synthesize neutral, fact-derived editorial copy without unverified claims."""
    if taxonomy.is_mobile_groomer:
        headline = f"{business_name} — Mobile Grooming"
        tagline = "Mobile grooming services for pets."
        value_prop = "Mobile pet grooming services."
    elif taxonomy.serves_dogs and taxonomy.serves_cats:
        headline = f"{business_name} — Dog and Cat Grooming"
        tagline = "Grooming services for dogs and cats."
        value_prop = "Grooming services for dogs and cats."
    elif taxonomy.serves_cats:
        headline = f"{business_name} — Cat Grooming"
        tagline = "Grooming services for cats."
        value_prop = "Grooming services for cats."
    elif taxonomy.serves_dogs:
        headline = f"{business_name} — Dog Grooming"
        tagline = "Grooming services for dogs."
        value_prop = "Grooming services for dogs."
    else:
        headline = f"{business_name} — Pet Grooming"
        tagline = "Pet grooming services."
        value_prop = "Pet grooming services."
    return headline, tagline, value_prop, list(wireframe.ordered_sections)


def _resolve_logo_path(
    project_root: Path,
    logo_path: str | Path | None,
    provenance_records: Sequence[AssetProvenanceRecord] | None,
) -> Path | None:
    if logo_path is not None:
        candidate = Path(logo_path)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        if candidate.is_file():
            return candidate

    if provenance_records:
        for rec in provenance_records:
            if "logo" in rec.local_path.lower() or "logo" in rec.asset_id.lower():
                candidate = Path(rec.local_path)
                if not candidate.is_absolute():
                    candidate = project_root / candidate
                if candidate.is_file():
                    return candidate

    for folder in (project_root / "assets" / "website", project_root / "assets" / "optimized"):
        if folder.is_dir():
            for p in sorted(folder.iterdir()):
                if p.is_file() and "logo" in p.name.lower():
                    return p
    return None


def generate_art_direction(
    project_folder: str | Path,
    *,
    logo_path: str | Path | None = None,
    provenance_records: Sequence[AssetProvenanceRecord] | None = None,
    llm_adapter: Any = None,
) -> tuple[BrandIdentity, CreativeBrief]:
    """Generate and persist `design/brand.json` and `design/creative-brief.json`."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name

    facts_path = project_root / "research" / "business-facts.json"
    facts_list: list[dict[str, Any]] = []
    if facts_path.is_file():
        facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
        facts_list = list(facts_payload.get("facts", []))

    business_name, phone, taxonomy = _detect_taxonomy(facts_list, project_id)

    resolved_logo = _resolve_logo_path(project_root, logo_path, provenance_records)
    extracted_colors = extract_colors_from_logo(resolved_logo) if resolved_logo else None

    logo_rel: str | None = None
    if resolved_logo and resolved_logo.is_file():
        try:
            logo_rel = resolved_logo.resolve().relative_to(project_root).as_posix()
        except ValueError:
            logo_rel = resolved_logo.as_posix()

    if extracted_colors is not None:
        primary_hex, secondary_hex, accent_hex = extracted_colors
        brand = BrandIdentity(
            project_id=project_id,
            business_name=business_name,
            logo_path=logo_rel,
            primary_color=primary_hex,
            secondary_color=secondary_hex,
            accent_color=accent_hex,
            palette_names=["extracted_primary", "extracted_secondary", "soft_sage"],
            used_fallback_palette=False,
        )
    else:
        brand = BrandIdentity(
            project_id=project_id,
            business_name=business_name,
            logo_path=logo_rel,
            primary_color=FALLBACK_NAVY_HEX,
            secondary_color=FALLBACK_WARM_AMBER_HEX,
            accent_color=FALLBACK_SOFT_SAGE_HEX,
            palette_names=list(FALLBACK_PALETTE_NAMES),
            used_fallback_palette=True,
        )

    selection_path = project_root / "design" / "image-selection.json"
    hero_filename: str | None = None
    hero_object_pos: str | None = None
    gallery_filenames: list[str] = []
    facility_filename: str | None = None

    if selection_path.is_file():
        sel_data = json.loads(selection_path.read_text(encoding="utf-8"))
        hero_obj = sel_data.get("hero_image")
        if isinstance(hero_obj, dict):
            hero_filename = hero_obj.get("filename")
            hero_object_pos = hero_obj.get("object_position")
        gallery_list = sel_data.get("grooming_gallery", [])
        if isinstance(gallery_list, list):
            gallery_filenames = [
                str(item["filename"])
                for item in gallery_list
                if isinstance(item, dict) and "filename" in item
            ]
        fac_obj = sel_data.get("salon_facility_or_van")
        if isinstance(fac_obj, dict):
            facility_filename = fac_obj.get("filename")

    wireframe = _build_wireframe(taxonomy)
    cta_label = (
        f"Call Now {phone}"
        if (taxonomy.is_mobile_groomer and phone)
        else "Book Grooming Appointment"
    )
    cta_target = f"tel:{re.sub(r'[^0-9+]', '', phone)}" if phone else None

    # Deterministic luxury editorial defaults, overridden when LLM adapter is provided
    (
        default_headline,
        default_tagline,
        default_value_prop,
        default_sections,
    ) = _synthesize_editorial_copy(business_name, taxonomy, wireframe)
    editorial_headline: str | None = default_headline
    editorial_tagline: str | None = default_tagline
    editorial_value_prop: str | None = default_value_prop
    suggested_sections: list[str] = default_sections

    if llm_adapter is not None:
        facts_summary = "\n".join(
            f"- {f.get('category')}: {f.get('claim_value')}"
            for f in facts_list
            if f.get("claim_value")
            and bool(f.get("is_verified", False))
            and str(f.get("status", "verified")) == "verified"
        )
        tax_str = "Mobile Groomer" if taxonomy.is_mobile_groomer else "Grooming Salon"
        serves_str = "Dogs & Cats" if (taxonomy.serves_dogs and taxonomy.serves_cats) else "Dogs"
        prompt = (
            f"Business: {business_name}\n"
            f"Taxonomy: {tax_str}\n"
            f"Serves: {serves_str}\n"
            f"Verified Facts:\n{facts_summary}\n\n"
            "Generate bespoke editorial copywriting for this pet grooming website.\n"
            "Return valid JSON strictly with fields: headline, tagline, value_proposition, "
            "suggested_section_order.\n"
            "Never invent discounts, reviews, or prices not in the verified facts."
        )
        sys_prompt = (
            "You are an elite editorial art director for luxury pet care brands. "
            "Output valid JSON only."
        )
        try:
            resp = llm_adapter.complete(
                prompt=prompt,
                system_prompt=sys_prompt,
                project_id=project_id,
                stage="art_direction",
                response_schema="editorial_brief_v1",
                temperature=0.2,
            )
            data = json.loads(resp.content)
            editorial_headline = (
                data.get("editorial_headline") or data.get("headline") or default_headline
            )
            editorial_tagline = (
                data.get("editorial_tagline") or data.get("tagline") or default_tagline
            )
            editorial_value_prop = (
                data.get("editorial_value_prop")
                or data.get("value_proposition")
                or default_value_prop
            )
            sec_order = data.get("suggested_sections") or data.get("suggested_section_order")
            if isinstance(sec_order, list) and sec_order:
                suggested_sections = [str(s) for s in sec_order]
        except Exception:
            pass

    brief = CreativeBrief(
        project_id=project_id,
        business_name=business_name,
        taxonomy=taxonomy,
        brand=brand,
        wireframe=wireframe,
        hero_image_filename=hero_filename,
        hero_object_position=hero_object_pos,
        gallery_filenames=gallery_filenames,
        facility_or_van_filename=facility_filename,
        primary_cta_label=cta_label,
        primary_cta_target=cta_target,
        editorial_headline=editorial_headline,
        editorial_tagline=editorial_tagline,
        editorial_value_prop=editorial_value_prop,
        suggested_sections=suggested_sections,
    )

    brand_out = project_root / "design" / "brand.json"
    brief_out = project_root / "design" / "creative-brief.json"
    _atomic_write_text(brand_out, brand.model_dump_json(indent=2))
    _atomic_write_text(brief_out, brief.model_dump_json(indent=2))
    return brand, brief
