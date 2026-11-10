"""Human Review Package, Release Freezing, and Approval-Gated Export for EditVista.

Implements:
- `generate_review_summary(project_id, ...)`: Aggregates verified vs. `needs_review` fact
  metrics, curated asset `rights_confirmed_for_preview` flags, 5-viewport Playwright
  screenshot relative paths, visual critique status, and technical QA status.
- `export_approved_deliverables(project_folder, ...)`: Conditionally locks compilation of
  `delivery/approved-homepage.html` and `delivery/ghl-ready.html` unless `is_approved=True`
  or a non-empty operator `approval_token` is supplied.
- `approve_and_freeze_release(project_id, operator_name, notes, ...)`: Computes a
  deterministic SHA-256 checksum tree across `website/` and `delivery/`, writes
  `delivery/release-manifest.json`, compiles `projects/_archives/<business-id>-approved.zip`,
  and atomically updates the project status to `approved` in SQLite via `immediate_transaction`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.cleanup import make_archive
from app.db import ProjectRecord, connect_db, get_project, init_db, upsert_project
from app.ghl_exporter import export_ghl_snippet, export_ghl_snippet_from_html
from app.reviser import compute_section_hashes
from engine.exporter import check_used_asset_rights, extract_used_assets


class ReleaseApprovalGateError(PermissionError):
    """Raised when attempting to export final deliverables without human approval."""


class ReleaseVerificationError(ValueError):
    """Raised when delivery/ files do not match delivery/release-manifest.json."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _make_writable(target: Path) -> None:
    if target.is_file():
        try:
            os.chmod(target, target.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _make_writable(target)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        _make_writable(target)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _resolve_project_root(
    project_id_or_path: str | Path,
    projects_dir: str | Path = "./projects",
) -> Path:
    candidate = Path(project_id_or_path)
    if candidate.is_dir():
        return candidate.resolve()
    return (Path(projects_dir).resolve() / str(project_id_or_path)).resolve()


class ReleaseManifest(BaseModel):
    """Immutable release manifest saved to `delivery/release-manifest.json`."""

    model_config = ConfigDict(extra="allow")

    project_id: str
    version: str = "v1"
    version_hash: str
    approved_at: datetime = Field(default_factory=_utc_now)
    approver: str
    operator_name: str
    qa_report_sha256: str = Field(default="")
    notes: str
    checksum_tree: dict[str, str]
    section_hashes: dict[str, str] = Field(default_factory=dict)
    asset_signatures: dict[str, str] = Field(default_factory=dict)
    acknowledged_issues: list[str] = Field(default_factory=list)


def generate_review_summary(
    project_id: str,
    *,
    projects_dir: str | Path = "./projects",
) -> dict[str, Any]:
    """Assemble the side-by-side human review package summary for a project."""
    project_root = _resolve_project_root(project_id, projects_dir)
    if not project_root.is_dir():
        raise ValueError(f"Project directory not found: {project_root}")

    # 1. Fact verification metrics
    facts_path = project_root / "research" / "business-facts.json"
    verified_count = 0
    needs_review_count = 0
    if facts_path.is_file():
        facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
        for fact in facts_payload.get("facts", []):
            if bool(fact.get("is_verified")) and fact.get("status") == "verified":
                verified_count += 1
            else:
                needs_review_count += 1

    # 2. Curated assets & rights_confirmed_for_preview status
    asset_manifest_path = project_root / "research" / "asset-manifest.json"
    curated_assets: list[dict[str, Any]] = []
    if asset_manifest_path.is_file():
        raw_assets = json.loads(asset_manifest_path.read_text(encoding="utf-8"))
        if isinstance(raw_assets, list):
            for item in raw_assets:
                if isinstance(item, dict) and item.get("file"):
                    curated_assets.append(
                        {
                            "file": str(item["file"]),
                            "sha256": str(item.get("sha256", "")),
                            "source_url": str(item.get("url") or item.get("source_url") or ""),
                            "rights_confirmed_for_preview": bool(
                                item.get("rights_confirmed_for_preview", False)
                            ),
                        }
                    )

    # 3. Relative paths to 5-viewport Playwright screenshots
    screenshots_dir = project_root / "qa" / "screenshots"
    screenshots: list[str] = []
    if screenshots_dir.is_dir():
        for width in (1440, 1024, 768, 390, 360):
            shot = screenshots_dir / f"{width}px.png"
            if shot.is_file():
                screenshots.append(shot.relative_to(project_root).as_posix())

    # 4. Technical QA & Visual Critique statuses
    qa_report_path = project_root / "qa" / "qa-report.json"
    technical_qa_passed = False
    if qa_report_path.is_file():
        qa_data = json.loads(qa_report_path.read_text(encoding="utf-8"))
        technical_qa_passed = bool(qa_data.get("passed", False))

    critique_path = project_root / "qa" / "visual-critique.json"
    visual_critique_status = "pending"
    visual_defects: list[dict[str, Any]] = []
    if critique_path.is_file():
        crit_data = json.loads(critique_path.read_text(encoding="utf-8"))
        visual_critique_status = str(crit_data.get("status", "pending"))
        visual_defects = list(crit_data.get("defects", []))

    return {
        "project_id": project_root.name,
        "fact_metrics": {
            "verified_count": verified_count,
            "needs_review_count": needs_review_count,
            "total_facts": verified_count + needs_review_count,
        },
        "assets": curated_assets,
        "screenshots": screenshots,
        "technical_qa_passed": technical_qa_passed,
        "visual_critique_status": visual_critique_status,
        "visual_defects": visual_defects,
    }


def stage_release_candidate(project_folder: str | Path) -> tuple[Path, Path]:
    """Compile candidate standalone HTML and GHL snippet into release-candidate/ (not delivery/)."""
    project_root = Path(project_folder).resolve()
    website_dir = project_root / "website"
    index_path = website_dir / "index.html"
    if not index_path.is_file():
        raise ValueError(f"Missing website/index.html in {project_root}")

    css_path = website_dir / "css" / "styles.css"
    js_path = website_dir / "js" / "main.js"

    html_doc = index_path.read_text(encoding="utf-8")
    if css_path.is_file():
        css_text = css_path.read_text(encoding="utf-8")
        html_doc = html_doc.replace(
            '<link rel="stylesheet" href="css/styles.css">',
            f"<style>\n{css_text}\n</style>",
        )
    if js_path.is_file():
        js_text = js_path.read_text(encoding="utf-8")
        html_doc = html_doc.replace(
            '<script src="js/main.js"></script>',
            f"<script>\n{js_text}\n</script>",
        )

    rc_dir = project_root / "release-candidate"
    rc_dir.mkdir(parents=True, exist_ok=True)

    standalone_path = rc_dir / "standalone.html"
    approved_alias_path = rc_dir / "approved-homepage.html"
    _atomic_write_text(standalone_path, html_doc)
    _atomic_write_text(approved_alias_path, html_doc)

    ghl_snippet = export_ghl_snippet_from_html(html_doc, business_id=project_root.name)
    ghl_path = rc_dir / "ghl-ready.html"
    _atomic_write_text(ghl_path, ghl_snippet)

    return standalone_path, ghl_path


def export_approved_deliverables(
    project_folder: str | Path,
    *,
    is_approved: bool = False,
    approval_token: str | None = None,
) -> tuple[Path, Path]:
    """Compile approved-homepage.html and ghl-ready.html behind the human approval gate."""
    has_valid_token = isinstance(approval_token, str) and len(approval_token.strip()) >= 4
    if not is_approved and not has_valid_token:
        raise ReleaseApprovalGateError(
            "Deliverable export is locked: require is_approved=True or valid approval_token"
        )

    project_root = Path(project_folder).resolve()
    standalone_path, _ = stage_release_candidate(project_root)

    delivery_dir = project_root / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    approved_html_path = delivery_dir / "approved-homepage.html"
    shutil.copy2(standalone_path, approved_html_path)

    export_ghl_snippet(project_root, business_id=project_root.name)
    ghl_ready_path = delivery_dir / "ghl-ready.html"

    # Ensure delivery/retained-assets.json exists and is marked approved=True
    retained_path = delivery_dir / "retained-assets.json"
    retained_assets: list[str] = []
    if retained_path.is_file():
        existing_ret = json.loads(retained_path.read_text(encoding="utf-8"))
        retained_assets = [str(a) for a in existing_ret.get("assets", [])]
    else:
        opt_dir = project_root / "assets" / "optimized"
        if opt_dir.is_dir():
            for f in sorted(opt_dir.iterdir()):
                if f.is_file() and not f.is_symlink():
                    retained_assets.append(f.relative_to(project_root).as_posix())

    _atomic_write_text(
        retained_path,
        json.dumps({"approved": True, "assets": retained_assets}, indent=2),
    )

    return approved_html_path, ghl_ready_path


def compute_release_checksum_tree(project_root: Path) -> tuple[str, dict[str, str], dict[str, str]]:
    """Compute a deterministic SHA-256 tree across website/, delivery/, and retained assets."""
    checksum_tree: dict[str, str] = {}
    for folder_name in ("website", "delivery"):
        folder = project_root / folder_name
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(project_root).as_posix()
            if rel == "delivery/release-manifest.json":
                continue
            checksum_tree[rel] = hashlib.sha256(p.read_bytes()).hexdigest()

    asset_signatures: dict[str, str] = {}
    retained_path = project_root / "delivery" / "retained-assets.json"
    if retained_path.is_file():
        ret_data = json.loads(retained_path.read_text(encoding="utf-8"))
        for rel_asset in sorted(set(str(x) for x in ret_data.get("assets", []))):
            asset_file = project_root / rel_asset
            if asset_file.is_file() and not asset_file.is_symlink():
                asset_signatures[rel_asset] = hashlib.sha256(asset_file.read_bytes()).hexdigest()

    tree_hasher = hashlib.sha256()
    for rel_path, file_sha in sorted(checksum_tree.items()):
        tree_hasher.update(f"{rel_path}:{file_sha}\n".encode())
    for rel_asset, asset_sha in sorted(asset_signatures.items()):
        tree_hasher.update(f"asset:{rel_asset}:{asset_sha}\n".encode())

    return tree_hasher.hexdigest(), checksum_tree, asset_signatures


def approve_release(
    project_id_or_path: str | Path,
    version: str = "v1",
    operator_name: str | None = None,
    notes: str | None = None,
    *,
    projects_dir: str | Path = "./projects",
    db_path: str | Path | None = None,
    acknowledge_open_issues: bool | None = None,
) -> str:
    """Human approval gate: verifies QA/rights, copies candidate to delivery/ & versions/vN/,

    writes release-manifest.json with per-file and per-section hashes, and makes files read-only.
    """
    clean_op = operator_name.strip() if operator_name is not None else None
    if clean_op is not None and not clean_op:
        raise ValueError("operator_name must not be empty")

    clean_notes = notes.strip() if notes is not None else None
    if clean_notes is not None and not clean_notes:
        raise ValueError("notes must not be empty")

    project_root = _resolve_project_root(project_id_or_path, projects_dir)
    if not project_root.is_dir():
        raise ValueError(f"Project directory not found: {project_root}")

    resolved_project_id = project_root.name
    version = (version or "v1").strip()
    if ".." in version or "/" in version or "\\" in version:
        raise ValueError(f"Invalid version identifier: {version}")

    # 1. Refuse to overwrite an existing approved version
    version_dir = (project_root / "versions" / version).resolve()
    if not version_dir.is_relative_to((project_root / "versions").resolve()):
        raise ValueError(f"Invalid version path traversal: {version}")
    if version_dir.exists():
        raise ReleaseApprovalGateError(
            f"Version {version} already exists and cannot be overwritten"
        )

    # 2. Refuse if QA report has critical defects
    qa_path = project_root / "qa" / "qa-report.json"
    if not qa_path.is_file():
        raise ReleaseApprovalGateError("QA report not found; cannot authorize release")
    qa_data = json.loads(qa_path.read_text(encoding="utf-8"))
    if not qa_data.get("passed", False) or qa_data.get("critical_defects"):
        defects = qa_data.get("critical_defects") or ["QA report passed is false"]
        raise ReleaseApprovalGateError(f"QA report detected critical defects: {defects}")

    # Ensure release-candidate/ is staged
    rc_dir = project_root / "release-candidate"
    if not (rc_dir / "standalone.html").is_file():
        stage_release_candidate(project_root)

    candidate_html = rc_dir / "standalone.html"
    current_candidate_sha = hashlib.sha256(candidate_html.read_bytes()).hexdigest()

    # Refuse if QA report candidate sha256 differs from current candidate (stale QA)
    qa_candidate_sha = qa_data.get("candidate_sha256")
    if not qa_candidate_sha or qa_candidate_sha != current_candidate_sha:
        raise ReleaseApprovalGateError(
            f"Stale QA report: tested candidate sha256 ({qa_candidate_sha}) "
            f"differs from current candidate ({current_candidate_sha})"
        )

    # 3. Refuse if any referenced asset has rights_confirmed_for_preview == False
    offending_assets = check_used_asset_rights(project_root, source_html_path=candidate_html)
    if offending_assets:
        raise ReleaseApprovalGateError(
            f"Release blocked: asset rights_confirmed_for_preview is false for {offending_assets}"
        )

    # 3b. Refuse if each used asset has no matching audit entry for its CURRENT sha256
    from app.rights import check_used_assets_audit_signatures

    used_assets = extract_used_assets(candidate_html.read_text(encoding="utf-8"))
    audit_file = project_root / "research" / "rights-audit.jsonl"
    if used_assets and not audit_file.is_file():
        raise ReleaseApprovalGateError(
            f"Release blocked: missing rights audit log (research/rights-audit.jsonl). "
            f"Audit check is mandatory for every used asset ({used_assets})."
        )
    audit_violations = check_used_assets_audit_signatures(project_root, used_assets)
    if audit_violations:
        raise ReleaseApprovalGateError(
            f"Release blocked: used asset has no matching audit entry for its CURRENT sha256: "
            f"{audit_violations}"
        )

    # 3c. Refuse if open visual defects or unverified facts are present unless acknowledged
    open_issues: list[str] = []
    critique_path = project_root / "qa" / "visual-critique.json"
    if critique_path.is_file():
        try:
            crit_data = json.loads(critique_path.read_text(encoding="utf-8"))
            if crit_data.get("status") == "defects_found" or crit_data.get("defects"):
                for d in crit_data.get("defects", []):
                    desc = d.get("description") if isinstance(d, dict) else str(d)
                    open_issues.append(f"visual_critique_defect: {desc}")
        except Exception:
            pass

    facts_path = project_root / "research" / "business-facts.json"
    if facts_path.is_file():
        try:
            facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
            cand_html_text = candidate_html.read_text(encoding="utf-8")
            for fact in facts_payload.get("facts", []):
                if not bool(fact.get("is_verified", False)) or fact.get("status") == "needs_review":
                    claim_val = str(fact.get("claim_value", "")).strip()
                    if claim_val and claim_val in cand_html_text:
                        open_issues.append(
                            f"unverified_fact_used: {fact.get('category')}."
                            f"{fact.get('claim_key')}='{claim_val}'"
                        )
        except Exception:
            pass

    if open_issues and acknowledge_open_issues is False:
        raise ReleaseApprovalGateError(
            f"Release blocked: open issues detected (visual critique defects or unverified facts): "
            f"{open_issues}. Pass --acknowledge-open-issues to approve."
        )

    # 4. Copy candidate into delivery/ and versions/vN/
    delivery_dir = project_root / "delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    version_dir.mkdir(parents=True, exist_ok=True)

    used_assets = extract_used_assets(candidate_html.read_text(encoding="utf-8"))
    retained_payload = {
        "approved": True,
        "assets": used_assets,
    }
    _atomic_write_text(
        delivery_dir / "retained-assets.json",
        json.dumps(retained_payload, indent=2),
    )

    for fname in ("standalone.html", "approved-homepage.html", "ghl-ready.html"):
        src_f = rc_dir / fname
        if src_f.is_file():
            _make_writable(delivery_dir / fname)
            shutil.copy2(src_f, delivery_dir / fname)
            shutil.copy2(src_f, version_dir / fname)

    _make_writable(delivery_dir / "retained-assets.json")
    shutil.copy2(delivery_dir / "retained-assets.json", version_dir / "retained-assets.json")

    # Snapshot referenced assets into version_dir so the version is self-contained
    for rel_asset in used_assets:
        src_asset = (project_root / rel_asset).resolve()
        if src_asset.is_relative_to(project_root) and src_asset.is_file():
            dst_asset = version_dir / rel_asset
            dst_asset.parent.mkdir(parents=True, exist_ok=True)
            _make_writable(dst_asset)
            shutil.copy2(src_asset, dst_asset)

    # 5. Compute per-section hashes and release manifest
    approved_html_text = (delivery_dir / "approved-homepage.html").read_text(encoding="utf-8")
    section_hashes = compute_section_hashes(
        approved_html_text, project_folder=project_root / "website"
    )

    version_hash, checksum_tree, asset_signatures = compute_release_checksum_tree(project_root)

    qa_report_sha256 = hashlib.sha256(qa_path.read_bytes()).hexdigest()
    final_op = clean_op if clean_op is not None else "Dev"
    final_notes = clean_notes if clean_notes is not None else "Verified"
    manifest = ReleaseManifest(
        project_id=resolved_project_id,
        version=version,
        version_hash=version_hash,
        approved_at=_utc_now(),
        approver=final_op,
        operator_name=final_op,
        qa_report_sha256=qa_report_sha256,
        notes=final_notes,
        checksum_tree=checksum_tree,
        section_hashes=section_hashes,
        asset_signatures=asset_signatures,
        acknowledged_issues=open_issues if acknowledge_open_issues else [],
    )
    manifest_json = manifest.model_dump_json(indent=2)
    _atomic_write_text(delivery_dir / "release-manifest.json", manifest_json)
    _atomic_write_text(version_dir / "release-manifest.json", manifest_json)

    # 6. Make all files in delivery/ and versions/vN/ read-only
    for folder in (delivery_dir, version_dir):
        for fpath in folder.rglob("*"):
            if fpath.is_file():
                current_mode = fpath.stat().st_mode
                os.chmod(fpath, current_mode & ~stat.S_IWRITE)

    # 7. Compile immutable archive projects/_archives/<business-id>-approved.zip
    make_archive(project_root)

    # 8. Update SQLite project record
    resolved_db = (
        Path(db_path).resolve()
        if db_path is not None
        else (project_root.parent / "editvista.sqlite3")
    )
    conn = connect_db(resolved_db)
    try:
        init_db(conn)
        existing = get_project(conn, resolved_project_id)
        biz_name = existing.business_name if existing else resolved_project_id
        primary_url = existing.primary_url if existing else f"https://{resolved_project_id}.local/"
        upsert_project(
            conn,
            ProjectRecord(
                project_id=resolved_project_id,
                business_name=biz_name,
                primary_url=primary_url,
                status="approved",
            ),
        )
    finally:
        conn.close()

    return version_hash


def approve_and_freeze_release(
    project_id: str,
    operator_name: str,
    notes: str,
    *,
    version: str = "v1",
    projects_dir: str | Path = "./projects",
    db_path: str | Path | None = None,
) -> str:
    """Freeze an immutable release hash, write manifest, build ZIP archive, and update SQLite."""
    return approve_release(
        project_id_or_path=project_id,
        version=version,
        operator_name=operator_name,
        notes=notes,
        projects_dir=projects_dir,
        db_path=db_path,
    )


def verify_release(project_folder: str | Path) -> bool:
    """Re-hash delivery/ against delivery/release-manifest.json and refuse on mismatch."""
    project_root = Path(project_folder).resolve()
    delivery_dir = project_root / "delivery"
    manifest_path = delivery_dir / "release-manifest.json"
    if not manifest_path.is_file():
        raise ReleaseVerificationError(f"Release manifest not found at {manifest_path}")

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    checksum_tree: dict[str, str] = manifest_data.get("checksum_tree", {})

    for rel_path, expected_sha in sorted(checksum_tree.items()):
        if not rel_path.startswith("delivery/"):
            continue
        disk_file = project_root / rel_path
        if not disk_file.is_file():
            raise ReleaseVerificationError(f"Missing delivery file: {rel_path}")
        actual_sha = hashlib.sha256(disk_file.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise ReleaseVerificationError(
                f"Delivery file hash mismatch for {rel_path}: "
                f"expected {expected_sha}, got {actual_sha}"
            )

    for p in sorted(delivery_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(project_root).as_posix()
        if rel == "delivery/release-manifest.json":
            continue
        if rel not in checksum_tree:
            raise ReleaseVerificationError(f"Untracked delivery file found: {rel}")

    # Verify all released asset signatures recorded in release-manifest.json
    asset_signatures: dict[str, str] = manifest_data.get("asset_signatures", {})
    for rel_asset, expected_asset_sha in sorted(asset_signatures.items()):
        asset_file = project_root / rel_asset
        if not asset_file.is_file():
            raise ReleaseVerificationError(f"Missing released asset: {rel_asset}")
        actual_asset_sha = hashlib.sha256(asset_file.read_bytes()).hexdigest()
        if actual_asset_sha != expected_asset_sha:
            raise ReleaseVerificationError(
                f"Released asset hash mismatch for {rel_asset}: "
                f"expected {expected_asset_sha}, got {actual_asset_sha}"
            )

    return True
