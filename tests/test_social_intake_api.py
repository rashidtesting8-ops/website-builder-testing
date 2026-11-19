"""TDD tests: frontend URL intake carries social URLs/permission and can force re-scrape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.cockpit import IntakeRequest
from app.pipeline import _seed_input_from_intake, refresh_social_for_project

from engine.service import PipelineRequest


def test_intake_models_accept_social_and_permission_fields() -> None:
    intake = IntakeRequest(
        url="https://www.lilys-club.co.uk/",
        instagram_url="https://www.instagram.com/lilysclubuk/",
        facebook_url="https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/",
        i_have_permission=True,
        permission_basis="Owner consent confirmed",
    )
    assert intake.i_have_permission is True
    assert intake.instagram_url and "instagram.com" in intake.instagram_url

    worker = PipelineRequest(
        url=intake.url,
        instagram_url=intake.instagram_url,
        facebook_url=intake.facebook_url,
        i_have_permission=True,
    )
    assert worker.facebook_url and "facebook.com" in worker.facebook_url


def test_input_seed_persists_social_fields_without_overwriting(tmp_path: Path) -> None:
    project = tmp_path / "seeded"
    project.mkdir()
    _seed_input_from_intake(
        project,
        url="https://www.lilys-club.co.uk/",
        business_name="Lily's Club",
        instagram_url="https://instagram.com/lilysclubuk",
        facebook_url="https://facebook.com/lilysclub",
        i_have_permission=True,
        permission_basis="Owner consent",
    )
    data = json.loads((project / "input.json").read_text(encoding="utf-8"))
    assert data["instagram_url"] == "https://instagram.com/lilysclubuk"
    assert data["facebook_url"] == "https://facebook.com/lilysclub"
    assert data["i_have_permission"] is True
    assert data["permission_basis"] == "Owner consent"

    # A second intake must not clobber operator edits.
    (project / "input.json").write_text(json.dumps({"hero_filename": "dog.jpg"}), encoding="utf-8")
    _seed_input_from_intake(
        project,
        url="https://other.example/",
        business_name="Other",
        instagram_url=None,
        facebook_url=None,
        i_have_permission=False,
        permission_basis=None,
    )
    assert json.loads((project / "input.json").read_text(encoding="utf-8")) == {
        "hero_filename": "dog.jpg"
    }


def test_social_refresh_recollects_without_full_pipeline(
    monkeypatch: Any, tmp_path: Path
) -> None:
    project = tmp_path / "refresh"
    (project / "research").mkdir(parents=True)
    (project / "input.json").write_text(
        json.dumps(
            {
                "instagram_url": "https://instagram.com/lilysclubuk",
                "i_have_permission": True,
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_collect(root: Path, warnings: list[str]) -> dict[str, Any]:
        captured["root"] = root
        captured["warnings"] = warnings
        return {"input_sha256": "abc", "platforms": {"instagram": {"status": "review_needed"}}}

    monkeypatch.setattr("app.pipeline._collect_configured_social", fake_collect)
    result = refresh_social_for_project(project)
    assert captured["root"] == project
    assert result["report"]["platforms"]["instagram"]["status"] == "review_needed"
