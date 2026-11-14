"""Tests for P0 pipeline collect failure handling and recovery.

Invariants tested:
1. On collect failure: record stage "failed" with the error, set project status "failed",
   stop the pipeline, and raise a clear error. No fake pages.json, no later stages.
2. Stages left "running" by a crashed earlier run are marked "interrupted" at the start
   of the next run.
3. --resume retries a failed collect (a failed stage never counts as completed).
4. A broken single media download does not abort the project (only total page collection
   failure stops the run).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import app.pipeline as pipeline_module
import pytest
from app.db import (
    JobRunRecord,
    ProjectRecord,
    connect_db,
    get_job_run,
    get_project,
    init_db,
    upsert_job_run,
    upsert_project,
)
from app.pipeline import run_pipeline
from app.qa_engine import QADefectReport
from PIL import Image


def _dummy_qa_runner(project_root: Path) -> QADefectReport:
    qa_dir = project_root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    report = QADefectReport(
        project_id=project_root.name,
        target_url="http://127.0.0.1:8000/release-candidate/standalone.html",
        passed=True,
        candidate_sha256="fake_sha_for_test",
        viewports=[],
    )
    report_path = qa_dir / "qa-report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def _mock_successful_collect(
    url: str,
    output_dir: Path | str,
    max_pages: int = 10,
    max_media: int = 40,
    allow_private: bool = False,
) -> dict[str, Any]:
    out = Path(output_dir)
    research_dir = out / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = out / "assets" / "website"
    assets_dir.mkdir(parents=True, exist_ok=True)

    pages = [
        {
            "url": url,
            "title": "Retry Salon",
            "text": "Full service dog grooming, baths, haircuts, nail trim. Phone: 555-123-4567.",
            "sha256": "a" * 64,
        }
    ]
    (research_dir / "pages.json").write_text(json.dumps(pages, indent=2), encoding="utf-8")
    (research_dir / "asset-manifest.json").write_text(json.dumps([], indent=2), encoding="utf-8")

    return {
        "project_folder": str(out),
        "pages_collected": 1,
        "media_collected": 0,
        "media_failures": 0,
    }


def test_collect_failure_stops_pipeline_no_fake_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1: On collect failure: record stage 'failed', set project status 'failed',

    raise a clear error, write NO fake pages.json, and run NO later stages.
    """
    db_path = tmp_path / "editvista.sqlite3"
    project_id = "failing-salon"

    def _broken_collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Simulated network timeout connecting to client site")

    monkeypatch.setattr(pipeline_module, "collect", _broken_collect)

    with pytest.raises(RuntimeError, match="Collection failed|Simulated network timeout"):
        run_pipeline(
            url="https://fail.example.com",
            business_name="Failing Salon",
            business_id=project_id,
            projects_dir=tmp_path,
            db_path=db_path,
            qa_runner=_dummy_qa_runner,
        )

    # 1. Fake research/pages.json must NOT be written
    fake_pages_path = tmp_path / project_id / "research" / "pages.json"
    assert not fake_pages_path.exists(), (
        "Fake research/pages.json should NEVER be written on failure"
    )

    # 2. Database project status must be 'failed'
    conn = connect_db(db_path)
    proj = get_project(conn, project_id)
    assert proj is not None
    assert proj.status == "failed"

    # 3. Only the collect stage should exist in job_runs with status 'failed'
    collect_run = get_job_run(conn, f"{project_id}:collect")
    assert collect_run is not None
    assert collect_run.status == "failed"
    assert "Simulated network timeout" in (collect_run.error_message or "")

    # 4. No downstream stages should have been executed
    rows = conn.execute(
        "SELECT module_name, status FROM job_runs WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    module_names = [str(r["module_name"]) for r in rows]
    assert module_names == ["collect"], f"Expected only collect stage, but found: {module_names}"
    conn.close()


def test_resume_retries_failed_collect_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 3: --resume must retry a failed collect.

    A failed stage never counts as completed.
    """
    db_path = tmp_path / "editvista.sqlite3"
    project_id = "retry-salon"

    def _broken_collect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Temporary 503 Service Unavailable")

    monkeypatch.setattr(pipeline_module, "collect", _broken_collect)

    # Step 1: Run fails during collect
    with pytest.raises(RuntimeError):
        run_pipeline(
            url="https://retry.example.com",
            business_name="Retry Salon",
            business_id=project_id,
            projects_dir=tmp_path,
            db_path=db_path,
            qa_runner=_dummy_qa_runner,
        )

    conn = connect_db(db_path)
    assert get_project(conn, project_id).status == "failed"  # type: ignore
    collect_job = get_job_run(conn, f"{project_id}:collect")
    assert collect_job is not None and collect_job.status == "failed"
    conn.close()

    # Step 2: Site recovers, unpatch collect to succeed and run with resume=True
    monkeypatch.setattr(pipeline_module, "collect", _mock_successful_collect)

    result = run_pipeline(
        url="https://retry.example.com",
        business_name="Retry Salon",
        business_id=project_id,
        projects_dir=tmp_path,
        db_path=db_path,
        resume=True,
        qa_runner=_dummy_qa_runner,
    )

    assert result.status == "awaiting_approval"
    assert (tmp_path / project_id / "research" / "pages.json").exists()

    conn = connect_db(db_path)
    updated_proj = get_project(conn, project_id)
    assert updated_proj is not None and updated_proj.status == "awaiting_approval"

    updated_collect = get_job_run(conn, f"{project_id}:collect")
    assert updated_collect is not None and updated_collect.status == "completed"

    # Later stages should also have executed to completion
    extract_job = get_job_run(conn, f"{project_id}:extract")
    assert extract_job is not None and extract_job.status == "completed"
    conn.close()


def test_stages_left_running_marked_interrupted_on_next_run(tmp_path: Path) -> None:
    """Requirement 2: Stages left 'running' by a crashed earlier run are marked 'interrupted'

    at the start of the next run.
    """
    db_path = tmp_path / "editvista.sqlite3"
    project_id = "crashed-salon"
    conn = connect_db(db_path)
    init_db(conn)

    # Simulate a run that crashed mid-execution while running 'extract'
    upsert_project(
        conn,
        ProjectRecord(
            project_id=project_id,
            business_name="Crashed Salon",
            primary_url="https://crashed.example.com",
            status="running",
        ),
    )
    upsert_job_run(
        conn,
        JobRunRecord(
            run_id=f"{project_id}:prior_crashed_stage",
            project_id=project_id,
            module_name="prior_crashed_stage",
            status="running",
            idempotency_key=f"{project_id}:prior_crashed_stage:dummy_hash",
            error_message=None,
        ),
    )
    conn.close()

    # When next run starts, the running stage must be marked 'interrupted'
    try:
        run_pipeline(
            url="https://crashed.example.com",
            business_name="Crashed Salon",
            business_id=project_id,
            projects_dir=tmp_path,
            db_path=db_path,
            qa_runner=_dummy_qa_runner,
        )
    except Exception:
        pass

    conn = connect_db(db_path)
    crashed_job = get_job_run(conn, f"{project_id}:prior_crashed_stage")
    assert crashed_job is not None
    assert crashed_job.status == "interrupted", (
        f"Expected 'interrupted', but got '{crashed_job.status}'"
    )
    conn.close()


def test_broken_single_media_download_does_not_abort_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 4: A broken single MEDIA download must still NOT abort the project.

    Only a total page-collection failure stops the run.
    """
    db_path = tmp_path / "editvista.sqlite3"
    project_id = "media-fail-salon"

    def _collect_with_failed_media(
        url: str,
        output_dir: Path | str,
        max_pages: int = 10,
        max_media: int = 40,
        allow_private: bool = False,
    ) -> dict[str, Any]:
        out = Path(output_dir)
        research_dir = out / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = out / "assets" / "website"
        assets_dir.mkdir(parents=True, exist_ok=True)

        pages = [
            {
                "url": url,
                "title": "Media Fail Salon",
                "text": "Quality grooming services. Phone: 555-987-6543.",
                "sha256": "b" * 64,
            }
        ]
        (research_dir / "pages.json").write_text(json.dumps(pages, indent=2), encoding="utf-8")

        # Media manifest has one failed media download and one successful one
        img = Image.new("RGB", (32, 32), color=(100, 150, 200))
        img.save(assets_dir / "logo.png", format="PNG")
        good_img_bytes = (assets_dir / "logo.png").read_bytes()
        media_manifest = [
            {
                "url": "https://media.example.com/broken.jpg",
                "status": "failed",
                "error": "HTTP 404 Not Found",
            },
            {
                "url": "https://media.example.com/logo.png",
                "status": "downloaded",
                "file": "assets/website/logo.png",
                "sha256": "c" * 64,
                "bytes": len(good_img_bytes),
                "rights_confirmed_for_preview": False,
            },
        ]
        (research_dir / "asset-manifest.json").write_text(
            json.dumps(media_manifest, indent=2), encoding="utf-8"
        )

        return {
            "project_folder": str(out),
            "pages_collected": 1,
            "media_collected": 1,
            "media_failures": 1,
        }

    monkeypatch.setattr(pipeline_module, "collect", _collect_with_failed_media)

    result = run_pipeline(
        url="https://media.example.com",
        business_name="Media Fail Salon",
        business_id=project_id,
        projects_dir=tmp_path,
        db_path=db_path,
        qa_runner=_dummy_qa_runner,
    )

    # Pipeline MUST proceed to awaiting_approval despite the single broken media item
    assert result.status == "awaiting_approval"
    conn = connect_db(db_path)
    proj = get_project(conn, project_id)
    assert proj is not None and proj.status == "awaiting_approval"
    conn.close()
