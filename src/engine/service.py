"""Private n8n collector & full 9-stage pipeline API. Bound to 127.0.0.1 by Docker Compose.

Bounded in-memory queue, durable SQLite statuses. Interrupted jobs remain
marked interrupted on restart instead of falsely saying they're running.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from engine.collector import collect, safe_url, slugify

app = FastAPI(title="Pet Grooming Asset Collector & Pipeline Worker", docs_url="/docs")
executor = ThreadPoolExecutor(max_workers=2)
slots = BoundedSemaphore(4)
lock = Lock()
DATA_DIR = Path(os.environ.get("PROJECTS_DIR", "./projects"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = Path(
    os.environ.get(
        "WORKER_DB_PATH",
        str(DATA_DIR / "_system" / "jobs.sqlite"),
    )
)
DB.parent.mkdir(parents=True, exist_ok=True)
COCKPIT_BASE_URL = os.environ.get("COCKPIT_BASE_URL", "http://127.0.0.1:8888").rstrip("/")


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    return con


def store(record: dict[str, Any]) -> None:
    with lock, connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO jobs (id, payload) VALUES (?,?)",
            (record["job_id"], json.dumps(record)),
        )


def fetch(job_id: str) -> dict[str, Any] | None:
    with lock, connect() as con:
        row = con.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


with connect() as _db:
    for _id, _blob in _db.execute("SELECT id,payload FROM jobs").fetchall():
        _old = json.loads(_blob)
        if _old.get("status") in ("running", "queued"):
            _old.update(status="interrupted", error="Service restarted; resubmit to retry")
            _db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(_old), _id))


class CollectRequest(BaseModel):
    url: str
    max_pages: int = Field(default=25, ge=1, le=40)
    max_media: int = Field(default=80, ge=1, le=120)
    full_pipeline: bool = False
    business_name: str | None = None
    business_id: str | None = None
    resume: bool = False


class PipelineRequest(BaseModel):
    url: str
    business_name: str | None = None
    business_id: str | None = None
    resume: bool = False


class ApprovePipelineRequest(BaseModel):
    version: str = "v1"
    operator: str
    notes: str
    human_review: dict[str, Any]


def _infer_business_name(url: str, explicit_name: str | None) -> str:
    if explicit_name and explicit_name.strip():
        return explicit_name.strip()
    host = urllib.parse.urlparse(url).hostname or "Pet Grooming Salon"
    clean = host.removeprefix("www.").split(".")[0]
    return clean.replace("-", " ").replace("_", " ").title() or "Pet Grooming Salon"


def _container_safe_qa_runner(project_folder: str | Path, **kwargs: Any) -> Any:
    """Run Playwright QA and propagate failures so unavailable browsers cannot pass release."""
    import app.pipeline as pipe_mod
    return pipe_mod.run_qa_engine(project_folder, **kwargs)


def _run_full_pipeline_sync(
    *,
    url: str,
    business_name: str | None,
    business_id: str | None,
    resume: bool,
) -> dict[str, Any]:
    from app.pipeline import run_pipeline

    resolved_name = _infer_business_name(url, explicit_name=business_name)
    resolved_id = slugify(business_id or resolved_name)
    res = run_pipeline(
        url=url,
        business_name=resolved_name,
        business_id=resolved_id,
        projects_dir=DATA_DIR,
        resume=resume,
        pause_for_approval=True,
        qa_runner=_container_safe_qa_runner,
    )
    return {
        "project_id": res.project_id,
        "business_name": res.business_name,
        "site_url": res.primary_url,
        "pipeline_status": res.status,
        "qa_passed": res.qa_passed,
        "project_folder": str(res.project_folder),
        "cockpit_review_url": f"{COCKPIT_BASE_URL}/review/{res.project_id}",
        "skipped_stages": res.skipped_stages,
        "warnings": res.warnings,
        "errors": res.errors,
    }


def work(job_id: str, req: CollectRequest) -> None:
    try:
        store({"job_id": job_id, "status": "running"})
        if req.full_pipeline:
            result = _run_full_pipeline_sync(
                url=req.url,
                business_name=req.business_name,
                business_id=req.business_id,
                resume=req.resume,
            )
        else:
            result = collect(req.url, DATA_DIR, req.max_pages, req.max_media)
        store({"job_id": job_id, "status": "done", "result": result})
    except Exception as exc:
        store({"job_id": job_id, "status": "failed", "error": str(exc)[:400]})
    finally:
        slots.release()


def work_pipeline(job_id: str, req: PipelineRequest) -> None:
    try:
        store({"job_id": job_id, "status": "running"})
        result = _run_full_pipeline_sync(
            url=req.url,
            business_name=req.business_name,
            business_id=req.business_id,
            resume=req.resume,
        )
        store({"job_id": job_id, "status": "done", "result": result})
    except Exception as exc:
        store({"job_id": job_id, "status": "failed", "error": str(exc)[:400]})
    finally:
        slots.release()


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/jobs", status_code=202)
def create_job(req: CollectRequest) -> dict[str, str]:
    try:
        safe_url(req.url)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "Collector busy; retry later")
    job_id = uuid.uuid4().hex
    payload = {"job_id": job_id, "status": "queued"}
    store(payload)
    try:
        executor.submit(work, job_id, req)
    except Exception:
        slots.release()
        raise
    return payload


@app.post("/pipeline/run", status_code=202)
def create_pipeline_job(req: PipelineRequest) -> dict[str, str]:
    try:
        safe_url(req.url)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "Worker busy; retry later")
    job_id = uuid.uuid4().hex
    payload = {"job_id": job_id, "status": "queued"}
    store(payload)
    try:
        executor.submit(work_pipeline, job_id, req)
    except Exception:
        slots.release()
        raise
    return payload


@app.post("/pipeline/{project_id}/approve")
def approve_pipeline_project(
    project_id: str, req: ApprovePipelineRequest
) -> dict[str, Any]:
    from app.approval import approve_release

    safe_id = slugify(project_id)
    try:
        tree_sha = approve_release(
            safe_id,
            version=req.version,
            operator_name=req.operator,
            notes=req.notes,
            projects_dir=DATA_DIR,
            review_attestation=req.human_review,
        )
        return {
            "status": "approved",
            "project_id": safe_id,
            "version": req.version,
            "release_tree_sha256": tree_sha,
            "ghl_snippet_path": str(DATA_DIR / safe_id / "delivery" / "ghl-ready.html"),
            "approved_homepage_path": str(
                DATA_DIR / safe_id / "delivery" / "approved-homepage.html"
            ),
        }
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    rec = fetch(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job")
    return rec
