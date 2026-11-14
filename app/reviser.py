"""Targeted Surgical Revision Engine for EditVista Pet Grooming Website Builder Engine.

Consumes `website/index.html`, `website/css/styles.css`, and `qa/visual-critique.json`.
Enforces the surgical patching rule from `AGENTS.md` and `docs/DEVELOPER_BRIEF.md`:
- Modifies ONLY the specific `<section id="...">` containers flagged in the critique.
- Every untouched sibling `<section id="...">` remains byte-identical (verified via SHA-256).
- Appends timestamped revision entries to `projects/<business-id>/design/change-log.md`.
- Hard-caps automatic critique-revision iterations at `MAX_REVISION_ITERATIONS = 3`,
  escalating to `review_needed` if defects persist.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field

from app.visual_critic import (
    FLAT_BEIGE_TOKENS,
    OBJECT_POS_Y_RE,
    SMALL_PADDING_RE,
    VisualCriticReport,
    VisualDefect,
    run_visual_critic,
)

MAX_REVISION_ITERATIONS = 3
LoopStatus = Literal["passed", "review_needed"]

SECTION_BLOCK_RE = re.compile(
    r"(<section\b[^>]*\bid=(['\"])(?P<id>[^'\"]+)\2[^>]*>[\s\S]*?</section>)",
    re.I,
)
OBJECT_POS_EXTRACT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?%\s+\d{1,3}(?:\.\d+)?%)")


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


def compute_section_hashes(
    html_or_source: str | Path,
    css_source: str | Path | dict[str, str] | None = None,
    *,
    project_folder: str | Path | None = None,
) -> dict[str, str]:
    """Compute SHA-256 digests across sections (data-section based, HTML+CSS)."""
    if isinstance(html_or_source, Path) or (
        isinstance(html_or_source, str)
        and "<" not in html_or_source
        and Path(html_or_source).exists()
    ):
        p = Path(html_or_source).resolve()
        if p.is_dir():
            if project_folder is None:
                project_folder = p
            for cand in (
                "website/index.html",
                "release-candidate/standalone.html",
                "delivery/approved-homepage.html",
                "index.html",
            ):
                if (p / cand).is_file():
                    html_text = (p / cand).read_text(encoding="utf-8")
                    break
            else:
                html_text = ""
        else:
            html_text = p.read_text(encoding="utf-8")
            if project_folder is None:
                project_folder = p.parent
    else:
        html_text = str(html_or_source)

    soup = BeautifulSoup(html_text, "html.parser")
    data_sec_tags = soup.find_all(attrs={"data-section": True})
    raw_sections: dict[str, str] = {}
    if data_sec_tags:
        for t in data_sec_tags:
            sname = str(t["data-section"]).strip()
            if sname:
                raw_sections[sname] = str(t)
    else:
        # Fallback for legacy HTML without data-section attributes
        for sec in soup.find_all("section"):
            sid = sec.get("id")
            if sid and str(sid).strip():
                raw_sections[str(sid).strip()] = str(sec)

    css_map: dict[str, str] = {}
    if isinstance(css_source, dict):
        css_map = {str(k): str(v) for k, v in css_source.items()}
    else:
        css_dir = None
        if project_folder is not None:
            pf = Path(project_folder).resolve()
            for candidate_dir in (pf / "website" / "css", pf / "css", pf):
                if candidate_dir.is_dir():
                    css_dir = candidate_dir
                    break
        if css_dir is not None:
            for sname in raw_sections:
                cfile = css_dir / f"{sname}.css"
                if cfile.is_file():
                    css_map[sname] = cfile.read_text(encoding="utf-8").strip()

        style_text = ""
        if isinstance(css_source, str):
            style_text += "\n" + css_source
        for style_tag in soup.find_all("style"):
            style_text += "\n" + style_tag.get_text()

        if style_text:
            for sname in raw_sections:
                if sname not in css_map:
                    m = re.search(
                        rf"/\*\s*section:\s*{re.escape(sname)}\s*\*/([\s\S]*?)(?:/\*\s*(?:endsection|section):|\Z)",
                        style_text,
                        re.IGNORECASE,
                    )
                    if m:
                        css_map[sname] = m.group(1).strip()

    hashes: dict[str, str] = {}
    for sname, sec_html in raw_sections.items():
        sec_css = css_map.get(sname, "").strip()
        if sec_css:
            combined = f"{sec_html.strip()}\n/* CSS */\n{sec_css}"
        else:
            combined = sec_html.strip()
        hashes[sname] = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    return hashes


class RevisionPatchResult(BaseModel):
    """Result of a single surgical revision pass across flagged sections."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    modified_section_ids: list[str] = Field(default_factory=list)
    untouched_section_ids: list[str] = Field(default_factory=list)
    before_hashes: dict[str, str] = Field(default_factory=dict)
    after_hashes: dict[str, str] = Field(default_factory=dict)
    changelog_path: Path
    patched_at: datetime = Field(default_factory=_utc_now)


class CritiqueRevisionLoopResult(BaseModel):
    """Outcome of the bounded (max 3 iterations) critique-and-revision loop."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    iterations_run: int
    final_status: LoopStatus
    escalated_to_human: bool
    modified_sections_history: list[list[str]] = Field(default_factory=list)


def _patch_single_section_markup(raw_section_html: str, defects: list[VisualDefect]) -> str:
    """Parse and surgically update ONLY the target `<section>` fragment."""
    frag_soup = BeautifulSoup(raw_section_html, "html.parser")
    sec_tag = frag_soup.find("section")
    if not isinstance(sec_tag, Tag):
        return raw_section_html

    applied_types: list[str] = []

    for defect in defects:
        dtype = defect.defect_type.lower()
        instr = defect.actionable_instruction
        applied_types.append(defect.defect_type)

        # 1. Fix cropped pet head / object-position
        if "cropped_pet_head" in dtype or "object-position" in instr.lower():
            pos_match = OBJECT_POS_EXTRACT_RE.search(instr)
            target_pos = pos_match.group(1) if pos_match else "50% 28%"
            for img in sec_tag.find_all("img"):
                if isinstance(img, Tag):
                    style_val = str(img.get("style", ""))
                    if OBJECT_POS_Y_RE.search(style_val):
                        style_val = OBJECT_POS_Y_RE.sub(
                            f"object-position: {target_pos}", style_val
                        )
                    else:
                        stripped = style_val.strip()
                        sep = "; " if stripped and not stripped.endswith(";") else ""
                        style_val = f"{stripped}{sep}object-position: {target_pos};"
                    img["style"] = style_val

        # 2. Fix repetitive card syndrome (flat beige #F5F5DC backgrounds)
        if "repetitive_card" in dtype or "beige" in instr.lower() or "#f5f5dc" in instr.lower():
            for el in [sec_tag, *sec_tag.find_all(True)]:
                if not isinstance(el, Tag):
                    continue
                style_val = str(el.get("style", ""))
                if any(tok in style_val.lower() for tok in FLAT_BEIGE_TOKENS):
                    cleaned_style = re.sub(
                        r"background(?:-color)?\s*:\s*(?:#f5f5dc|#f5f0e6|#ede6d6|#e8dec8|beige)\s*;?",
                        "background-color: var(--ev-surface, #FFFFFF);",
                        style_val,
                        flags=re.I,
                    )
                    el["style"] = cleaned_style.strip()

        # 3. Fix cramped section padding / whitespace rhythm
        if "spacing" in dtype or "padding" in instr.lower() or "rhythm" in dtype:
            sec_style = str(sec_tag.get("style", ""))
            if SMALL_PADDING_RE.search(sec_style):
                sec_style = SMALL_PADDING_RE.sub(
                    "padding: clamp(1.25rem, 2.5vw, 2rem)", sec_style
                )
            else:
                sep = "; " if sec_style.strip() and not sec_style.strip().endswith(";") else ""
                sec_style = f"{sec_style.strip()}{sep}padding: clamp(1.25rem, 2.5vw, 2rem);"
            sec_tag["style"] = sec_style.strip()

        # 4. Fix hero typography contrast or CTA prominence
        if "contrast" in dtype or "cta" in dtype or "typography" in dtype:
            heading = sec_tag.find(["h1", "h2"])
            if isinstance(heading, Tag):
                h_style = str(heading.get("style", ""))
                sep = "; " if h_style.strip() and not h_style.strip().endswith(";") else ""
                heading["style"] = f"{h_style.strip()}{sep}color: var(--ev-primary, #1B365D);"

    sec_tag["data-ev-revised"] = ",".join(dict.fromkeys(applied_types))
    return str(sec_tag)


def _append_changelog(
    changelog_path: Path,
    *,
    timestamp: datetime,
    defects_by_section: dict[str, list[VisualDefect]],
    before_hashes: dict[str, str],
    after_hashes: dict[str, str],
) -> None:
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    existing = changelog_path.read_text(encoding="utf-8") if changelog_path.is_file() else ""
    if not existing:
        existing = "# EditVista Targeted Revision Change Log\n\n"

    lines: list[str] = [
        f"## Revision — {timestamp.isoformat()}",
    ]
    for sec_id, sec_defects in defects_by_section.items():
        b_hash = before_hashes.get(sec_id, "n/a")[:12]
        a_hash = after_hashes.get(sec_id, "n/a")[:12]
        lines.append(f"- **Section `#{sec_id}`** (`{b_hash}` -> `{a_hash}`):")
        for d in sec_defects:
            lines.append(
                f"  - `{d.defect_type}` ({d.screenshot_path}): {d.actionable_instruction}"
            )
    lines.append("")

    updated = existing + "\n".join(lines) + "\n"
    _atomic_write_text(changelog_path, updated)


def apply_targeted_revisions(project_folder: str | Path) -> RevisionPatchResult:
    """Apply surgical DOM & CSS patches to ONLY the sections flagged in visual-critique.json."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name

    index_path = project_root / "website" / "index.html"
    css_path = project_root / "website" / "css" / "styles.css"
    critique_path = project_root / "qa" / "visual-critique.json"
    website_dir = project_root / "website"
    changelog_path = project_root / "design" / "change-log.md"

    if not index_path.is_file():
        raise ValueError(f"Missing website HTML file: {index_path}")
    if not critique_path.is_file():
        raise ValueError(f"Missing visual critique file: {critique_path}")

    critique = VisualCriticReport.model_validate_json(
        critique_path.read_text(encoding="utf-8")
    )

    original_html = index_path.read_text(encoding="utf-8")
    before_hashes = compute_section_hashes(original_html, project_folder=website_dir)

    defects_by_section: dict[str, list[VisualDefect]] = {}
    for defect in critique.defects:
        defects_by_section.setdefault(defect.target_section_id, []).append(defect)

    if not defects_by_section:
        return RevisionPatchResult(
            project_id=project_id,
            modified_section_ids=[],
            untouched_section_ids=list(before_hashes.keys()),
            before_hashes=before_hashes,
            after_hashes=before_hashes,
            changelog_path=changelog_path,
        )

    modified_ids: list[str] = []
    patched_sections_map: dict[str, str] = {}

    def _replace_flagged_section(match: re.Match[str]) -> str:
        raw_block = match.group(1)
        sec_id = match.group("id")
        if sec_id not in defects_by_section:
            # Byte-for-byte preservation of untouched sections
            return raw_block
        patched_block = _patch_single_section_markup(raw_block, defects_by_section[sec_id])
        if patched_block != raw_block:
            modified_ids.append(sec_id)
            patched_sections_map[sec_id] = patched_block
        return patched_block

    updated_html = SECTION_BLOCK_RE.sub(_replace_flagged_section, original_html)

    # Synchronize canonical per-section HTML files under website/sections/<sid>.html
    sections_dir = website_dir / "sections"
    if sections_dir.is_dir():
        for sid, p_html in patched_sections_map.items():
            sec_file = sections_dir / f"{sid}.html"
            if sec_file.is_file():
                _atomic_write_text(sec_file, p_html.strip() + "\n")

    # Append targeted CSS rules scoped strictly to the flagged #section_id selectors
    if css_path.is_file() and modified_ids:
        existing_css = css_path.read_text(encoding="utf-8")
        css_patches: list[str] = []
        for sid in modified_ids:
            rule = (
                f"#{sid} {{ padding: clamp(1.25rem, 2.5vw, 2rem); "
                "background-color: var(--ev-surface, #FFFFFF); }}"
            )
            css_patches.append(rule)
            sec_css_file = website_dir / "css" / f"{sid}.css"
            if sec_css_file.is_file():
                sec_css_text = sec_css_file.read_text(encoding="utf-8")
                if rule not in sec_css_text:
                    _atomic_write_text(sec_css_file, sec_css_text.rstrip() + "\n" + rule + "\n")
        _atomic_write_text(css_path, existing_css.rstrip() + "\n" + "\n".join(css_patches) + "\n")

    after_hashes = compute_section_hashes(updated_html, project_folder=website_dir)

    # Verify invariant: every untouched section's SHA-256 hash MUST remain identical
    untouched_ids = [sid for sid in before_hashes if sid not in defects_by_section]
    for sid in untouched_ids:
        if before_hashes.get(sid) != after_hashes.get(sid):
            raise RuntimeError(f"Surgical revision invariant violated on untouched section #{sid}")

    _atomic_write_text(index_path, updated_html)
    hashes_file = website_dir / "section-hashes.json"
    if hashes_file.is_file() or sections_dir.is_dir():
        _atomic_write_text(hashes_file, json.dumps(after_hashes, indent=2))

    now_ts = _utc_now()
    _append_changelog(
        changelog_path,
        timestamp=now_ts,
        defects_by_section=defects_by_section,
        before_hashes=before_hashes,
        after_hashes=after_hashes,
    )

    return RevisionPatchResult(
        project_id=project_id,
        modified_section_ids=modified_ids,
        untouched_section_ids=untouched_ids,
        before_hashes=before_hashes,
        after_hashes=after_hashes,
        changelog_path=changelog_path,
        patched_at=now_ts,
    )


def run_critique_revision_loop(
    project_folder: str | Path,
    *,
    critic_fn: Callable[..., VisualCriticReport] = run_visual_critic,
    max_iterations: int = MAX_REVISION_ITERATIONS,
    llm_adapter: Any = None,
) -> CritiqueRevisionLoopResult:
    """Execute bounded critique-and-revision loop (hard-capped at 3 iterations)."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name
    capped_limit = min(max(1, max_iterations), MAX_REVISION_ITERATIONS)

    history: list[list[str]] = []
    iterations = 0

    for _ in range(capped_limit):
        iterations += 1
        if critic_fn == run_visual_critic and llm_adapter is not None:
            report = run_visual_critic(project_root, llm_adapter=llm_adapter)
        else:
            report = critic_fn(project_root)
        if report.status == "critic_unavailable":
            return CritiqueRevisionLoopResult(
                project_id=project_id,
                iterations_run=iterations,
                final_status="review_needed",
                escalated_to_human=True,
                modified_sections_history=history,
            )
        if report.status == "passed" or not report.defects:
            return CritiqueRevisionLoopResult(
                project_id=project_id,
                iterations_run=iterations,
                final_status="passed",
                escalated_to_human=False,
                modified_sections_history=history,
            )

        patch_res = apply_targeted_revisions(project_root)
        history.append(patch_res.modified_section_ids)

    # Check if final revision resolved defects without exceeding the 3-iteration cap
    return CritiqueRevisionLoopResult(
        project_id=project_id,
        iterations_run=iterations,
        final_status="review_needed",
        escalated_to_human=True,
        modified_sections_history=history,
    )
