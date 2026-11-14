"""Unit tests for Visual Critic (app/visual_critic.py) and Targeted Reviser (app/reviser.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.reviser import (
    MAX_REVISION_ITERATIONS,
    apply_targeted_revisions,
    compute_section_hashes,
    run_critique_revision_loop,
)
from app.visual_critic import (
    VisualCriticReport,
    VisualDefect,
    run_visual_critic,
)
from PIL import Image, ImageDraw

from engine.llm.adapter import BudgetExceeded


def _create_screenshot(
    path: Path,
    *,
    width: int = 1440,
    height: int = 900,
    low_contrast_hero: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=(248, 250, 252))
    draw = ImageDraw.Draw(img)
    if low_contrast_hero:
        # Very low-variance washed-out beige block in hero region
        draw.rectangle((0, 0, width, 320), fill=(238, 232, 218))
    else:
        draw.rectangle((0, 0, width, 320), fill=(27, 54, 93))
        draw.rectangle((80, 60, 640, 240), fill=(255, 255, 255))
    img.save(path, format="PNG")
    return path


MOCK_DEFECTIVE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Paws &amp; Polish Grooming</title>
  <link rel="stylesheet" href="css/styles.css">
</head>
<body>
  <header class="ev-header">
    <nav class="ev-nav"><a href="#top">Paws &amp; Polish</a></nav>
  </header>
  <main id="top" class="ev-main">
    <section id="hero" class="ev-hero">
      <div>
        <h1 class="ev-hero-title">Paws &amp; Polish</h1>
        <a class="cta btn" href="#contact">Book Now</a>
      </div>
      <div class="ev-hero-media">
        <img class="ev-hero-img" src="../assets/optimized/hero.jpg"
          alt="Hero Dog" style="object-position: 50% 2%;">
      </div>
    </section>
    <section id="services" class="ev-section" style="background-color: #F5F5DC; padding: 4px;">
      <h2 class="ev-section-title">Grooming Services</h2>
      <div class="ev-grid">
        <div class="ev-card" style="background-color: #F5F5DC;">Full Groom</div>
        <div class="ev-card" style="background-color: #F5F5DC;">Bath &amp; Brush</div>
        <div class="ev-card" style="background-color: #F5F5DC;">Nail Trim</div>
        <div class="ev-card" style="background-color: #F5F5DC;">Puppy Intro</div>
      </div>
    </section>
    <section id="gallery" class="ev-section">
      <h2 class="ev-section-title">Portfolio Gallery</h2>
      <div class="ev-grid">
        <div class="ev-card"><img src="../assets/optimized/g1.jpg" alt="Dog 1"></div>
      </div>
    </section>
    <section id="contact" class="ev-section">
      <h2 class="ev-section-title">Contact Us</h2>
      <p>742 Evergreen Terrace, Austin, TX 78701</p>
    </section>
  </main>
</body>
</html>
"""


def test_visual_critic_detects_actionable_defects_and_rejects_vague_feedback(
    tmp_path: Path,
) -> None:
    """Visual Critic must output structured, actionable defects and forbid vague instructions."""
    project_root = tmp_path / "projects" / "paws-polish"
    (project_root / "website" / "css").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)
    (project_root / "qa" / "screenshots").mkdir(parents=True, exist_ok=True)

    (project_root / "website" / "index.html").write_text(MOCK_DEFECTIVE_HTML, encoding="utf-8")
    (project_root / "website" / "css" / "styles.css").write_text(
        ":root { --ev-primary: #1B365D; }\n#services { padding: 4px; background: #F5F5DC; }\n",
        encoding="utf-8",
    )
    (project_root / "design" / "brand.json").write_text(
        json.dumps(
            {
                "project_id": "paws-polish",
                "business_name": "Paws & Polish",
                "primary_color": "#1B365D",
                "secondary_color": "#D98A2B",
                "accent_color": "#8FA998",
            }
        ),
        encoding="utf-8",
    )
    (project_root / "design" / "creative-brief.json").write_text(
        json.dumps(
            {
                "project_id": "paws-polish",
                "business_name": "Paws & Polish",
                "hero_object_position": "50% 28%",
            }
        ),
        encoding="utf-8",
    )

    _create_screenshot(
        project_root / "qa" / "screenshots" / "1440px.png",
        low_contrast_hero=True,
    )

    report = run_visual_critic(project_root)

    critique_path = project_root / "qa" / "visual-critique.json"
    assert critique_path.is_file()
    assert report.status == "defects_found"
    assert len(report.defects) >= 2

    target_ids = {d.target_section_id for d in report.defects}
    assert "hero" in target_ids
    assert "services" in target_ids

    for defect in report.defects:
        assert defect.screenshot_path.endswith(".png")
        assert len(defect.actionable_instruction) >= 15
        assert "make it better" not in defect.actionable_instruction.lower()

    # Verify Pydantic validation rejects vague instructions like "make it better"
    with pytest.raises(ValueError, match="vague"):
        VisualDefect(
            target_section_id="services",
            screenshot_path="qa/screenshots/1440px.png",
            defect_type="repetitive_card_syndrome",
            actionable_instruction="Please make it better",
        )


class _RaisingCompletionAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, **_kwargs: object) -> object:
        raise self.error


def test_critic_propagates_budget_exceeded(tmp_path: Path) -> None:
    project_root = tmp_path / "budget-limited-critic"
    _create_screenshot(project_root / "qa" / "screenshots" / "1440px.png")

    with pytest.raises(BudgetExceeded, match="call cap"):
        run_visual_critic(
            project_root,
            llm_adapter=_RaisingCompletionAdapter(BudgetExceeded("call cap reached")),
        )


def test_critic_unavailable_when_llm_fails_even_if_heuristics_clean(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "unavailable-critic"
    _create_screenshot(project_root / "qa" / "screenshots" / "1440px.png")

    report = run_visual_critic(
        project_root,
        llm_adapter=_RaisingCompletionAdapter(RuntimeError("provider unavailable")),
    )
    saved_report = json.loads(
        (project_root / "qa" / "visual-critique.json").read_text(encoding="utf-8")
    )

    assert report.defects == []
    assert report.status == "critic_unavailable"
    assert report.llm_used is False
    assert any("provider unavailable" in warning for warning in report.warnings)
    assert saved_report["llm_used"] is False
    assert saved_report["warnings"] == report.warnings


def test_critic_without_adapter_marks_llm_used_false(tmp_path: Path) -> None:
    project_root = tmp_path / "deterministic-only-critic"
    _create_screenshot(project_root / "qa" / "screenshots" / "1440px.png")

    report = run_visual_critic(project_root)

    assert report.status == "passed"
    assert report.llm_used is False
    assert report.warnings == []


def test_revision_loop_escalates_when_critic_is_unavailable(tmp_path: Path) -> None:
    report = VisualCriticReport(
        project_id="unavailable-loop",
        status="critic_unavailable",
        llm_used=False,
        warnings=["provider unavailable"],
    )

    result = run_critique_revision_loop(
        tmp_path / "unavailable-loop",
        critic_fn=lambda _project_folder: report,
    )

    assert result.final_status == "review_needed"
    assert result.escalated_to_human is True
    assert result.iterations_run == 1


def test_pipeline_returns_needs_review_when_critic_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.pipeline as pipeline_module
    from app.pipeline import run_pipeline
    from tests.test_pipeline_e2e import _ephemeral_grooming_salon_server
    from tests.test_pipeline_lifecycle import _fast_qa_runner

    unavailable_report = VisualCriticReport(
        project_id="unavailable-pipeline",
        status="critic_unavailable",
        llm_used=False,
        warnings=["provider unavailable"],
    )
    monkeypatch.setattr(
        pipeline_module,
        "run_visual_critic",
        lambda _project_folder, **_kwargs: unavailable_report,
    )

    with _ephemeral_grooming_salon_server() as target_url:
        result = run_pipeline(
            url=target_url,
            business_name="Bark & Bliss Pet Grooming Salon",
            business_id="unavailable-pipeline",
            projects_dir=tmp_path / "projects",
            db_path=tmp_path / "projects" / "editvista.sqlite3",
            allow_private=True,
            qa_runner=_fast_qa_runner,
        )

    assert result.status == "needs_review"
    assert result.qa_passed is True
    assert any("critic unavailable" in error.lower() for error in result.errors)


def test_reviser_modifies_only_targeted_section_leaving_siblings_byte_identical(
    tmp_path: Path,
) -> None:
    """Reviser surgically patches only the flagged <section id='...'> and updates change-log.md."""
    project_root = tmp_path / "projects" / "surgical-test"
    (project_root / "website" / "css").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)
    (project_root / "qa" / "screenshots").mkdir(parents=True, exist_ok=True)

    index_file = project_root / "website" / "index.html"
    css_file = project_root / "website" / "css" / "styles.css"
    index_file.write_text(MOCK_DEFECTIVE_HTML, encoding="utf-8")
    css_file.write_text(":root { --ev-primary: #1B365D; }\n", encoding="utf-8")

    before_hashes = compute_section_hashes(index_file.read_text(encoding="utf-8"))
    assert set(before_hashes.keys()) == {"hero", "services", "gallery", "contact"}

    # Critique targeting ONLY #services
    critique = VisualCriticReport(
        project_id="surgical-test",
        status="defects_found",
        defects=[
            VisualDefect(
                target_section_id="services",
                screenshot_path="qa/screenshots/1440px.png",
                defect_type="repetitive_card_syndrome",
                actionable_instruction=(
                    "Increase padding on #services to clamp(1.5rem, 3vw, 2.5rem), remove beige "
                    "#F5F5DC backgrounds, and adjust font contrast to WCAG AA."
                ),
            )
        ],
    )
    critique_path = project_root / "qa" / "visual-critique.json"
    critique_path.write_text(critique.model_dump_json(indent=2), encoding="utf-8")

    patch_result = apply_targeted_revisions(project_root)

    after_html = index_file.read_text(encoding="utf-8")
    after_hashes = compute_section_hashes(after_html)

    # #services MUST be modified
    assert "services" in patch_result.modified_section_ids
    assert after_hashes["services"] != before_hashes["services"]

    # Sibling sections (#hero, #gallery, #contact) MUST remain byte-identical
    assert after_hashes["hero"] == before_hashes["hero"]
    assert after_hashes["gallery"] == before_hashes["gallery"]
    assert after_hashes["contact"] == before_hashes["contact"]

    # Verify change-log.md is created and appended incrementally on a second patch
    changelog_path = project_root / "design" / "change-log.md"
    assert changelog_path.is_file()
    first_log = changelog_path.read_text(encoding="utf-8")
    assert "services" in first_log

    # Second critique targeting ONLY #hero
    critique_hero = VisualCriticReport(
        project_id="surgical-test",
        status="defects_found",
        defects=[
            VisualDefect(
                target_section_id="hero",
                screenshot_path="qa/screenshots/1440px.png",
                defect_type="cropped_pet_head",
                actionable_instruction=(
                    "Adjust hero image object-position on #hero to 50% 28% and enforce WCAG AA "
                    "typography framing."
                ),
            )
        ],
    )
    critique_path.write_text(critique_hero.model_dump_json(indent=2), encoding="utf-8")
    apply_targeted_revisions(project_root)

    second_log = changelog_path.read_text(encoding="utf-8")
    assert len(second_log) > len(first_log)
    assert "hero" in second_log

    after_second_hashes = compute_section_hashes(index_file.read_text(encoding="utf-8"))
    # #services, #gallery, #contact must remain unchanged from before the second patch
    assert after_second_hashes["services"] == after_hashes["services"]
    assert after_second_hashes["gallery"] == before_hashes["gallery"]
    assert after_second_hashes["contact"] == before_hashes["contact"]
    assert hashlib.sha256(b"test").hexdigest()


def test_reviser_hard_caps_loop_at_3_iterations_and_escalates_to_review_needed(
    tmp_path: Path,
) -> None:
    """Automatic critique-revision loop must stop at 3 iterations and return review_needed."""
    project_root = tmp_path / "projects" / "stubborn-defect"
    (project_root / "website" / "css").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)
    (project_root / "qa" / "screenshots").mkdir(parents=True, exist_ok=True)

    (project_root / "website" / "index.html").write_text(MOCK_DEFECTIVE_HTML, encoding="utf-8")
    (project_root / "website" / "css" / "styles.css").write_text(
        ":root { --ev-primary: #1B365D; }\n",
        encoding="utf-8",
    )

    call_count = 0

    def always_failing_critic(_folder: str | Path) -> VisualCriticReport:
        nonlocal call_count
        call_count += 1
        rep = VisualCriticReport(
            project_id="stubborn-defect",
            status="defects_found",
            defects=[
                VisualDefect(
                    target_section_id="services",
                    screenshot_path="qa/screenshots/1440px.png",
                    defect_type="spacing_and_whitespace_rhythm",
                    actionable_instruction=(
                        "Increase padding on #services to 2.5rem and restore vertical rhythm."
                    ),
                )
            ],
        )
        (Path(_folder) / "qa" / "visual-critique.json").write_text(
            rep.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return rep

    loop_result = run_critique_revision_loop(
        project_root,
        critic_fn=always_failing_critic,
    )

    assert MAX_REVISION_ITERATIONS == 3
    assert loop_result.iterations_run == 3
    assert call_count == 3
    assert loop_result.final_status == "review_needed"
    assert loop_result.escalated_to_human is True
