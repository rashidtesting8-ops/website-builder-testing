"""Automated Visual Critic for the EditVista Pet Grooming Website Builder Engine.

Inspects rendered screenshots from `projects/<business-id>/qa/screenshots/`,
`design/creative-brief.json`, `design/brand.json`, and `website/` to evaluate:
1. Hero section typography framing, text contrast against background image, and cropped pet heads.
2. Repetitive card syndrome (flat beige boxes `#F5F5DC` or generic repetitive card styles).
3. Section spacing rhythm, whitespace balance, and CTA prominence.

Outputs `projects/<business-id>/qa/visual-critique.json` with `status: "passed"`,
`"defects_found"`, or `"critic_unavailable"` and specific, actionable defects per
`target_section_id`. Vague instructions such as "make it better" are strictly prohibited by
Pydantic schema validation.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageStat
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from engine.llm.adapter import BudgetExceeded

CritiqueStatus = Literal["passed", "defects_found", "critic_unavailable"]

PROHIBITED_VAGUE_PHRASES: tuple[str, ...] = (
    "make it better",
    "improve design",
    "fix layout",
    "looks bad",
    "make it nicer",
    "more modern",
    "change style",
)

FLAT_BEIGE_TOKENS: tuple[str, ...] = (
    "#f5f5dc",
    "#f5f0e6",
    "#ede6d6",
    "#e8dec8",
    "beige",
)

OBJECT_POS_Y_RE = re.compile(
    r"object-position\s*:\s*\d{1,3}(?:\.\d+)?%\s+(\d{1,3}(?:\.\d+)?)%",
    re.I,
)
SMALL_PADDING_RE = re.compile(r"padding\s*:\s*([0-9]+(?:\.[0-9]+)?)px", re.I)


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


class VisualDefect(BaseModel):
    """Actionable visual defect tied to a specific `<section id='...'>` and screenshot."""

    model_config = ConfigDict(extra="forbid")

    target_section_id: str
    screenshot_path: str
    defect_type: str
    actionable_instruction: str

    @field_validator("target_section_id", "screenshot_path", "defect_type")
    @classmethod
    def _validate_non_empty(cls, v: str, info: Any) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must not be empty")
        return cleaned

    @field_validator("actionable_instruction")
    @classmethod
    def _validate_actionable(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 15:
            raise ValueError(
                "actionable_instruction is too short or vague; provide a concrete CSS/DOM command"
            )
        lower = cleaned.lower()
        for phrase in PROHIBITED_VAGUE_PHRASES:
            if phrase in lower:
                raise ValueError(
                    f"Prohibited vague instruction phrase {phrase!r} detected in critique"
                )
        return cleaned


class VisualCriticReport(BaseModel):
    """Pydantic v2 contract persisted to `projects/<business-id>/qa/visual-critique.json`."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    status: CritiqueStatus
    inspected_screenshots: list[str] = Field(default_factory=list)
    defects: list[VisualDefect] = Field(default_factory=list)
    llm_used: bool = False
    warnings: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=_utc_now)


class VisualCritiqueLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "defects_found"]
    defects: list[VisualDefect] = Field(default_factory=list)


def _inspect_hero_screenshot_contrast(screenshot_file: Path) -> bool:
    """Return True if the top hero fold in the screenshot has low luminance contrast/variance."""
    with Image.open(screenshot_file) as img:
        gray = img.convert("L")
        w, h = gray.size
        hero_band = gray.crop((0, 0, w, max(120, min(h, 360))))
        stat = ImageStat.Stat(hero_band)
        stddev = float(stat.stddev[0]) if stat.stddev else 0.0
        return stddev < 18.0


def _resolve_section_id(section_tag: Tag, index: int) -> str:
    sec_id = section_tag.get("id")
    if isinstance(sec_id, str) and sec_id.strip():
        return sec_id.strip()
    classes = section_tag.get("class")
    if isinstance(classes, list) and any("hero" in str(c) for c in classes):
        return "hero"
    return f"section-{index}"


def run_visual_critic(
    project_folder: str | Path,
    *,
    llm_adapter: Any = None,
) -> VisualCriticReport:
    """Inspect screenshots, brand/brief JSONs, and HTML/CSS to produce visual-critique.json."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name

    screenshots_dir = project_root / "qa" / "screenshots"
    screenshot_files: list[Path] = []
    if screenshots_dir.is_dir():
        screenshot_files = sorted(
            p for p in screenshots_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"
        )

    inspected_rels = [p.relative_to(project_root).as_posix() for p in screenshot_files]
    primary_shot_rel = (
        inspected_rels[0] if inspected_rels else "qa/screenshots/1440px.png"
    )

    brief_path = project_root / "design" / "creative-brief.json"
    brief_data: dict[str, Any] = {}
    if brief_path.is_file():
        brief_data = json.loads(brief_path.read_text(encoding="utf-8"))

    brand_path = project_root / "design" / "brand.json"
    brand_data: dict[str, Any] = {}
    if brand_path.is_file():
        brand_data = json.loads(brand_path.read_text(encoding="utf-8"))

    primary_color = str(brand_data.get("primary_color", "#1B365D"))
    expected_hero_pos = str(brief_data.get("hero_object_position") or "50% 28%")

    index_path = project_root / "website" / "index.html"
    css_path = project_root / "website" / "css" / "styles.css"
    html_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    css_text = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""

    defects: list[VisualDefect] = []

    # 1. Screenshot-based hero contrast & framing check
    low_hero_contrast = False
    for shot_file in screenshot_files:
        if "1440" in shot_file.name or shot_file == screenshot_files[0]:
            if _inspect_hero_screenshot_contrast(shot_file):
                low_hero_contrast = True
            break

    if html_text:
        soup = BeautifulSoup(html_text, "html.parser")
        sections = soup.find_all("section")

        for idx, sec in enumerate(sections, start=1):
            if not isinstance(sec, Tag):
                continue
            sec_id = _resolve_section_id(sec, idx)
            sec_html_lower = str(sec).lower()
            sec_style = str(sec.get("style", ""))

            # Check A: Hero framing, contrast, cropped pet head, or missing CTA
            is_hero = sec_id == "hero" or "ev-hero" in " ".join(sec.get("class", []))
            if is_hero:
                if low_hero_contrast:
                    defects.append(
                        VisualDefect(
                            target_section_id=sec_id,
                            screenshot_path=primary_shot_rel,
                            defect_type="hero_typography_contrast",
                            actionable_instruction=(
                                f"Enforce WCAG AA text contrast on #{sec_id} using brand primary "
                                f"{primary_color} and high-contrast surface framing."
                            ),
                        )
                    )

                hero_img = sec.find("img")
                if isinstance(hero_img, Tag):
                    img_style = str(hero_img.get("style", ""))
                    match_y = OBJECT_POS_Y_RE.search(img_style)
                    if match_y:
                        y_val = float(match_y.group(1))
                        if y_val < 10.0 or y_val > 75.0:
                            defects.append(
                                VisualDefect(
                                    target_section_id=sec_id,
                                    screenshot_path=primary_shot_rel,
                                    defect_type="cropped_pet_head",
                                    actionable_instruction=(
                                        f"Update hero image object-position in #{sec_id} to "
                                        f"{expected_hero_pos} so the pet head is not clipped."
                                    ),
                                )
                            )

                cta_el = sec.select_one("a.cta, a.btn, button")
                if cta_el is None:
                    defects.append(
                        VisualDefect(
                            target_section_id=sec_id,
                            screenshot_path=primary_shot_rel,
                            defect_type="cta_prominence",
                            actionable_instruction=(
                                f"Add a high-prominence primary booking CTA inside #{sec_id} "
                                f"with background-color {primary_color} and #FFFFFF text."
                            ),
                        )
                    )

            # Check B: Repetitive card syndrome (flat beige boxes #F5F5DC)
            has_flat_beige = any(tok in sec_html_lower for tok in FLAT_BEIGE_TOKENS) or (
                f"#{sec_id}" in css_text.lower()
                and any(tok in css_text.lower() for tok in FLAT_BEIGE_TOKENS)
            )
            if has_flat_beige:
                defects.append(
                    VisualDefect(
                        target_section_id=sec_id,
                        screenshot_path=primary_shot_rel,
                        defect_type="repetitive_card_syndrome",
                        actionable_instruction=(
                            f"Replace flat beige (#F5F5DC) card backgrounds in #{sec_id} with "
                            "clean surface tokens (--ev-bg / #FFFFFF) and brand accent borders."
                        ),
                    )
                )

            # Check C: Section spacing rhythm & cramped padding
            pad_match = SMALL_PADDING_RE.search(sec_style)
            if pad_match and float(pad_match.group(1)) < 16.0:
                defects.append(
                    VisualDefect(
                        target_section_id=sec_id,
                        screenshot_path=primary_shot_rel,
                        defect_type="spacing_and_whitespace_rhythm",
                        actionable_instruction=(
                            f"Increase padding on #{sec_id} to clamp(1.25rem, 2.5vw, 2rem) and "
                            "restore vertical whitespace rhythm."
                        ),
                    )
                )

    # Multimodal LLM visual critique on rendered screenshots
    llm_used = False
    llm_unavailable = False
    warnings: list[str] = []
    if llm_adapter is not None and not screenshot_files:
        llm_unavailable = True
        warnings.append("Visual critic LLM call skipped: no screenshots are available.")
    elif llm_adapter is not None:
        critic_prompt = (
            f"Project: {project_id}\n"
            f"Primary Brand Color: {primary_color}\n"
            "Inspect the rendered homepage screenshot(s) and review for visual design defects:\n"
            "- Flat beige or monotonous card styles\n"
            "- Poor typography contrast or illegible overlay on pet photos\n"
            "- Clipped pet heads or awkward focal framing\n"
            "- Cramped section padding or inconsistent whitespace rhythm\n\n"
            "Return valid JSON strictly matching:\n"
            '{"status": "defects_found" | "passed", "defects": [{"target_section_id": "...", '
            '"screenshot_path": "...", "defect_type": "...", "actionable_instruction": "..."}]}\n'
            "Instructions must be concrete and actionable CSS/DOM commands (>= 15 characters). "
            "Never use vague phrases like 'make it better'."
        )
        sys_prompt = (
            "You are a strict visual QA design critic for pet grooming websites. "
            "Output valid JSON only."
        )
        try:
            shot_images = [p for p in screenshot_files[:2]]
            resp = llm_adapter.complete(
                prompt=critic_prompt,
                system_prompt=sys_prompt,
                images=shot_images,
                project_id=project_id,
                stage="visual_critic",
                response_schema="visual_critique_v1",
                temperature=0.1,
            )
            llm_data = VisualCritiqueLLMResponse.model_validate_json(resp.content)
            defects.extend(llm_data.defects)
            llm_used = True
        except BudgetExceeded:
            raise
        except (RuntimeError, ValidationError) as exc:
            llm_unavailable = True
            warnings.append(f"Visual critic LLM call failed: {exc}")

    status: CritiqueStatus = (
        "critic_unavailable"
        if llm_unavailable
        else "defects_found"
        if defects
        else "passed"
    )
    report = VisualCriticReport(
        project_id=project_id,
        status=status,
        inspected_screenshots=inspected_rels,
        defects=defects,
        llm_used=llm_used,
        warnings=warnings,
    )
    out_path = project_root / "qa" / "visual-critique.json"
    _atomic_write_text(out_path, report.model_dump_json(indent=2))
    return report
