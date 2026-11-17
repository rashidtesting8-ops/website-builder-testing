"""Visual Curator for the EditVista Pet Grooming Website Builder Engine.

Scans `assets/optimized/` and `assets/thumbnails/contact-sheet.jpg` with Pillow,
calculates aspect ratios, resolutions, sharpness, embedded text / promotional banner
flags, and focal point crop coordinates (`object-position: X% Y%`). Integrates with
a pluggable LVM adapter (or deterministic Pillow heuristics) and saves ranked,
strictly deduplicated section assignments to `projects/<business-id>/design/image-selection.json`.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, ImageFilter, ImageStat
from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_HERO_WIDTH = 800
MIN_GALLERY_COUNT = 4
MIN_SHARPNESS_SCORE = 0.08
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PERCEPTUAL_HASH_MAX_DISTANCE = 5  # 64-bit dHash; <=5 bits ~ visually identical


def _perceptual_hash(img: Image.Image) -> int:
    """64-bit difference hash (Pillow only, no dependency)."""
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits = (bits << 1) | (1 if pixels[row + x] > pixels[row + x + 1] else 0)
    return bits


def _hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()

CSS_PERCENT_RE = re.compile(r"^\d{1,3}(?:\.\d+)?%$")
CSS_OBJECT_POSITION_RE = re.compile(r"^\d{1,3}(?:\.\d+)?%\s+\d{1,3}(?:\.\d+)?%$")

PROMO_FILENAME_TOKENS = (
    "promo",
    "banner",
    "discount",
    "coupon",
    "flyer",
    "sale",
    "offer",
    "text",
)
FACILITY_FILENAME_TOKENS = (
    "salon",
    "facility",
    "van",
    "mobile",
    "interior",
    "storefront",
    "shop",
    "truck",
)

RoleHint = Literal["hero", "gallery", "facility_or_van", "exclude"]


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


def _format_css_pct(value: float) -> str:
    clamped = max(0.0, min(100.0, round(value, 1)))
    if clamped.is_integer():
        return f"{int(clamped)}%"
    return f"{clamped:.1f}%"


class CuratorHeuristicObservation(BaseModel):
    """Observation returned by an LVM adapter or deterministic Pillow visual analyzer."""

    model_config = ConfigDict(extra="forbid")

    pet_visible: bool = True
    has_embedded_text: bool = False
    sharpness_score: float = Field(default=0.75, ge=0.0, le=1.0)
    role_hint: RoleHint = "gallery"
    focal_x_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    focal_y_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    notes: str = ""


LVMAdapter = Callable[[Path, Image.Image], CuratorHeuristicObservation]


class CuratedImageCandidate(BaseModel):
    """Ranked, provenance-linked image candidate with CSS focal coordinates."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    relative_path: str
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    aspect_ratio: float = Field(gt=0.0)
    sharpness_score: float = Field(ge=0.0, le=1.0)
    pet_visible: bool
    has_embedded_text: bool
    low_res_for_hero: bool
    focal_point_x_pct: float = Field(ge=0.0, le=100.0)
    focal_point_y_pct: float = Field(ge=0.0, le=100.0)
    focal_x: str
    focal_y: str
    object_position: str
    rank_score: float

    @field_validator("focal_x", "focal_y")
    @classmethod
    def _validate_css_pct(cls, v: str) -> str:
        if not CSS_PERCENT_RE.match(v):
            raise ValueError(f"Invalid CSS percentage coordinate: {v}")
        numeric = float(v[:-1])
        if not (0.0 <= numeric <= 100.0):
            raise ValueError(f"CSS percentage out of bounds [0, 100]: {v}")
        return v

    @field_validator("object_position")
    @classmethod
    def _validate_object_position(cls, v: str) -> str:
        if not CSS_OBJECT_POSITION_RE.match(v):
            raise ValueError(f"Invalid CSS object-position value: {v}")
        return v


class ExcludedImageRecord(BaseModel):
    """Image excluded from placement with explicit failure reasons."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    relative_path: str
    width: int
    height: int
    reasons: list[str]


class ImageSelectionReport(BaseModel):
    """Output contract persisted to `projects/<business-id>/design/image-selection.json`."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    curated_at: datetime = Field(default_factory=_utc_now)
    contact_sheet_inspected: bool = False
    contact_sheet_path: str | None = None
    hero_image: CuratedImageCandidate | None = None
    grooming_gallery: list[CuratedImageCandidate] = Field(default_factory=list)
    salon_facility_or_van: CuratedImageCandidate | None = None
    excluded_images: list[ExcludedImageRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _compute_sharpness(img: Image.Image) -> float:
    """Estimate normalized sharpness [0.0, 1.0] from edge gradient variance."""
    gray = img.convert("L").resize((256, 256))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    stat = ImageStat.Stat(edges)
    stddev = float(stat.stddev[0]) if stat.stddev else 0.0
    return round(max(0.0, min(1.0, stddev / 64.0)), 4)


def _detect_embedded_banner_text(image_path: Path, img: Image.Image) -> bool:
    """Detect baked-in promotional text or banners via filename and top strip stroke density."""
    lower_name = image_path.stem.lower()
    if any(tok in lower_name for tok in PROMO_FILENAME_TOKENS):
        return True

    w, h = img.size
    if w < 64 or h < 64:
        return False

    band_h = max(16, h // 6)
    top_band = img.crop((0, 0, w, band_h)).convert("L")
    edges = top_band.filter(ImageFilter.FIND_EDGES)
    edge_mean = float(ImageStat.Stat(edges).mean[0])
    top_stddev = float(ImageStat.Stat(top_band).stddev[0])
    # High-contrast alternating text strokes inside a top banner produce extreme edge density
    return edge_mean > 85.0 and top_stddev > 75.0


def _calculate_focal_point(img: Image.Image) -> tuple[float, float]:
    """Calculate subject focal centroid (x%, y%) biased upper-center to avoid clipping faces."""
    small = img.convert("L").resize((64, 64))
    edges = small.filter(ImageFilter.FIND_EDGES)
    pixels = edges.load()
    if pixels is None:
        return 50.0, 30.0

    total_weight = 0.0
    weighted_x = 0.0
    weighted_y = 0.0

    for y in range(64):
        # Upper-half bias (pet heads/eyes usually sit in the upper-middle third)
        upper_bias = 1.25 if 6 <= y <= 40 else 0.85
        for x in range(64):
            center_bias = 1.15 if 12 <= x <= 52 else 0.85
            val = float(pixels[x, y])
            if val < 25.0:
                continue
            w = val * upper_bias * center_bias
            total_weight += w
            weighted_x += x * w
            weighted_y += y * w

    if total_weight <= 0.0:
        return 50.0, 30.0

    cx = (weighted_x / total_weight) / 63.0 * 100.0
    cy = (weighted_y / total_weight) / 63.0 * 100.0
    # Clamp within safe framing window [15%, 85%] horizontally and [20%, 65%] vertically
    safe_x = round(max(15.0, min(85.0, cx)), 1)
    safe_y = round(max(20.0, min(65.0, cy)), 1)
    return safe_x, safe_y


def default_lvm_heuristic_adapter(
    image_path: Path,
    img: Image.Image,
) -> CuratorHeuristicObservation:
    """Deterministic Pillow visual evaluator used when no external LVM adapter is injected."""
    sharpness = _compute_sharpness(img)
    has_text = _detect_embedded_banner_text(image_path, img)
    fx, fy = _calculate_focal_point(img)
    lower_name = image_path.stem.lower()

    is_facility = any(tok in lower_name for tok in FACILITY_FILENAME_TOKENS)
    role_hint: RoleHint = "gallery"
    if has_text:
        role_hint = "exclude"
    elif is_facility:
        role_hint = "facility_or_van"
    elif "hero" in lower_name or img.width >= 1200:
        role_hint = "hero"

    return CuratorHeuristicObservation(
        pet_visible=not is_facility,
        has_embedded_text=has_text,
        sharpness_score=sharpness,
        role_hint=role_hint,
        focal_x_pct=fx,
        focal_y_pct=fy,
        notes="Evaluated via deterministic Pillow visual heuristic adapter",
    )


def curate_project_images(
    project_folder: str | Path,
    *,
    lvm_adapter: LVMAdapter | None = None,
) -> ImageSelectionReport:
    """Scan optimized assets and contact sheet, evaluate candidates, and save selection JSON."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name
    adapter = lvm_adapter or default_lvm_heuristic_adapter

    optimized_dir = project_root / "assets" / "optimized"
    contact_sheet_file = project_root / "qa" / "contact-sheet.jpg"

    contact_sheet_inspected = False
    contact_sheet_rel: str | None = None
    if contact_sheet_file.is_file():
        with Image.open(contact_sheet_file) as cs_img:
            cs_img.verify()
        contact_sheet_inspected = True
        contact_sheet_rel = contact_sheet_file.relative_to(project_root).as_posix()

    image_files: list[Path] = []
    if optimized_dir.is_dir():
        image_files = sorted(
            p
            for p in optimized_dir.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    valid_candidates: list[tuple[CuratedImageCandidate, RoleHint]] = []
    excluded: list[ExcludedImageRecord] = []
    seen_sha256: set[str] = set()
    seen_phashes: list[int] = []

    for img_path in image_files:
        rel_path = img_path.relative_to(project_root).as_posix()
        raw_bytes = img_path.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        with Image.open(img_path) as pil_img:
            rgb_img = pil_img.convert("RGB")
            width, height = rgb_img.size
            aspect_ratio = round(width / max(1, height), 4)
            obs = adapter(img_path, rgb_img)

        low_res_for_hero = width < MIN_HERO_WIDTH
        failure_reasons: list[str] = []

        if sha256 in seen_sha256:
            failure_reasons.append("duplicate_sha256: identical image content already evaluated")
        phash = _perceptual_hash(rgb_img)
        if any(
            _hamming_distance(phash, prev) <= PERCEPTUAL_HASH_MAX_DISTANCE
            for prev in seen_phashes
        ):
            failure_reasons.append(
                "duplicate_perceptual: visually identical image already evaluated"
            )
        if obs.has_embedded_text:
            failure_reasons.append(
                "embedded_text_or_promotional_banner: baked-in text or discount graphic detected"
            )
        if low_res_for_hero:
            failure_reasons.append(
                f"low_resolution: width {width}px is below {MIN_HERO_WIDTH}px threshold"
            )
        if obs.sharpness_score < MIN_SHARPNESS_SCORE:
            failure_reasons.append(
                f"excessive_blur: sharpness {obs.sharpness_score:.2f} below {MIN_SHARPNESS_SCORE}"
            )

        if failure_reasons:
            excluded.append(
                ExcludedImageRecord(
                    filename=img_path.name,
                    relative_path=rel_path,
                    width=width,
                    height=height,
                    reasons=failure_reasons,
                )
            )
            continue

        seen_sha256.add(sha256)
        seen_phashes.append(phash)
        focal_x = _format_css_pct(obs.focal_x_pct)
        focal_y = _format_css_pct(obs.focal_y_pct)
        obj_pos = f"{focal_x} {focal_y}"

        # Rank score combines sharpness, landscape suitability, resolution, and pet visibility
        aspect_bonus = 0.15 if 1.2 <= aspect_ratio <= 2.0 else 0.05
        res_bonus = min(0.25, (width / 2000.0) * 0.25)
        pet_bonus = 0.20 if obs.pet_visible else 0.05
        rank_score = round(obs.sharpness_score * 0.55 + aspect_bonus + res_bonus + pet_bonus, 4)

        candidate = CuratedImageCandidate(
            filename=img_path.name,
            relative_path=rel_path,
            sha256=sha256,
            width=width,
            height=height,
            aspect_ratio=aspect_ratio,
            sharpness_score=obs.sharpness_score,
            pet_visible=obs.pet_visible,
            has_embedded_text=obs.has_embedded_text,
            low_res_for_hero=low_res_for_hero,
            focal_point_x_pct=round(obs.focal_x_pct, 1),
            focal_point_y_pct=round(obs.focal_y_pct, 1),
            focal_x=focal_x,
            focal_y=focal_y,
            object_position=obj_pos,
            rank_score=rank_score,
        )
        valid_candidates.append((candidate, obs.role_hint))

    # Strict deduplication across sections: each filename / sha256 can only be assigned once
    assigned_filenames: set[str] = set()
    warnings: list[str] = []

    # 1. Select salon_facility_or_van first if explicitly hinted
    facility_candidate: CuratedImageCandidate | None = None
    facility_pool = sorted(
        (c for c, hint in valid_candidates if hint == "facility_or_van"),
        key=lambda item: item.rank_score,
        reverse=True,
    )
    if facility_pool:
        facility_candidate = facility_pool[0]
        assigned_filenames.add(facility_candidate.filename)

    # 2. Select hero_image (must not be low_res_for_hero, not embedded text, not already assigned)
    hero_candidate: CuratedImageCandidate | None = None
    hero_pool = sorted(
        (
            c
            for c, hint in valid_candidates
            if c.filename not in assigned_filenames
            and not c.low_res_for_hero
            and not c.has_embedded_text
            and c.pet_visible
            and "logo" not in c.filename.lower()
        ),
        key=lambda item: (
            1 if "hero" in item.filename.lower() else 0,
            item.rank_score,
        ),
        reverse=True,
    )
    if hero_pool:
        hero_candidate = hero_pool[0]
        assigned_filenames.add(hero_candidate.filename)
    else:
        warnings.append("No eligible hero_image candidate >= 800px without embedded text found.")

    # 3. Select grooming_gallery (minimum 4 unique verified photos, never reusing hero or facility)
    gallery_pool = sorted(
        (
            c
            for c, _ in valid_candidates
            if c.filename not in assigned_filenames and not c.has_embedded_text
        ),
        key=lambda item: (1 if item.pet_visible else 0, item.rank_score),
        reverse=True,
    )
    grooming_gallery: list[CuratedImageCandidate] = []
    for cand in gallery_pool:
        if cand.filename not in assigned_filenames:
            grooming_gallery.append(cand)
            assigned_filenames.add(cand.filename)

    # 4. If facility was not explicitly hinted and > 4 gallery photos exist, assign last to facility
    if facility_candidate is None and len(grooming_gallery) > MIN_GALLERY_COUNT:
        facility_candidate = grooming_gallery.pop()

    if len(grooming_gallery) < MIN_GALLERY_COUNT:
        warnings.append(
            f"grooming_gallery contains {len(grooming_gallery)} unique verified photos "
            f"(minimum {MIN_GALLERY_COUNT} recommended; status needs_review)."
        )

    report = ImageSelectionReport(
        project_id=project_id,
        contact_sheet_inspected=contact_sheet_inspected,
        contact_sheet_path=contact_sheet_rel,
        hero_image=hero_candidate,
        grooming_gallery=grooming_gallery,
        salon_facility_or_van=facility_candidate,
        excluded_images=excluded,
        warnings=warnings,
    )

    out_path = project_root / "design" / "image-selection.json"
    _atomic_write_text(out_path, report.model_dump_json(indent=2))
    return report
