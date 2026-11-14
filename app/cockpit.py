"""Local Visual Review & Operator Cockpit (Track 2: Human-in-the-Loop Web UI).

FastAPI + Vanilla HTML/CSS/JS operator dashboard for side-by-side inspection,
fact verification auditing, 5-viewport screenshot QA, asset rights confirmation,
surgical section revisions, and release freezing.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.approval import (
    HumanReviewAttestation,
    ReleaseApprovalGateError,
    approve_release,
    generate_review_summary,
    stage_release_candidate,
)
from app.db import connect_db, init_db, list_projects
from app.generator import revise_section
from app.ghl_exporter import (
    export_ghl_snippet_from_html,
    validate_ghl_embed_simulation,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


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


def _resolve_project_root(project_id: str, projects_dir: Path) -> Path:
    if ".." in project_id or "/" in project_id or "\\" in project_id:
        raise HTTPException(status_code=400, detail="Invalid project identifier")
    project_root = (projects_dir / project_id).resolve()
    if not project_root.is_relative_to(projects_dir.resolve()) or not project_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project_root


def _resolve_safe_file(base_dir: Path, rel_path: str) -> Path:
    cleaned = rel_path.lstrip("/\\")
    target = (base_dir / cleaned).resolve()
    if not target.is_relative_to(base_dir.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found or access denied")
    return target


class ConfirmRightsRequest(BaseModel):
    asset_file: str | None = None
    asset: str | None = None
    sha256: str | None = None
    basis: str
    operator: str
    evidence: str


class ApproveReleaseRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    operator_name: str = Field(min_length=2)
    notes: str = Field(min_length=10)
    version: str = Field(default="v1")
    human_review: HumanReviewAttestation | None = None


class ReviseSectionRequest(BaseModel):
    section_name: str
    new_html: str


class IntakeRequest(BaseModel):
    """URL-paste intake payload forwarded verbatim to the worker service."""

    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(min_length=1)
    business_name: str | None = None
    business_id: str | None = None
    resume: bool = False


_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")


def _worker_detail(resp: requests.Response) -> str:
    """Extract a FastAPI-style 'detail' message from a worker error response."""
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
    text = (resp.text or "").strip()
    return text[:300] if text else "Worker request failed"


def _worker_json(resp: requests.Response) -> dict[str, Any]:
    """Parse a worker success body, failing closed on non-object JSON."""
    try:
        payload = resp.json()
    except ValueError as exc:
        raise HTTPException(502, "Worker returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise HTTPException(502, "Worker returned an unexpected response")
    return payload


def _render_dashboard_html(projects: list[dict[str, Any]]) -> str:
    template_path = TEMPLATES_DIR / "dashboard.html"
    raw_template = template_path.read_text(encoding="utf-8")

    cards = []
    status_counts = {
        "running": 0,
        "awaiting_approval": 0,
        "needs_review": 0,
        "approved": 0,
    }
    for p in projects:
        pid = html.escape(p["project_id"])
        name = html.escape(p.get("business_name") or pid)
        status_value = str(p.get("status") or "unknown").lower()
        status = html.escape(status_value)
        url_value = str(p.get("primary_url") or "").strip()
        url = html.escape(url_value)
        if status_value in status_counts:
            status_counts[status_value] += 1

        status_classes = {
            "approved": "badge-green",
            "needs_review": "badge-red",
            "awaiting_approval": "badge-amber",
            "running": "badge-blue",
        }
        status_class = status_classes.get(status_value, "badge-neutral")
        search_text = html.escape(f"{name} {pid} {url}".lower(), quote=True)
        source_markup = (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
            if url_value
            else "<span>Source URL not recorded</span>"
        )

        card_snippet = (
            f'<article class="project-card" data-project-card data-search="{search_text}">\n'
            '  <div class="card-topline">\n'
            '    <span class="card-kind">Website project</span>\n'
            f'    <span class="status-badge {status_class}">{status.replace("_", " ")}</span>\n'
            "  </div>\n"
            f"  <h3>{name}</h3>\n"
            f'  <p class="project-url">{source_markup}</p>\n'
            '  <div class="card-divider"></div>\n'
            '  <div class="card-footer">\n'
            f'    <span class="pid-tag">ID · {pid}</span>\n'
            f'    <a class="open-link" href="/review/{pid}">Open review '
            '<span aria-hidden="true">&rarr;</span></a>\n'
            "  </div>\n"
            "</article>"
        )
        cards.append(card_snippet)

    cards_html = (
        "\n".join(cards)
        if cards
        else (
            '<div class="empty-state" id="no-projects">'
            "<strong>No projects yet</strong>"
            "Create a local project to begin the evidence-led workflow."
            "</div>"
        )
    )
    if cards:
        cards_html += (
            '<div class="empty-state" id="search-empty" hidden>'
            "<strong>No matching projects</strong>"
            "Try a different project name, ID, or source URL."
            "</div>"
        )

    rendered = raw_template.replace("{{CARDS_HTML}}", cards_html)
    rendered = rendered.replace("{{PROJECT_COUNT}}", str(len(projects)))
    rendered = rendered.replace("{{RUNNING_COUNT}}", str(status_counts["running"]))
    rendered = rendered.replace(
        "{{REVIEW_COUNT}}",
        str(status_counts["awaiting_approval"] + status_counts["needs_review"]),
    )
    rendered = rendered.replace("{{APPROVED_COUNT}}", str(status_counts["approved"]))
    return rendered


def _render_review_cockpit_html(
    project_id: str,
    project_data: dict[str, Any],
) -> str:
    template_path = TEMPLATES_DIR / "review_cockpit.html"
    raw_template = template_path.read_text(encoding="utf-8")

    pid = html.escape(project_id)
    summary = project_data.get("summary", {})
    facts = project_data.get("facts", [])
    assets = summary.get("assets", [])
    status = html.escape(project_data.get("status", "unknown"))
    fact_metrics = summary.get("fact_metrics", {})
    verified_count = int(fact_metrics.get("verified_count", 0))
    needs_review_count = int(fact_metrics.get("needs_review_count", 0))
    confirmed_asset_count = sum(
        bool(asset.get("rights_confirmed_for_preview")) for asset in assets
    )
    unconfirmed_asset_count = len(assets) - confirmed_asset_count
    qa_passed = bool(summary.get("technical_qa_passed"))

    if status == "approved":
        guardrail_class = "notice-success"
        guardrail_text = (
            "This project is marked approved. Verify its frozen release manifest "
            "before sharing deliverables."
        )
    elif unconfirmed_asset_count:
        guardrail_class = "notice-warning"
        guardrail_text = (
            f"{unconfirmed_asset_count} asset(s) still need documented rights evidence. "
            "Do not approve or publish until the evidence is confirmed."
        )
    elif needs_review_count:
        guardrail_class = "notice-warning"
        guardrail_text = (
            f"{needs_review_count} factual claim(s) still need human review before release."
        )
    elif not qa_passed:
        guardrail_class = "notice-warning"
        guardrail_text = "Technical QA is not marked as passed. Keep this candidate in draft."
    else:
        guardrail_class = "notice-info"
        guardrail_text = (
            "Automated checks are recorded. Human review and explicit release approval "
            "are still required."
        )

    # Facts rows
    fact_rows = []
    for f in facts:
        cat = html.escape(str(f.get("category", "")))
        key = html.escape(str(f.get("claim_key", "")))
        val = html.escape(str(f.get("claim_value", "")))
        snippet = html.escape(str(f.get("source_text_snippet", "")))
        src_url = html.escape(str(f.get("source_url", "")))
        is_ver = bool(f.get("is_verified"))
        badge = (
            '<span class="badge badge-green">verified</span>'
            if is_ver
            else '<span class="badge badge-amber">needs_review</span>'
        )

        row = (
            "        <tr>\n"
            f'          <td><span class="cat-pill">{cat}</span></td>\n'
            f"          <td><strong>{key}</strong><br>"
            f'<span class="val-text">{val}</span></td>\n'
            f"          <td>{badge}</td>\n"
            f'          <td class="snippet-cell">"{snippet}"<br>'
            f'<a href="{src_url}" target="_blank" rel="noopener">{src_url}</a></td>\n'
            "        </tr>\n"
        )
        fact_rows.append(row)
    facts_html = (
        "".join(fact_rows) if fact_rows else "<tr><td colspan='4'>No facts extracted.</td></tr>"
    )

    # Asset rows
    asset_rows = []
    for a in assets:
        rel_f = str(a.get("file", ""))
        esc_f = html.escape(rel_f)
        sha = html.escape(str(a.get("sha256", ""))[:12])
        url = html.escape(str(a.get("source_url", "")))
        rights = bool(a.get("rights_confirmed_for_preview"))
        r_badge = (
            '<span class="badge badge-green">Confirmed</span>'
            if rights
            else '<span class="badge badge-amber">Unconfirmed</span>'
        )
        btn = (
            '<span class="badge badge-green">Audited</span>'
            if rights
            else (
                f'<button class="btn-sm" onclick="confirmRights(\'{esc_f}\')">'
                "Record Rights Evidence</button>"
            )
        )

        row = (
            "        <tr>\n"
            f'          <td><img src="/assets/{pid}/{esc_f}" class="thumb-img" '
            f'alt="{esc_f}" onerror="this.style.display=\'none\'"></td>\n'
            f"          <td><code>{esc_f}</code><br>"
            f'<span class="sha-text">SHA: {sha}</span></td>\n'
            f'          <td><a href="{url}" target="_blank" rel="noopener" '
            f'class="src-link">{url}</a></td>\n'
            f"          <td>{r_badge}</td>\n"
            f"          <td>{btn}</td>\n"
            "        </tr>\n"
        )
        asset_rows.append(row)
    assets_html = (
        "".join(asset_rows)
        if asset_rows
        else "<tr><td colspan='5'>No assets in manifest.</td></tr>"
    )

    # Screenshot tabs
    tabs = []
    panes = []
    widths = [1440, 1024, 768, 390, 360]
    for idx, w in enumerate(widths):
        active_cls = "active" if idx == 0 else ""
        tabs.append(
            f'<button class="tab-btn {active_cls}" type="button" role="tab" '
            f'aria-selected="{str(idx == 0).lower()}" aria-controls="vp-{w}" '
            f'onclick="openTab(event, \'vp-{w}\')">{w}px</button>'
        )
        panes.append(
            f'        <div id="vp-{w}" class="tab-pane {active_cls}" role="tabpanel">\n'
            '          <div class="shot-wrapper">\n'
            f'            <img src="/screenshots/{pid}/{w}px.png" '
            f'alt="Viewport {w}px render" class="viewport-shot">\n'
            "          </div>\n"
            "        </div>"
        )

    rendered = raw_template.replace("{{PID}}", pid)
    rendered = rendered.replace("{{STATUS}}", status)
    candidate_sha256 = str(summary.get("candidate_sha256", ""))
    rendered = rendered.replace("{{CANDIDATE_SHA256}}", html.escape(candidate_sha256))
    approval_action = (
        '<span class="badge badge-green">Release already frozen</span>'
        if status == "approved"
        else (
            '<a class="btn-approve" href="#human-approval">Review and sign off '
            '<span aria-hidden="true">&rarr;</span></a>'
        )
    )
    rendered = rendered.replace("{{APPROVAL_ACTION}}", approval_action)
    status_class = {
        "approved": "badge-green",
        "needs_review": "badge-red",
        "awaiting_approval": "badge-amber",
        "running": "badge-blue",
    }.get(str(project_data.get("status", "unknown")).lower(), "badge-neutral")
    rendered = rendered.replace("{{STATUS_CLASS}}", status_class)
    rendered = rendered.replace("{{VERIFIED_COUNT}}", str(verified_count))
    rendered = rendered.replace("{{NEEDS_REVIEW_COUNT}}", str(needs_review_count))
    rendered = rendered.replace(
        "{{CONFIRMED_ASSETS}}",
        f"{confirmed_asset_count}<span> / {len(assets)}</span>",
    )
    rendered = rendered.replace("{{UNCONFIRMED_ASSETS}}", str(unconfirmed_asset_count))
    rendered = rendered.replace(
        "{{QA_STATUS}}", "Passed" if qa_passed else "Not passed"
    )
    rendered = rendered.replace("{{GUARDRAIL_CLASS}}", guardrail_class)
    rendered = rendered.replace("{{GUARDRAIL_TEXT}}", html.escape(guardrail_text))
    rendered = rendered.replace("{{FACTS_HTML}}", facts_html)
    rendered = rendered.replace("{{ASSETS_HTML}}", assets_html)
    rendered = rendered.replace("{{TABS_HTML}}", "\n".join(tabs))
    rendered = rendered.replace("{{PANES_HTML}}", "\n".join(panes))
    return rendered


def create_cockpit_app(
    projects_dir: str | Path = "./projects",
    worker_base_url: str | None = None,
) -> FastAPI:
    """Factory creating the Operator Cockpit FastAPI instance bound to a projects root."""
    resolved_projects_dir = Path(projects_dir).resolve()
    resolved_projects_dir.mkdir(parents=True, exist_ok=True)
    db_path = resolved_projects_dir / "editvista.sqlite3"
    resolved_worker_url = (
        worker_base_url
        or os.environ.get("WORKER_BASE_URL", "http://127.0.0.1:8787")
    ).rstrip("/")

    app = FastAPI(title="EditVista Operator Cockpit", docs_url="/api/docs")

    @app.get("/api/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        conn = connect_db(db_path)
        try:
            init_db(conn)
            records = list_projects(conn)
            projects_map = {r.project_id: r.model_dump() for r in records}
        finally:
            conn.close()

        # Augment with directories present on disk
        for p in resolved_projects_dir.iterdir():
            if p.is_dir() and not p.name.startswith(("_", ".")):
                if p.name not in projects_map:
                    projects_map[p.name] = {
                        "project_id": p.name,
                        "business_name": p.name,
                        "status": "awaiting_approval",
                        "primary_url": "",
                    }

        return HTMLResponse(content=_render_dashboard_html(list(projects_map.values())))

    @app.get("/review/{project_id}", response_class=HTMLResponse)
    def review_cockpit(project_id: str) -> HTMLResponse:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        summary = generate_review_summary(project_id, projects_dir=resolved_projects_dir)

        facts_list: list[dict[str, Any]] = []
        facts_path = root / "research" / "business-facts.json"
        if facts_path.is_file():
            try:
                f_data = json.loads(facts_path.read_text(encoding="utf-8"))
                facts_list = f_data.get("facts", [])
            except Exception:
                pass

        status = "awaiting_approval"
        conn = connect_db(db_path)
        try:
            init_db(conn)
            row = conn.execute(
                "SELECT status FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row:
                status = str(row[0])
        finally:
            conn.close()

        project_data = {
            "summary": summary,
            "facts": facts_list,
            "status": status,
        }
        return HTMLResponse(content=_render_review_cockpit_html(project_id, project_data))

    @app.get("/api/projects/{project_id}/summary")
    def project_summary_api(project_id: str) -> dict[str, Any]:
        _resolve_project_root(project_id, resolved_projects_dir)
        return generate_review_summary(project_id, projects_dir=resolved_projects_dir)

    @app.post("/api/intake", status_code=202)
    def intake_endpoint(req: IntakeRequest) -> dict[str, Any]:
        """Start a full pipeline run on the worker (the single heavy-build slot lives there)."""
        payload: dict[str, Any] = {
            "url": req.url,
            "business_name": req.business_name,
            "business_id": req.business_id,
            "resume": req.resume,
        }
        try:
            resp = requests.post(
                f"{resolved_worker_url}/pipeline/run",
                json=payload,
                timeout=15.0,
            )
        except requests.RequestException as exc:
            raise HTTPException(
                502,
                f"Worker unreachable at {resolved_worker_url}: {type(exc).__name__}",
            ) from exc
        if resp.status_code != 202:
            raise HTTPException(resp.status_code, _worker_detail(resp))
        return _worker_json(resp)

    @app.get("/api/jobs/{job_id}")
    def job_status_endpoint(job_id: str) -> dict[str, Any]:
        if _JOB_ID_RE.fullmatch(job_id) is None:
            raise HTTPException(404, "Unknown job")
        try:
            resp = requests.get(f"{resolved_worker_url}/jobs/{job_id}", timeout=10.0)
        except requests.RequestException as exc:
            raise HTTPException(
                502,
                f"Worker unreachable at {resolved_worker_url}: {type(exc).__name__}",
            ) from exc
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, _worker_detail(resp))
        return _worker_json(resp)

    @app.post("/api/projects/{project_id}/confirm-rights")
    def confirm_asset_rights_endpoint(
        project_id: str, req: ConfirmRightsRequest
    ) -> dict[str, Any]:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        target_asset = (req.asset or req.asset_file or req.sha256 or "").strip()
        if not target_asset:
            raise HTTPException(status_code=400, detail="asset identifier is required")

        manifest_path = root / "research" / "asset-manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=404, detail="asset-manifest.json not found")

        # Verify asset matches by exact file or sha256 (no endswith suffix match)
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        target_norm = target_asset.replace("\\", "/").strip().lstrip("/")
        matched = False
        for item in manifest_data:
            item_file = str(item.get("file", "")).replace("\\", "/").strip().lstrip("/")
            item_sha = str(item.get("sha256", "")).strip()
            item_name = Path(item_file).name
            if target_norm in (item_file, item_name, item_sha):
                matched = True
                break

        if not matched:
            raise HTTPException(
                status_code=404,
                detail=f"Asset '{target_asset}' not found in manifest by exact file or sha256",
            )

        try:
            from app.rights import confirm_asset_rights as do_confirm_asset_rights

            confirmed = do_confirm_asset_rights(
                root,
                asset=target_norm,
                basis=req.basis,
                operator=req.operator,
                evidence=req.evidence,
                projects_dir=resolved_projects_dir,
                db_path=db_path,
            )
            return {
                "status": "updated",
                "asset": target_asset,
                "rights_confirmed_for_preview": True,
                "confirmed_entries": confirmed,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/approve")
    def approve_release_endpoint(project_id: str, req: ApproveReleaseRequest) -> dict[str, Any]:
        if req.human_review is None:
            raise HTTPException(
                status_code=400,
                detail="Human review checklist is required before release approval",
            )
        root = _resolve_project_root(project_id, resolved_projects_dir)
        candidate_path = root / "release-candidate" / "standalone.html"
        if not candidate_path.is_file():
            raise HTTPException(status_code=409, detail="No staged release candidate to review")
        current_candidate_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        if req.human_review.candidate_sha256 != current_candidate_sha:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Reviewed candidate is stale; reload the review page and inspect "
                    "the current candidate"
                ),
            )
        try:
            manifest_sha = approve_release(
                root,
                version=req.version,
                operator_name=req.operator_name,
                notes=req.notes,
                projects_dir=resolved_projects_dir,
                db_path=db_path,
                acknowledge_open_issues=False,
                review_attestation=req.human_review.model_dump(mode="json"),
            )
            return {
                "status": "approved",
                "project_id": project_id,
                "version": req.version,
                "manifest_sha256": manifest_sha,
            }
        except ReleaseApprovalGateError as gate_err:
            raise HTTPException(status_code=400, detail=str(gate_err)) from gate_err
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/revise-section")
    def revise_section_endpoint(project_id: str, req: ReviseSectionRequest) -> dict[str, Any]:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        try:
            new_hashes = revise_section(
                req.section_name,
                req.new_html,
                project_folder=root,
            )
            stage_release_candidate(root)
            return {
                "status": "revised",
                "project_id": project_id,
                "modified_section": req.section_name,
                "section_hashes": new_hashes,
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _compile_ghl_bundle(
        project_id: str,
        root: Path,
        request: Request,
        cdn_base_url: str | None = None,
    ) -> dict[str, Any]:
        source_html_path: Path | None = None
        source_stage = "candidate"
        for candidate_path, stage_label in (
            (root / "delivery" / "approved-homepage.html", "approved_delivery"),
            (root / "release-candidate" / "standalone.html", "release_candidate"),
            (root / "website" / "index.html", "website_draft"),
        ):
            if candidate_path.is_file():
                source_html_path = candidate_path
                source_stage = stage_label
                break

        if source_html_path is None:
            raise HTTPException(status_code=404, detail="No compiled HTML found for GHL export")

        base_origin = str(request.base_url).rstrip("/")
        effective_cdn = (cdn_base_url or f"{base_origin}/assets/{project_id}").strip().rstrip("/")
        raw_html = source_html_path.read_text(encoding="utf-8")
        normalized_html = raw_html.replace('src="../assets/', 'src="assets/')
        ghl_html = export_ghl_snippet_from_html(
            normalized_html,
            business_id=project_id,
            cdn_base_url=effective_cdn,
        )
        root_id = f"ghl-ev-engine-root-{project_id}"

        host_page_html = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "  <title>GoHighLevel Funnel Host Simulator &mdash; "
            f"{html.escape(project_id)}</title>\n"
            "  <style>\n"
            "    /* Hostile GoHighLevel Funnel Builder Global Reset */\n"
            "    body { margin: 0; background: #0f172a; font-family: system-ui, sans-serif; }\n"
            "    .ghl-sim-bar {\n"
            "      background: #0f172a; color: #f8fafc; padding: 10px 20px;\n"
            "      display: flex; justify-content: space-between; align-items: center;\n"
            "      border-bottom: 2px solid #22c55e; font-size: 13px;\n"
            "    }\n"
            "    .ghl-sim-pill {\n"
            "      background: #14532d; color: #86efac; padding: 4px 10px;\n"
            "      border-radius: 999px; font-weight: 600; font-size: 12px;\n"
            "    }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <div class=\"ghl-sim-bar\">\n"
            "    <div><strong>GoHighLevel Funnel Host Simulator</strong> "
            f"&bull; Root Container: <code>#{html.escape(root_id)}</code></div>\n"
            "    <div class=\"ghl-sim-pill\">Scoped CSS + IIFE JS Isolation Verified</div>\n"
            "  </div>\n"
            f"{ghl_html}\n"
            "</body>\n"
            "</html>"
        )
        sim_report = validate_ghl_embed_simulation(ghl_html, root_id)
        return {
            "project_id": project_id,
            "root_id": root_id,
            "source_stage": source_stage,
            "cdn_base_url": effective_cdn,
            "ghl_html": ghl_html,
            "host_page_html": host_page_html,
            "simulation": sim_report,
        }

    @app.get("/api/projects/{project_id}/ghl-export")
    def ghl_export_endpoint(
        project_id: str,
        request: Request,
        cdn_base_url: str | None = None,
    ) -> dict[str, Any]:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        bundle = _compile_ghl_bundle(project_id, root, request, cdn_base_url=cdn_base_url)
        return {
            "project_id": bundle["project_id"],
            "root_id": bundle["root_id"],
            "source_stage": bundle["source_stage"],
            "cdn_base_url": bundle["cdn_base_url"],
            "ghl_html": bundle["ghl_html"],
            "simulation": bundle["simulation"],
        }

    @app.get("/ghl-preview/{project_id}", response_class=HTMLResponse)
    def ghl_preview_page(
        project_id: str,
        request: Request,
        cdn_base_url: str | None = None,
    ) -> HTMLResponse:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        bundle = _compile_ghl_bundle(project_id, root, request, cdn_base_url=cdn_base_url)
        return HTMLResponse(content=bundle["host_page_html"])

    @app.get("/preview/{project_id}/{subpath:path}")
    def serve_preview(project_id: str, subpath: str) -> Response:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        target: Path | None = None
        for base in (root / "release-candidate", root / "website", root):
            try:
                candidate = _resolve_safe_file(base, subpath)
                target = candidate
                break
            except HTTPException:
                continue

        if not target or not target.is_file():
            raise HTTPException(status_code=404, detail="Preview file not found")

        mime, _ = mimetypes.guess_type(target.name)
        content_type = mime or "application/octet-stream"
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
            html_str = target.read_text(encoding="utf-8")
            html_str = html_str.replace(
                'src="../assets/', f'src="/assets/{project_id}/assets/'
            )
            return Response(content=html_str.encode("utf-8"), media_type=content_type)
        return Response(content=target.read_bytes(), media_type=content_type)

    @app.get("/screenshots/{project_id}/{filename}")
    def serve_screenshot(project_id: str, filename: str) -> Response:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        target = _resolve_safe_file(root / "qa" / "screenshots", filename)
        return Response(content=target.read_bytes(), media_type="image/png")

    @app.get("/assets/{project_id}/{subpath:path}")
    def serve_asset(project_id: str, subpath: str) -> Response:
        root = _resolve_project_root(project_id, resolved_projects_dir)
        target = _resolve_safe_file(root, subpath)
        mime, _ = mimetypes.guess_type(target.name)
        return Response(content=target.read_bytes(), media_type=mime or "image/jpeg")

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EditVista Operator Cockpit Web Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8787, help="Port to listen on")
    parser.add_argument("--projects-dir", default="./projects", help="Projects directory root")
    args = parser.parse_args(argv)

    cockpit_app = create_cockpit_app(projects_dir=args.projects_dir)
    print(f"Starting EditVista Operator Cockpit on http://{args.host}:{args.port}")
    uvicorn.run(cockpit_app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
