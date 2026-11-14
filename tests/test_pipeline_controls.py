from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import app.pipeline as pipeline_module
import pytest
from app.db import connect_db
from app.pipeline import main, run_pipeline

SITE_URL = "http://127.0.0.1:8765/"
INSTAGRAM_URL = "https://www.instagram.com/paws.polish/"


def _write_collection(
    site_url: str,
    output_dir: Path | str,
    *,
    title: str = "Paws & Polish | Grooming",
    og_site_name: str = "Paws & Polish",
    instagram_url: str | None = INSTAGRAM_URL,
    **_kwargs: Any,
) -> dict[str, Any]:
    project = Path(output_dir) / "fixture-site"
    research = project / "research"
    snapshots = research / "snapshots"
    for folder in (research, snapshots, project / "assets" / "website"):
        folder.mkdir(parents=True, exist_ok=True)

    og_tag = (
        f'<meta property="og:site_name" content="{og_site_name}">'
        if og_site_name
        else ""
    )
    html = (
        "<html><head>"
        f"<title>{title}</title>{og_tag}"
        "</head><body><h1>Paws &amp; Polish</h1>"
        "<p>Dog grooming and baths.</p></body></html>"
    )
    page_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    (snapshots / f"{page_hash}.html").write_text(html, encoding="utf-8")
    (research / "pages.json").write_text(
        json.dumps(
            [
                {
                    "url": site_url,
                    "title": title,
                    "text": "Dog grooming and baths.",
                    "sha256": page_hash,
                }
            ]
        ),
        encoding="utf-8",
    )
    (research / "asset-manifest.json").write_text("[]", encoding="utf-8")
    (research / "social-sources.json").write_text(
        json.dumps(
            {
                "instagram": [instagram_url] if instagram_url else [],
                "facebook": [],
                "collection_status": "needs_authorized_collection_or_manual_upload",
            }
        ),
        encoding="utf-8",
    )
    return {
        "project_folder": str(project),
        "pages_collected": 1,
        "media_collected": 0,
        "media_failures": 0,
    }


def _run_to_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "Paws & Polish",
    stage: str = "collect",
) -> Any:
    monkeypatch.setattr(pipeline_module, "collect", _write_collection)
    return run_pipeline(
        url=SITE_URL,
        business_name=name,
        business_id="pipeline-controls",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "projects" / "editvista.sqlite3",
        allow_private=True,
        stop_after=stage,
    )


def test_stop_after_collect_records_only_completed_collect_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_to_stage(tmp_path, monkeypatch)

    assert result.status == "stopped_after_stage"
    assert result.stopped_after_stage == "collect"
    conn = connect_db(tmp_path / "projects" / "editvista.sqlite3")
    try:
        rows = conn.execute(
            "SELECT module_name, status FROM job_runs "
            "WHERE project_id = ? ORDER BY module_name",
            ("pipeline-controls",),
        ).fetchall()
        assert [(row["module_name"], row["status"]) for row in rows] == [
            ("collect", "completed")
        ]
    finally:
        conn.close()


def test_cli_accepts_stop_after_stage_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pipeline_module, "collect", _write_collection)

    exit_code = main(
        [
            "--url",
            SITE_URL,
            "--name",
            "Paws & Polish",
            "--business-id",
            "pipeline-controls",
            "--projects-dir",
            str(tmp_path / "projects"),
            "--db-path",
            str(tmp_path / "projects" / "editvista.sqlite3"),
            "--allow-private",
            "--stop-after",
            "collect",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "stopped_after_stage"
    assert result["stopped_after_stage"] == "collect"


def test_stop_after_rejects_unknown_stage() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--url",
                SITE_URL,
                "--name",
                "Paws & Polish",
                "--stop-after",
                "not-a-stage",
            ]
        )

    assert exc_info.value.code == 2


def test_resume_reports_completed_collect_stage_as_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _run_to_stage(tmp_path, monkeypatch, stage="collect")
    resumed = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "projects" / "editvista.sqlite3",
        allow_private=True,
        resume=True,
        stop_after="extract",
    )

    assert first.stopped_after_stage == "collect"
    assert resumed.stopped_after_stage == "extract"
    assert resumed.skipped_stages == ["collect"]


def test_resume_retries_interrupted_collect_despite_stale_page_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect_calls = 0

    def tracked_collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal collect_calls
        collect_calls += 1
        return _write_collection(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "collect", tracked_collect)
    first = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "projects" / "editvista.sqlite3",
        allow_private=True,
        stop_after="collect",
    )
    conn = connect_db(tmp_path / "projects" / "editvista.sqlite3")
    try:
        conn.execute(
            "UPDATE job_runs SET status = 'interrupted' "
            "WHERE project_id = ? AND module_name = 'collect'",
            ("pipeline-controls",),
        )
    finally:
        conn.close()

    resumed = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "projects" / "editvista.sqlite3",
        allow_private=True,
        resume=True,
        stop_after="collect",
    )

    assert first.stopped_after_stage == "collect"
    assert resumed.skipped_stages == []
    assert collect_calls == 2


def test_resume_recollects_when_social_input_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "pipeline-controls"
    project_root.mkdir(parents=True)
    input_path = project_root / "input.json"
    input_path.write_text(json.dumps({"instagram_url": INSTAGRAM_URL}), encoding="utf-8")
    collect_calls = 0

    def tracked_collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal collect_calls
        collect_calls += 1
        return _write_collection(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "collect", tracked_collect)
    run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=projects_dir,
        db_path=projects_dir / "editvista.sqlite3",
        allow_private=True,
        stop_after="collect",
    )
    input_path.write_text(
        json.dumps(
            {
                "instagram_url": "https://www.instagram.com/changed-account/",
                "i_have_permission": True,
            }
        ),
        encoding="utf-8",
    )

    resumed = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=projects_dir,
        db_path=projects_dir / "editvista.sqlite3",
        allow_private=True,
        resume=True,
        stop_after="collect",
    )
    report = json.loads(
        (project_root / "research" / "social-collection-report.json").read_text(
            encoding="utf-8"
        )
    )

    assert collect_calls == 2
    assert resumed.skipped_stages == []
    assert report["platforms"]["instagram"]["supplied_url"] == (
        "https://www.instagram.com/changed-account/"
    )


def test_different_input_and_website_social_urls_require_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "pipeline-controls"
    project_root.mkdir(parents=True)
    supplied_url = "https://www.instagram.com/client-supplied/"
    (project_root / "input.json").write_text(
        json.dumps(
            {
                "instagram_url": supplied_url,
                "i_have_permission": True,
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(pipeline_module, "collect", _write_collection)
    monkeypatch.setattr(
        pipeline_module,
        "collect_social",
        lambda *args, **kwargs: calls.append((args, kwargs)),
        raising=False,
    )

    result = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=projects_dir,
        db_path=projects_dir / "editvista.sqlite3",
        allow_private=True,
        stop_after="collect",
    )
    report = json.loads(
        (project_root / "research" / "social-collection-report.json").read_text(
            encoding="utf-8"
        )
    )

    instagram = report["platforms"]["instagram"]
    assert result.status == "stopped_after_stage"
    assert instagram["status"] == "needs_review"
    assert instagram["supplied_url"] == supplied_url
    assert instagram["website_urls"] == [INSTAGRAM_URL]
    assert instagram["reason"] == "input_url_differs_from_website"
    assert calls == []


def test_social_collection_requires_explicit_permission_and_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "pipeline-controls"
    project_root.mkdir(parents=True)
    (project_root / "input.json").write_text(
        json.dumps({"instagram_url": INSTAGRAM_URL}),
        encoding="utf-8",
    )
    calls: list[tuple[Any, ...]] = []

    def failed_social_collection(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {
            "status": "review_needed",
            "reason": "login_required",
            "files_added": 0,
        }

    monkeypatch.setattr(pipeline_module, "collect", _write_collection)
    monkeypatch.setattr(pipeline_module, "collect_social", failed_social_collection)

    run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=projects_dir,
        db_path=projects_dir / "editvista.sqlite3",
        allow_private=True,
        stop_after="collect",
    )
    report = json.loads(
        (project_root / "research" / "social-collection-report.json").read_text(
            encoding="utf-8"
        )
    )

    assert report["platforms"]["instagram"]["status"] == "review_needed"
    assert report["platforms"]["instagram"]["reason"] == "permission_required"
    assert calls == []


def test_authorized_social_failure_is_recorded_without_aborting_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "pipeline-controls"
    project_root.mkdir(parents=True)
    (project_root / "input.json").write_text(
        json.dumps(
            {
                "instagram_url": INSTAGRAM_URL,
                "i_have_permission": True,
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[Any, ...]] = []

    def failed_social_collection(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {
            "status": "review_needed",
            "reason": "login_required",
            "files_added": 0,
        }

    monkeypatch.setattr(pipeline_module, "collect", _write_collection)
    monkeypatch.setattr(pipeline_module, "collect_social", failed_social_collection)

    result = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=projects_dir,
        db_path=projects_dir / "editvista.sqlite3",
        allow_private=True,
        stop_after="collect",
    )
    report = json.loads(
        (project_root / "research" / "social-collection-report.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.status == "stopped_after_stage"
    assert report["platforms"]["instagram"]["status"] == "review_needed"
    assert report["platforms"]["instagram"]["reason"] == "login_required"
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["max_files"] == 40
    assert kwargs["days_limit"] == 365
    assert kwargs["i_have_permission"] is True


def test_social_collector_exception_becomes_review_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / "projects"
    project_root = projects_dir / "pipeline-controls"
    project_root.mkdir(parents=True)
    (project_root / "input.json").write_text(
        json.dumps(
            {
                "instagram_url": INSTAGRAM_URL,
                "i_have_permission": True,
            }
        ),
        encoding="utf-8",
    )

    def broken_social_collection(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("public collector failed")

    monkeypatch.setattr(pipeline_module, "collect", _write_collection)
    monkeypatch.setattr(pipeline_module, "collect_social", broken_social_collection)

    result = run_pipeline(
        url=SITE_URL,
        business_name="Paws & Polish",
        business_id="pipeline-controls",
        projects_dir=projects_dir,
        db_path=projects_dir / "editvista.sqlite3",
        allow_private=True,
        stop_after="collect",
    )
    report = json.loads(
        (project_root / "research" / "social-collection-report.json").read_text(
            encoding="utf-8"
        )
    )

    assert result.status == "stopped_after_stage"
    assert report["platforms"]["instagram"]["status"] == "review_needed"
    assert report["platforms"]["instagram"]["reason"] == "social_collector_error"
    assert report["platforms"]["instagram"]["error"] == "public collector failed"


def test_operator_business_name_matching_site_metadata_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_to_stage(tmp_path, monkeypatch, stage="extract")
    facts = json.loads(
        (
            result.project_folder
            / "research"
            / "business-facts.json"
        ).read_text(encoding="utf-8")
    )["facts"]
    matched = [
        fact
        for fact in facts
        if fact["category"] == "business_name"
        and fact["claim_value"] == "Paws & Polish"
    ]
    comparison = json.loads(
        (
            result.project_folder
            / "research"
            / "business-name-comparison.json"
        ).read_text(encoding="utf-8")
    )

    assert result.status == "stopped_after_stage"
    assert matched
    assert matched[0]["verified"] is True
    assert matched[0]["status"] == "verified"
    assert matched[0]["source_url"] == SITE_URL
    assert matched[0]["source_text"] == "Paws & Polish"
    assert comparison["status"] == "verified"


def test_operator_business_name_mismatch_stays_needs_review_with_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_to_stage(
        tmp_path,
        monkeypatch,
        name="Paws Polish Studio",
        stage="extract",
    )
    facts = json.loads(
        (
            result.project_folder
            / "research"
            / "business-facts.json"
        ).read_text(encoding="utf-8")
    )["facts"]
    business_name_facts = [
        fact for fact in facts if fact["category"] == "business_name"
    ]
    comparison = json.loads(
        (
            result.project_folder
            / "research"
            / "business-name-comparison.json"
        ).read_text(encoding="utf-8")
    )

    assert business_name_facts
    assert all(fact["verified"] is False for fact in business_name_facts)
    assert all(fact["status"] == "needs_review" for fact in business_name_facts)
    assert comparison["status"] == "needs_review"
    assert comparison["comparisons"][0]["diff"]


def test_resume_rechecks_name_when_operator_input_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_to_stage(tmp_path, monkeypatch, stage="extract")
    resumed = run_pipeline(
        url=SITE_URL,
        business_name="Paws Polish Studio",
        business_id="pipeline-controls",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "projects" / "editvista.sqlite3",
        allow_private=True,
        resume=True,
        stop_after="extract",
    )
    comparison = json.loads(
        (
            resumed.project_folder
            / "research"
            / "business-name-comparison.json"
        ).read_text(encoding="utf-8")
    )

    assert resumed.skipped_stages == ["collect"]
    assert comparison["status"] == "needs_review"
    assert comparison["operator_name"] == "Paws Polish Studio"

    next_resume = run_pipeline(
        url=SITE_URL,
        business_name="Paws Polish Studio",
        business_id="pipeline-controls",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "projects" / "editvista.sqlite3",
        allow_private=True,
        resume=True,
        stop_after="extract",
    )

    assert next_resume.skipped_stages == ["collect", "extract"]
