"""Master End-to-End Orchestrator & State Machine for EditVista Pet Grooming Website Builder Engine.

Full Lifecycle (Section 3 of Developer Brief):
Collection -> Normalization -> Extraction -> Curation -> Art Direction -> Generation ->
Critic & QA -> Targeted Revision Loop (max 3 cycles) -> Human Approval Gate ->
Frozen GHL Export & Release Archive -> Safe Cleanup.

CLI Interface:
1. Build / Resume:
   `python -m app.pipeline --url <URL> --name <NAME> [--resume]`
2. Import client images:
   `python -m app.pipeline import-client-assets --project-id <ID> --folder <DIR>`
3. Approve & Freeze Release:
   `python -m app.pipeline approve --project-id <ID> --operator "Dev" --notes "Verified"`
4. Approval-Gated Cleanup:
   `python -m app.pipeline prune --project-id <ID> [--execute]`
5. Operator Cockpit Web Dashboard:
   `python -m app.pipeline cockpit [--port 8787] [--projects-dir ./projects]`
6. Milestone 5 Acceptance Pilot:
   `python -m app.pipeline pilot [--run-all]`
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, Tag
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from app.approval import (
    _resolve_project_root,
    approve_release,
    generate_review_summary,
    stage_release_candidate,
    verify_release,
)
from app.art_director import generate_art_direction
from app.claims_lint import run_claims_lint
from app.cleanup import safe_prune_project
from app.collector import collect, slugify
from app.curator import curate_project_images
from app.db import (
    AssetProvenanceRecord,
    BusinessFactRecord,
    JobRunRecord,
    ProjectRecord,
    connect_db,
    get_project,
    init_db,
    list_assets_for_project,
    mark_running_jobs_interrupted,
    upsert_asset_provenance,
    upsert_business_fact,
    upsert_job_run,
    upsert_project,
)
from app.fact_extractor import (
    BusinessFactsReport,
    ExtractedFact,
    extract_business_facts,
)
from app.generator import generate_website
from app.mvf_gate import enforce_mvf_gate
from app.qa_engine import QADefectReport, run_qa_engine
from app.reviser import MAX_REVISION_ITERATIONS, apply_targeted_revisions
from app.visual_critic import run_visual_critic
from engine.contact_sheet import make_sheet
from engine.contracts import freeze_demo_metadata, read_demo_metadata
from engine.social_collect import collect_social, scrub_sensitive_text

QARunnerFn = Callable[..., QADefectReport]
PIPELINE_STAGE_NAMES = (
    "collect",
    "extract",
    "curate",
    "art_director",
    "generate",
    "claims_lint",
    "qa",
    "critique",
    "revise",
    "ghl_exporter",
)
CLIENT_IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


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


def build_stage_idempotency_key(
    project_id: str,
    stage_name: str,
    input_manifest_hash: str,
) -> str:
    """Format the canonical SQLite idempotency key for a pipeline stage."""
    return f"{project_id}_{stage_name}_{input_manifest_hash}"


def compute_input_manifest_hash(project_root: Path, input_rel_paths: Sequence[str]) -> str:
    """Compute a deterministic SHA-256 digest across stage input files."""
    hasher = hashlib.sha256()
    for rel in sorted(input_rel_paths):
        p = project_root / rel
        if p.is_file():
            hasher.update(f"{rel}:".encode())
            hasher.update(hashlib.sha256(p.read_bytes()).digest())
        else:
            hasher.update(f"{rel}:missing\n".encode())
    return hasher.hexdigest()[:16]


def _normalized_business_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
    return re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()


def _business_name_comparison(
    project_root: Path,
    business_name: str,
    facts_report: BusinessFactsReport,
) -> dict[str, Any]:
    pages_path = project_root / "research" / "pages.json"
    pages: list[Any] = json.loads(pages_path.read_text(encoding="utf-8"))
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for page in pages:
        if not isinstance(page, dict):
            continue
        source_url = page.get("url")
        if not isinstance(source_url, str) or not source_url.strip():
            continue

        page_title = page.get("title")
        if isinstance(page_title, str) and page_title.strip():
            candidate = {
                "source_field": "title",
                "source_url": source_url,
                "source_text": page_title.strip(),
            }
            key = (candidate["source_field"], source_url, candidate["source_text"])
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

        snapshot_hash = page.get("sha256")
        if not isinstance(snapshot_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash):
            continue
        snapshot_path = project_root / "research" / "snapshots" / f"{snapshot_hash}.html"
        if not snapshot_path.is_file():
            continue
        soup = BeautifulSoup(snapshot_path.read_text(encoding="utf-8"), "html.parser")
        site_name_tag = soup.find("meta", attrs={"property": "og:site_name"})
        site_name = (
            site_name_tag.get("content")
            if isinstance(site_name_tag, Tag)
            else None
        )
        if isinstance(site_name, str) and site_name.strip():
            candidate = {
                "source_field": "og:site_name",
                "source_url": source_url,
                "source_text": site_name.strip(),
            }
            key = (candidate["source_field"], source_url, candidate["source_text"])
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

    normalized_operator_name = _normalized_business_name(business_name)
    comparisons: list[dict[str, Any]] = []
    matching_candidate: dict[str, str] | None = None
    for candidate in candidates:
        normalized_source = _normalized_business_name(candidate["source_text"])
        matches = bool(normalized_operator_name) and (
            normalized_operator_name == normalized_source
        )
        if matches and matching_candidate is None:
            matching_candidate = candidate
        diff = "\n".join(
            difflib.unified_diff(
                normalized_operator_name.split(),
                normalized_source.split(),
                fromfile="--name",
                tofile=candidate["source_field"],
                lineterm="",
            )
        )
        comparisons.append(
            {
                **candidate,
                "normalized_operator_name": normalized_operator_name,
                "normalized_source_text": normalized_source,
                "matches": matches,
                "diff": diff,
            }
        )

    for fact in facts_report.facts:
        if fact.category == "business_name":
            fact.is_verified = False
            fact.status = "needs_review"
            fact.last_checked_at = datetime.now(UTC)

    if matching_candidate is not None:
        matching_fact = next(
            (
                fact
                for fact in facts_report.facts
                if fact.category == "business_name"
                and _normalized_business_name(fact.claim_value) == normalized_operator_name
            ),
            None,
        )
        if matching_fact is None:
            digest = hashlib.sha256(
                (
                    f"{project_root.name}:business_name:business_name:"
                    f"{business_name}:{matching_candidate['source_url']}"
                ).encode()
            ).hexdigest()[:12]
            matching_fact = ExtractedFact(
                fact_id=f"fact-{digest}",
                project_id=project_root.name,
                category="business_name",
                claim_key="business_name",
                claim_value=business_name,
                source_url=matching_candidate["source_url"],
                source_text_snippet=matching_candidate["source_text"],
                source_kind="manual",
                is_verified=True,
                status="verified",
            )
            facts_report.facts.append(matching_fact)
        else:
            matching_fact.claim_value = business_name
            matching_fact.source_url = matching_candidate["source_url"]
            matching_fact.source_text_snippet = matching_candidate["source_text"]
            matching_fact.source_kind = "manual"
            matching_fact.is_verified = True
            matching_fact.status = "verified"
            matching_fact.last_checked_at = datetime.now(UTC)

    report = {
        "operator_name": business_name,
        "normalized_operator_name": normalized_operator_name,
        "status": "verified" if matching_candidate is not None else "needs_review",
        "comparisons": comparisons,
        "matched_source": matching_candidate,
    }
    payload = facts_report.model_dump(mode="json")
    for fact in payload["facts"]:
        fact["source_text"] = fact["source_text_snippet"]
        fact["verified"] = fact["is_verified"]
    _atomic_write_text(
        project_root / "research" / "business-facts.json",
        json.dumps(payload, indent=2, ensure_ascii=False),
    )
    _atomic_write_text(
        project_root / "research" / "business-name-comparison.json",
        json.dumps(report, indent=2, ensure_ascii=False),
    )
    return report


def _normalized_social_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        return (
            f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}"
            f"{parsed.path.rstrip('/').lower()}"
        )
    except ValueError:
        return value.strip().casefold()


def _project_input_sha256(project_root: Path) -> str | None:
    input_path = project_root / "input.json"
    if not input_path.is_file():
        return None
    return hashlib.sha256(input_path.read_bytes()).hexdigest()


def _social_input_matches_last_collection(project_root: Path) -> bool:
    report_path = project_root / "research" / "social-collection-report.json"
    if not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(report, dict)
        and report.get("input_sha256") == _project_input_sha256(project_root)
    )


def _collect_configured_social(
    project_root: Path,
    warnings: list[str],
) -> dict[str, Any]:
    input_path = project_root / "input.json"
    report_path = project_root / "research" / "social-collection-report.json"
    result: dict[str, Any] = {
        "input_sha256": _project_input_sha256(project_root),
        "platforms": {},
    }
    if not input_path.is_file():
        _atomic_write_text(report_path, json.dumps(result, indent=2))
        return result

    try:
        input_data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result.update(
            status="review_needed",
            reason="input_json_invalid",
            error=str(exc),
        )
        warnings.append("Social URLs need review because input.json could not be read.")
        _atomic_write_text(report_path, json.dumps(result, indent=2, ensure_ascii=False))
        return result
    if not isinstance(input_data, dict):
        result.update(
            status="review_needed",
            reason="input_json_invalid",
            error="input.json must contain a JSON object",
        )
        warnings.append("Social URLs need review because input.json is not an object.")
        _atomic_write_text(report_path, json.dumps(result, indent=2, ensure_ascii=False))
        return result

    sources_path = project_root / "research" / "social-sources.json"
    try:
        source_data = (
            json.loads(sources_path.read_text(encoding="utf-8"))
            if sources_path.is_file()
            else {}
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        source_data = {}
    if not isinstance(source_data, dict):
        source_data = {}

    permission_confirmed = input_data.get("i_have_permission") is True
    for platform in ("instagram", "facebook"):
        field = f"{platform}_url"
        supplied = input_data.get(field)
        if supplied is None or supplied == "":
            continue
        website_urls_raw = source_data.get(platform, [])
        if isinstance(website_urls_raw, str):
            website_urls_raw = [website_urls_raw]
        website_urls = (
            [
                value.strip()
                for value in website_urls_raw
                if isinstance(value, str) and value.strip()
            ]
            if isinstance(website_urls_raw, list)
            else []
        )
        entry: dict[str, Any] = {
            "supplied_url": supplied.strip() if isinstance(supplied, str) else None,
            "website_urls": website_urls,
        }
        result["platforms"][platform] = entry
        if not isinstance(supplied, str) or not supplied.strip():
            entry.update(status="review_needed", reason="invalid_input_url")
        elif website_urls and any(
            _normalized_social_url(supplied) != _normalized_social_url(found)
            for found in website_urls
        ):
            entry.update(
                status="needs_review",
                reason="input_url_differs_from_website",
            )
        elif not permission_confirmed:
            entry.update(status="review_needed", reason="permission_required")
        else:
            try:
                collector_result = collect_social(
                    project_root,
                    platform,
                    supplied.strip(),
                    max_files=40,
                    days_limit=365,
                    i_have_permission=True,
                )
            except Exception as exc:
                entry.update(
                    status="review_needed",
                    reason="social_collector_error",
                    error=scrub_sensitive_text(str(exc))[-500:],
                )
            else:
                if not isinstance(collector_result, dict):
                    entry.update(
                        status="review_needed",
                        reason="invalid_social_collector_result",
                    )
                else:
                    collector_status = collector_result.get("status")
                    entry["collector_status"] = collector_status
                    entry["files_added"] = collector_result.get("files_added", 0)
                    if collector_status in {"collected_needs_review", "review_needed"}:
                        entry["status"] = collector_status
                        reason = collector_result.get("reason")
                        if isinstance(reason, str) and reason:
                            entry["reason"] = scrub_sensitive_text(reason)
                    else:
                        entry.update(
                            status="review_needed",
                            reason="unexpected_social_collector_status",
                        )

        if entry.get("status") in {
            "review_needed",
            "needs_review",
            "collected_needs_review",
        }:
            warnings.append(
                f"Social {platform} needs review: {entry.get('reason', 'unknown reason')}."
            )

    _atomic_write_text(report_path, json.dumps(result, indent=2, ensure_ascii=False))
    return result


def _is_stage_completed_and_valid(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    stage_name: str,
    idempotency_key: str,
    project_root: Path,
    required_output_rel_paths: Sequence[str],
) -> bool:
    """Return True if SQLite job_runs shows status='completed' and output artifacts exist."""
    row = conn.execute(
        """
        SELECT status FROM job_runs
        WHERE project_id = ? AND module_name = ? AND idempotency_key = ?
        """,
        (project_id, stage_name, idempotency_key),
    ).fetchone()
    if row is None or str(row["status"]) != "completed":
        return False

    return all((project_root / rel).is_file() for rel in required_output_rel_paths)


def _record_job_transition(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    module_name: str,
    idempotency_key: str,
    status: str,
    error_message: str | None = None,
) -> JobRunRecord:
    run_id = f"{project_id}:{module_name}"
    record = JobRunRecord(
        run_id=run_id,
        project_id=project_id,
        module_name=module_name,
        status=status,
        idempotency_key=idempotency_key,
        error_message=error_message,
    )
    upsert_job_run(conn, record)
    conn.execute(
        "UPDATE job_runs SET idempotency_key = ? WHERE run_id = ?",
        (idempotency_key, run_id),
    )
    return record


def _stage_downloaded_assets_for_curator(
    conn: sqlite3.Connection,
    project_root: Path,
    project_id: str,
) -> list[AssetProvenanceRecord]:
    """Copy downloaded website media into `assets/optimized/` and record SQLite provenance."""
    manifest_path = project_root / "research" / "asset-manifest.json"
    optimized_dir = project_root / "assets" / "optimized"
    optimized_dir.mkdir(parents=True, exist_ok=True)
    provenance_records: list[AssetProvenanceRecord] = []

    if not manifest_path.is_file():
        return provenance_records

    items: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    demo = read_demo_metadata(project_root)
    third_party_demo = (
        demo is not None
        and demo.is_fixture
        and demo.kind == "third_party_internal_demo"
    )
    manifest_changed = False
    for idx, item in enumerate(items, start=1):
        if third_party_demo and item.get("rights_confirmed_for_preview") is not False:
            item["rights_confirmed_for_preview"] = False
            manifest_changed = True
        if item.get("status") not in {"downloaded", "client_supplied"}:
            continue
        rel_file = str(item.get("file", ""))
        sha256 = str(item.get("sha256", ""))
        src_url = str(item.get("source_url") or item.get("url") or "")
        if not rel_file or not sha256:
            continue

        src_path = project_root / rel_file
        if not src_path.is_file():
            continue

        dest_path = optimized_dir / src_path.name
        if not dest_path.is_file():
            shutil.copy2(src_path, dest_path)

        # Preserve the manifest's rights decision in SQLite.
        rec = AssetProvenanceRecord(
            asset_id=f"{project_id}-asset-{idx}-{sha256[:10]}",
            project_id=project_id,
            sha256_hash=sha256,
            source_url=src_url or "https://local.invalid/asset",
            local_path=rel_file,
            rights_confirmed_for_preview=(
                False
                if third_party_demo
                else bool(item.get("rights_confirmed_for_preview", False))
            ),
        )
        upsert_asset_provenance(conn, rec)
        provenance_records.append(rec)

    if manifest_changed:
        _atomic_write_text(manifest_path, json.dumps(items, indent=2))

    return provenance_records


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def import_client_assets(
    project_root: str | Path,
    folder: str | Path,
    *,
    supplied_by: str = "not_specified",
) -> dict[str, Any]:
    """Copy client images into the website asset manifest with explicit provenance."""
    root = Path(project_root).resolve()
    source_root = Path(folder).resolve()
    if not root.is_dir():
        raise ValueError(f"Project folder does not exist: {root}")
    if not source_root.is_dir():
        raise ValueError(f"Client asset folder does not exist: {source_root}")
    clean_supplier = supplied_by.strip()
    if not clean_supplier:
        raise ValueError("supplied_by must not be empty")

    all_files = sorted(path for path in source_root.rglob("*") if path.is_file())
    image_files = [
        path for path in all_files if path.suffix.lower() in CLIENT_IMAGE_EXTENSIONS
    ]
    ignored_files = [
        path.relative_to(source_root).as_posix()
        for path in all_files
        if path.suffix.lower() not in CLIENT_IMAGE_EXTENSIONS
    ]
    if not image_files:
        raise ValueError(f"No supported client image files found in {source_root}")

    prepared: list[dict[str, Any]] = []
    for source in image_files:
        if source.is_symlink() or not source.resolve().is_relative_to(source_root):
            raise ValueError(
                f"Client image must be a regular file inside the source folder: {source}"
            )
        try:
            with Image.open(source) as image:
                dimensions = [image.width, image.height]
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError(f"Invalid client image {source.name}: {exc}") from exc
        prepared.append(
            {
                "path": source,
                "original_filename": source.name,
                "sha256": _file_sha256(source),
                "dimensions": dimensions,
            }
        )

    manifest_path = root / "research" / "asset-manifest.json"
    if manifest_path.is_file():
        rows = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(
                f"Invalid asset manifest at {manifest_path}: expected a list of objects"
            )
    else:
        rows = []

    seen_hashes: set[str] = set()
    for row in rows:
        relative_file = row.get("file")
        expected_sha = row.get("sha256")
        if not isinstance(relative_file, str) or not isinstance(expected_sha, str):
            continue
        try:
            existing_path = (root / relative_file).resolve()
            existing_path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if (
            existing_path.is_file()
            and _file_sha256(existing_path) == expected_sha
        ):
            seen_hashes.add(expected_sha)

    website_assets = root / "assets" / "website"
    imported_rows: list[dict[str, Any]] = []
    duplicates = 0
    for item in prepared:
        sha256 = str(item["sha256"])
        if sha256 in seen_hashes:
            duplicates += 1
            continue
        source = Path(item["path"])
        filename = f"{slugify(source.stem)}-{sha256}{source.suffix.lower()}"
        destination = website_assets / filename
        _copy_file_atomically(source, destination)
        source_ref = (
            f"client-supplied://sha256/{sha256}/{quote(str(item['original_filename']), safe='')}"
        )
        row = {
            "file": destination.relative_to(root).as_posix(),
            "source": "client_supplied",
            "source_url": source_ref,
            "url": source_ref,
            "source_page": source_ref,
            "original_filename": item["original_filename"],
            "sha256": sha256,
            "dimensions": item["dimensions"],
            "captured_at": datetime.now(UTC).isoformat(),
            "supplied_by": clean_supplier,
            "rights_confirmed_for_preview": False,
            "status": "client_supplied",
            "discovery": "client_import",
        }
        rows.append(row)
        imported_rows.append(row)
        seen_hashes.add(sha256)

    _atomic_write_text(manifest_path, json.dumps(rows, indent=2, ensure_ascii=False))
    return {
        "assets_imported": len(imported_rows),
        "duplicates": duplicates,
        "ignored_files": ignored_files,
        "assets": imported_rows,
    }


def _enforce_third_party_demo_rights(
    conn: sqlite3.Connection,
    project_root: Path,
    project_id: str,
) -> None:
    """Keep third-party demo assets unconfirmed in both the manifest and SQLite."""
    demo = read_demo_metadata(project_root)
    if demo is None or not demo.is_fixture or demo.kind != "third_party_internal_demo":
        return

    for relative_path in (
        "research/asset-manifest.json",
        "research/social-media-manifest.json",
    ):
        manifest_path = project_root / relative_path
        if not manifest_path.is_file():
            continue
        items = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            raise ValueError(f"Invalid asset manifest at {manifest_path}: expected a list")
        changed = False
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"Invalid asset manifest row at {manifest_path}")
            if item.get("rights_confirmed_for_preview") is not False:
                item["rights_confirmed_for_preview"] = False
                changed = True
        if changed:
            _atomic_write_text(manifest_path, json.dumps(items, indent=2))

    for record in list_assets_for_project(conn, project_id):
        if record.rights_confirmed_for_preview:
            upsert_asset_provenance(
                conn,
                AssetProvenanceRecord(
                    asset_id=record.asset_id,
                    project_id=record.project_id,
                    sha256_hash=record.sha256_hash,
                    phash=record.phash,
                    source_url=record.source_url,
                    local_path=record.local_path,
                    rights_confirmed_for_preview=False,
                ),
            )


class ProjectLockedError(RuntimeError):
    """Raised when re-running the pipeline on an approved project without new_draft=True."""


DRAFT_DIRS: tuple[str, ...] = (
    "assets",
    "research",
    "design",
    "website",
    "release-candidate",
    "qa",
)


def _reset_draft_directories(project_root: Path) -> None:
    """Reset draft workspace directories without touching delivery/, versions/, or archives."""
    for dirname in DRAFT_DIRS:
        target = project_root / dirname
        if target.is_dir():
            shutil.rmtree(target)


class PipelineRunResult(BaseModel):
    """Structured result returned by `run_pipeline`."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    business_name: str
    primary_url: str
    project_folder: Path
    status: str
    qa_passed: bool
    skipped_stages: list[str] = Field(default_factory=list)
    stopped_after_stage: str | None = None
    review_summary: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def run_pipeline(
    *,
    url: str,
    business_name: str,
    business_id: str | None = None,
    projects_dir: str | Path = "./projects",
    db_path: str | Path | None = None,
    allow_private: bool = False,
    resume: bool = False,
    pause_for_approval: bool = False,
    qa_runner: QARunnerFn | None = None,
    llm_adapter: Any = None,
    new_draft: bool = False,
    acknowledge_gaps: bool = False,
    stop_after: str | None = None,
) -> PipelineRunResult:
    """Execute the EditVista lifecycle with SQLite idempotency, resumption, and approval gate."""
    if stop_after is not None and stop_after not in PIPELINE_STAGE_NAMES:
        raise ValueError(
            f"Unknown stage {stop_after!r}; choose one of: {', '.join(PIPELINE_STAGE_NAMES)}"
        )
    projects_root = Path(projects_dir).resolve()
    project_id = slugify(business_id or business_name)
    project_root = projects_root / project_id
    read_demo_metadata(project_root)
    projects_root.mkdir(parents=True, exist_ok=True)
    resolved_db_path = Path(db_path).resolve() if db_path else (projects_root / "editvista.sqlite3")

    warnings: list[str] = []
    errors: list[str] = []
    skipped_stages: list[str] = []
    active_qa_runner = qa_runner or run_qa_engine

    conn = connect_db(resolved_db_path)
    init_db(conn)

    # Clean up interrupted stages left by an earlier crashed run
    mark_running_jobs_interrupted(conn, project_id=project_id)

    # Protection gate: If approved, refuse rerun unless new_draft=True
    existing_project = get_project(conn, project_id)
    is_approved = (
        (project_root / "delivery" / "release-manifest.json").is_file()
        or (existing_project is not None and existing_project.status == "approved")
    )
    if is_approved and not new_draft:
        conn.close()
        raise ProjectLockedError(
            f"Project '{project_id}' is approved and locked. "
            "Re-running the pipeline on an approved project is prohibited to protect "
            "immutable delivery releases. Pass new_draft=True (CLI: --new-draft) to build "
            "a new draft without touching delivery/ or versions/."
        )

    try:
        freeze_demo_metadata(project_root)
        _enforce_third_party_demo_rights(conn, project_root, project_id)

        # Step 0: Draft reset if not resuming (never touching delivery/, versions/, or archives)
        if not resume and project_root.exists():
            _reset_draft_directories(project_root)

        # Step 1: Initialize database schema and project record
        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                status="running",
            ),
        )

        def stop_if_requested(
            stage_name: str,
            *,
            qa_passed: bool = False,
        ) -> PipelineRunResult | None:
            if stop_after != stage_name:
                return None
            upsert_project(
                conn,
                ProjectRecord(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    status="stopped_after_stage",
                ),
            )
            return PipelineRunResult(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                project_folder=project_root,
                status="stopped_after_stage",
                qa_passed=qa_passed,
                skipped_stages=skipped_stages,
                stopped_after_stage=stage_name,
                warnings=warnings,
                errors=errors,
            )

        # Step 2: Collect website pages & media candidates (stage: collect)
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        collect_idemp = build_stage_idempotency_key(project_id, "collect", url_hash)
        collect_record = conn.execute(
            "SELECT status FROM job_runs WHERE project_id = ? AND module_name = ?",
            (project_id, "collect"),
        ).fetchone()
        legacy_collected_project = (
            collect_record is None
            and (project_root / "research" / "pages.json").is_file()
            and not (project_root / "input.json").is_file()
        )
        if resume and (
            (
                _is_stage_completed_and_valid(
                    conn,
                    project_id=project_id,
                    stage_name="collect",
                    idempotency_key=collect_idemp,
                    project_root=project_root,
                    required_output_rel_paths=[
                        "research/pages.json",
                        "research/social-collection-report.json",
                    ],
                )
                and _social_input_matches_last_collection(project_root)
            )
            or legacy_collected_project
        ):
            skipped_stages.append("collect")
            provenance_records = list_assets_for_project(conn, project_id)
        else:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="collect",
                idempotency_key=collect_idemp,
                status="running",
            )
            try:
                staging_collect_dir = projects_root / f".collect_staging_{project_id}"
                if staging_collect_dir.exists():
                    shutil.rmtree(staging_collect_dir)
                collect_summary = collect(
                    url,
                    staging_collect_dir,
                    max_pages=10,
                    max_media=40,
                    allow_private=allow_private,
                )
                crawled_folder = Path(str(collect_summary["project_folder"]))
                project_root.mkdir(parents=True, exist_ok=True)
                for item_path in crawled_folder.iterdir():
                    if item_path.name in ("delivery", "versions", "input.json"):
                        continue
                    dest = project_root / item_path.name
                    if item_path.is_dir():
                        shutil.copytree(item_path, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item_path, dest)
                _enforce_third_party_demo_rights(conn, project_root, project_id)
                _collect_configured_social(project_root, warnings)
                shutil.rmtree(staging_collect_dir, ignore_errors=True)
                _record_job_transition(
                    conn,
                    project_id=project_id,
                    module_name="collect",
                    idempotency_key=collect_idemp,
                    status="completed",
                )
            except Exception as exc:
                staging_collect_dir = projects_root / f".collect_staging_{project_id}"
                if staging_collect_dir.exists():
                    shutil.rmtree(staging_collect_dir, ignore_errors=True)
                _record_job_transition(
                    conn,
                    project_id=project_id,
                    module_name="collect",
                    idempotency_key=collect_idemp,
                    status="failed",
                    error_message=f"Collection failed: {exc}",
                )
                upsert_project(
                    conn,
                    ProjectRecord(
                        project_id=project_id,
                        business_name=business_name,
                        primary_url=url,
                        status="failed",
                    ),
                )
                # Distinction: Individual broken media downloads are handled gracefully in
                # download_one() with status='failed' without raising, while total page-collection
                # failure raises and stops the pipeline immediately.
                raise RuntimeError(
                    f"Collection failed for '{url}': {exc}. "
                    "Cannot build website without verified source content."
                ) from exc
            provenance_records = _stage_downloaded_assets_for_curator(
                conn, project_root, project_id
            )

        stopped = stop_if_requested("collect")
        if stopped is not None:
            return stopped

        # Step 3: Extract and verify business facts (stage: extract)
        extract_inputs_hash = compute_input_manifest_hash(
            project_root,
            ["research/pages.json"],
        )
        extract_hash = hashlib.sha256(
            f"{extract_inputs_hash}:{_normalized_business_name(business_name)}".encode()
        ).hexdigest()[:16]
        extract_idemp = build_stage_idempotency_key(project_id, "extract", extract_hash)
        if resume and _is_stage_completed_and_valid(
            conn,
            project_id=project_id,
            stage_name="extract",
            idempotency_key=extract_idemp,
            project_root=project_root,
            required_output_rel_paths=[
                "research/business-facts.json",
                "research/business-name-comparison.json",
            ],
        ):
            skipped_stages.append("extract")
        else:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="extract",
                idempotency_key=extract_idemp,
                status="running",
            )
            facts_report = extract_business_facts(
                project_root, extract_classification=True
            )
            name_comparison = _business_name_comparison(
                project_root,
                business_name,
                facts_report,
            )
            if name_comparison["status"] != "verified":
                warnings.append(
                    "Operator business name needs review; see "
                    "research/business-name-comparison.json for the source diff."
                )
            for fact in facts_report.facts:
                upsert_business_fact(
                    conn,
                    BusinessFactRecord(
                        fact_id=fact.fact_id,
                        project_id=project_id,
                        category=fact.category,
                        claim_key=fact.claim_key,
                        claim_value=fact.claim_value,
                        source_url=fact.source_url,
                        source_text_snippet=fact.source_text_snippet,
                        is_verified=fact.is_verified,
                    ),
                )
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="extract",
                idempotency_key=extract_idemp,
                status="completed",
            )

        stopped = stop_if_requested("extract")
        if stopped is not None:
            return stopped

        # Zero-pages check: if 0 pages were crawled, stage failed immediately before model spend
        pages_file = project_root / "research" / "pages.json"
        pages_count = 0
        if pages_file.is_file():
            try:
                pages_data = json.loads(pages_file.read_text(encoding="utf-8"))
                if isinstance(pages_data, list):
                    pages_count = len(pages_data)
            except Exception:
                pass
        if pages_count == 0:
            run_sum_file = project_root / "research" / "run-summary.json"
            if run_sum_file.is_file():
                try:
                    sum_data = json.loads(run_sum_file.read_text(encoding="utf-8"))
                    pages_count = int(sum_data.get("pages_crawled", 0))
                except Exception:
                    pass

        if pages_count == 0:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="collect",
                idempotency_key=collect_idemp,
                status="failed",
                error_message="Collection crawled 0 pages",
            )
            upsert_project(
                conn,
                ProjectRecord(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    status="failed",
                ),
            )
            raise RuntimeError(
                f"Collection crawled 0 pages for '{url}'. "
                "Cannot build website without source pages."
            )

        # Enforce Minimum Viable Facts (MVF) gate BEFORE curate/art_director/generate
        # to prevent model spend on projects with insufficient verified facts.
        mvf_report = enforce_mvf_gate(project_root, acknowledge_gaps=acknowledge_gaps)
        if not mvf_report.passed and not acknowledge_gaps:
            upsert_project(
                conn,
                ProjectRecord(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    status="needs_review",
                ),
            )
            return PipelineRunResult(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                project_folder=project_root,
                status="needs_review",
                qa_passed=False,
                skipped_stages=skipped_stages,
                warnings=warnings,
                errors=[
                    f"MVF gate not met: missing {', '.join(mvf_report.missing_items)}. "
                    f"{mvf_report.exact_human_action_needed}"
                ],
            )

        # Step 4: Curate images (stage: curate)
        curate_hash = compute_input_manifest_hash(project_root, ["research/asset-manifest.json"])
        curate_idemp = build_stage_idempotency_key(project_id, "curate", curate_hash)
        if resume and _is_stage_completed_and_valid(
            conn,
            project_id=project_id,
            stage_name="curate",
            idempotency_key=curate_idemp,
            project_root=project_root,
            required_output_rel_paths=["design/image-selection.json"],
        ):
            skipped_stages.append("curate")
        else:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="curate",
                idempotency_key=curate_idemp,
                status="running",
            )
            curate_res = curate_project_images(project_root)
            warnings.extend(curate_res.warnings)
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="curate",
                idempotency_key=curate_idemp,
                status="completed",
            )

        stopped = stop_if_requested("curate")
        if stopped is not None:
            return stopped

        # Step 5: Art Direction & Brand Analysis (stage: art_director)
        art_hash = compute_input_manifest_hash(
            project_root,
            ["research/business-facts.json", "design/image-selection.json"],
        )
        art_idemp = build_stage_idempotency_key(project_id, "art_director", art_hash)
        if resume and _is_stage_completed_and_valid(
            conn,
            project_id=project_id,
            stage_name="art_director",
            idempotency_key=art_idemp,
            project_root=project_root,
            required_output_rel_paths=["design/brand.json", "design/creative-brief.json"],
        ):
            skipped_stages.append("art_director")
        else:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="art_director",
                idempotency_key=art_idemp,
                status="running",
            )
            generate_art_direction(
                project_root,
                provenance_records=provenance_records,
                llm_adapter=llm_adapter,
            )
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="art_director",
                idempotency_key=art_idemp,
                status="completed",
            )

        stopped = stop_if_requested("art_director")
        if stopped is not None:
            return stopped

        # Step 6: Generate semantic website code (stage: generate)
        gen_hash = compute_input_manifest_hash(
            project_root,
            [
                "research/business-facts.json",
                "design/image-selection.json",
                "design/brand.json",
                "design/creative-brief.json",
            ],
        )
        gen_idemp = build_stage_idempotency_key(project_id, "generate", gen_hash)
        if resume and _is_stage_completed_and_valid(
            conn,
            project_id=project_id,
            stage_name="generate",
            idempotency_key=gen_idemp,
            project_root=project_root,
            required_output_rel_paths=[
                "website/index.html",
                "website/css/styles.css",
                "website/js/main.js",
            ],
        ):
            skipped_stages.append("generate")
        else:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="generate",
                idempotency_key=gen_idemp,
                status="running",
            )
            generate_website(project_root)
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="generate",
                idempotency_key=gen_idemp,
                status="completed",
            )

        stopped = stop_if_requested("generate")
        if stopped is not None:
            return stopped

        # Stage: claims_lint (lint HTML claims against verified facts and UI allowlist)
        lint_hash = compute_input_manifest_hash(
            project_root,
            [
                "website/index.html",
                "research/business-facts.json",
            ],
        )
        lint_idemp = build_stage_idempotency_key(project_id, "claims_lint", lint_hash)
        _record_job_transition(
            conn,
            project_id=project_id,
            module_name="claims_lint",
            idempotency_key=lint_idemp,
            status="running",
        )
        stage_release_candidate(project_root)
        claims_lint_report = run_claims_lint(project_root)
        if not claims_lint_report.passed:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="claims_lint",
                idempotency_key=lint_idemp,
                status="failed",
                error_message=(
                    f"Claims lint failed: {len(claims_lint_report.violations)} violation(s)"
                ),
            )
            upsert_project(
                conn,
                ProjectRecord(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    status="needs_review",
                ),
            )
            return PipelineRunResult(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                project_folder=project_root,
                status="needs_review",
                qa_passed=False,
                skipped_stages=skipped_stages,
                warnings=warnings,
                errors=[
                    f"Claims lint failed: {len(claims_lint_report.violations)} violation(s) found. "
                    "Unbacked claims detected in generated website."
                ],
            )
        _record_job_transition(
            conn,
            project_id=project_id,
            module_name="claims_lint",
            idempotency_key=lint_idemp,
            status="completed",
        )

        stopped = stop_if_requested("claims_lint")
        if stopped is not None:
            return stopped

        # Step 7: Execute 5-viewport Playwright QA + Visual Critic + Targeted Revision Loop
        qa_hash = compute_input_manifest_hash(
            project_root,
            ["website/index.html", "website/css/styles.css", "website/js/main.js"],
        )
        qa_idemp = build_stage_idempotency_key(project_id, "qa", qa_hash)
        critique_idemp = build_stage_idempotency_key(project_id, "critique", qa_hash)
        revise_idemp = build_stage_idempotency_key(project_id, "revise", qa_hash)

        def run_qa() -> QADefectReport:
            try:
                return active_qa_runner(project_root)
            except Exception as exc:
                _record_job_transition(
                    conn,
                    project_id=project_id,
                    module_name="qa",
                    idempotency_key=qa_idemp,
                    status="failed",
                    error_message=f"QA execution failed: {exc}",
                )
                raise

        _record_job_transition(
            conn,
            project_id=project_id,
            module_name="qa",
            idempotency_key=qa_idemp,
            status="running",
        )
        stage_release_candidate(project_root)
        qa_report = run_qa()
        _record_job_transition(
            conn,
            project_id=project_id,
            module_name="qa",
            idempotency_key=qa_idemp,
            status="completed" if qa_report.passed else "failed",
            error_message=(
                None
                if qa_report.passed
                else "QA report detected critical viewport defects"
            ),
        )
        stopped = stop_if_requested("qa", qa_passed=qa_report.passed)
        if stopped is not None:
            return stopped

        # Bounded Critique -> Revise -> QA loop (max 3 cycles)
        for _cycle in range(MAX_REVISION_ITERATIONS):
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="critique",
                idempotency_key=critique_idemp,
                status="running",
            )
            critique_report = run_visual_critic(project_root, llm_adapter=llm_adapter)
            if critique_report.status == "critic_unavailable":
                critique_error = (
                    "Visual critic unavailable; a successful critic run is required "
                    "before the project can proceed."
                )
                warnings.extend(critique_report.warnings)
                _record_job_transition(
                    conn,
                    project_id=project_id,
                    module_name="critique",
                    idempotency_key=critique_idemp,
                    status="failed",
                    error_message=critique_error,
                )
                if qa_report.passed:
                    _record_job_transition(
                        conn,
                        project_id=project_id,
                        module_name="qa",
                        idempotency_key=qa_idemp,
                        status="completed",
                    )
                else:
                    _record_job_transition(
                        conn,
                        project_id=project_id,
                        module_name="qa",
                        idempotency_key=qa_idemp,
                        status="failed",
                        error_message="QA report detected critical viewport defects",
                    )
                upsert_project(
                    conn,
                    ProjectRecord(
                        project_id=project_id,
                        business_name=business_name,
                        primary_url=url,
                        status="needs_review",
                    ),
                )
                return PipelineRunResult(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    project_folder=project_root,
                    status="needs_review",
                    qa_passed=qa_report.passed,
                    skipped_stages=skipped_stages,
                    warnings=warnings,
                    errors=[critique_error],
                )
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="critique",
                idempotency_key=critique_idemp,
                status="completed",
            )
            stopped = stop_if_requested("critique", qa_passed=qa_report.passed)
            if stopped is not None:
                return stopped

            if critique_report.status == "passed" or not critique_report.defects:
                _record_job_transition(
                    conn,
                    project_id=project_id,
                    module_name="revise",
                    idempotency_key=revise_idemp,
                    status="completed",
                )
                stopped = stop_if_requested("revise", qa_passed=qa_report.passed)
                if stopped is not None:
                    return stopped
                break

            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="revise",
                idempotency_key=revise_idemp,
                status="running",
            )
            apply_targeted_revisions(project_root)
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="revise",
                idempotency_key=revise_idemp,
                status="completed",
            )
            stopped = stop_if_requested("revise", qa_passed=qa_report.passed)
            if stopped is not None:
                return stopped
            stage_release_candidate(project_root)
            qa_report = run_qa()

        if not qa_report.passed:
            _record_job_transition(
                conn,
                project_id=project_id,
                module_name="qa",
                idempotency_key=qa_idemp,
                status="failed",
                error_message="QA report detected critical viewport defects",
            )
            upsert_project(
                conn,
                ProjectRecord(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    status="needs_review",
                ),
            )
            return PipelineRunResult(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                project_folder=project_root,
                status="needs_review",
                qa_passed=False,
                skipped_stages=skipped_stages,
                warnings=warnings,
                errors=["QA engine reported viewport failures"],
            )

        _record_job_transition(
            conn,
            project_id=project_id,
            module_name="qa",
            idempotency_key=qa_idemp,
            status="completed",
        )

        # Enforce Minimum Viable Facts (MVF) gate and sanitize contact CTAs
        mvf_report = enforce_mvf_gate(project_root, acknowledge_gaps=acknowledge_gaps)
        if not mvf_report.passed and not acknowledge_gaps:
            upsert_project(
                conn,
                ProjectRecord(
                    project_id=project_id,
                    business_name=business_name,
                    primary_url=url,
                    status="needs_review",
                ),
            )
            return PipelineRunResult(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                project_folder=project_root,
                status="needs_review",
                qa_passed=qa_report.passed,
                skipped_stages=skipped_stages,
                warnings=warnings,
                errors=[
                    f"MVF gate not met: missing {', '.join(mvf_report.missing_items)}. "
                    f"{mvf_report.exact_human_action_needed}"
                ],
            )

        # Stage candidate standalone and GHL code into release-candidate/ (never delivery/)
        cand_path, _ = stage_release_candidate(project_root)
        cand_sha = hashlib.sha256(cand_path.read_bytes()).hexdigest()
        qa_report_path = project_root / "qa" / "qa-report.json"
        if qa_report_path.is_file():
            qa_json = json.loads(qa_report_path.read_text(encoding="utf-8"))
            if not qa_json.get("candidate_sha256"):
                qa_json["candidate_sha256"] = cand_sha
                _atomic_write_text(qa_report_path, json.dumps(qa_json, indent=2))

        # Assemble human review summary package
        review_summary = generate_review_summary(project_id, projects_dir=projects_root)
        review_summary["mvf_report"] = mvf_report.model_dump(mode="json")
        review_summary["acknowledged_gaps"] = (
            mvf_report.missing_items if not mvf_report.passed else []
        )
        _atomic_write_text(
            project_root / "qa" / "review-summary.json",
            json.dumps(review_summary, indent=2),
        )

        export_idemp = build_stage_idempotency_key(project_id, "ghl_exporter", qa_hash)
        _record_job_transition(
            conn,
            project_id=project_id,
            module_name="ghl_exporter",
            idempotency_key=export_idemp,
            status="completed",
        )
        stopped = stop_if_requested("ghl_exporter", qa_passed=True)
        if stopped is not None:
            return stopped

        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                status="awaiting_approval",
            ),
        )
        return PipelineRunResult(
            project_id=project_id,
            business_name=business_name,
            primary_url=url,
            project_folder=project_root,
            status="awaiting_approval",
            qa_passed=True,
            skipped_stages=skipped_stages,
            review_summary=review_summary,
            warnings=warnings,
            errors=errors,
        )
    except Exception as exc:
        errors.append(str(exc))
        upsert_project(
            conn,
            ProjectRecord(
                project_id=project_id,
                business_name=business_name,
                primary_url=url,
                status="failed",
            ),
        )
        raise
    finally:
        conn.close()


def _handle_approve_subcommand(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline approve",
        description="Approve and freeze an immutable website release and GHL export.",
    )
    parser.add_argument("--project", default=None, help="Target project slug ID or path")
    parser.add_argument("--project-id", default=None, help="Target project slug ID")
    parser.add_argument("--version", default="v1", help="Release version identifier (e.g. v1)")
    parser.add_argument("--operator", required=True, help="Human reviewer/operator name")
    parser.add_argument("--notes", required=True, help="Human approval decision notes")
    parser.add_argument(
        "--reviewed-candidate-sha256",
        required=True,
        help="SHA-256 shown in the human review cockpit for the candidate being approved",
    )
    parser.add_argument("--reviewed-preview", action="store_true", required=True)
    parser.add_argument("--reviewed-facts", action="store_true", required=True)
    parser.add_argument("--reviewed-assets", action="store_true", required=True)
    parser.add_argument("--reviewed-qa", action="store_true", required=True)
    parser.add_argument("--confirmed-release", action="store_true", required=True)
    parser.add_argument(
        "--acknowledge-open-issues",
        action="store_true",
        default=False,
        help="Acknowledge visual critique defects and needs_review facts",
    )
    parser.add_argument("--projects-dir", default="./projects", help="Root projects directory")
    parser.add_argument("--db-path", default=None, help="Optional SQLite DB path")
    args = parser.parse_args(argv)

    target_project = args.project or args.project_id
    if not target_project:
        parser.error("one of the arguments --project or --project-id is required")

    approve_release(
        target_project,
        version=args.version,
        operator_name=args.operator,
        notes=args.notes,
        projects_dir=args.projects_dir,
        db_path=args.db_path,
        acknowledge_open_issues=bool(args.acknowledge_open_issues),
        review_attestation={
            "candidate_sha256": args.reviewed_candidate_sha256,
            "reviewed_preview": bool(args.reviewed_preview),
            "reviewed_facts": bool(args.reviewed_facts),
            "reviewed_assets": bool(args.reviewed_assets),
            "reviewed_qa": bool(args.reviewed_qa),
            "confirmed_release": bool(args.confirmed_release),
        },
    )

    # If qa/mvf-report.json has acknowledged gaps, record them in release-manifest.json
    project_root = _resolve_project_root(target_project, args.projects_dir)
    mvf_report_path = project_root / "qa" / "mvf-report.json"
    delivery_manifest_path = project_root / "delivery" / "release-manifest.json"
    version_manifest_path = project_root / "versions" / str(args.version) / "release-manifest.json"
    if mvf_report_path.is_file() and delivery_manifest_path.is_file():
        try:
            mvf_data = json.loads(mvf_report_path.read_text(encoding="utf-8"))
            if mvf_data.get("acknowledged") or not mvf_data.get("passed"):
                import stat

                m_data = json.loads(delivery_manifest_path.read_text(encoding="utf-8"))
                m_data["acknowledged_gaps"] = mvf_data.get("missing_items", [])
                for p in (delivery_manifest_path, version_manifest_path):
                    if p.is_file():
                        os.chmod(p, p.stat().st_mode | stat.S_IWRITE)
                        p.write_text(json.dumps(m_data, indent=2), encoding="utf-8")
                        os.chmod(p, p.stat().st_mode & ~stat.S_IWRITE)
        except Exception:
            pass

    return 0


def _handle_confirm_rights_subcommand(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline confirm-rights",
        description="Confirm asset rights and record audit trail in rights-audit.jsonl.",
    )
    parser.add_argument("--project-id", required=True, help="Target project slug ID")
    parser.add_argument("--asset", default=None, help="Target asset sha256 or filename")
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Confirm rights for all assets",
    )
    parser.add_argument(
        "--basis",
        required=True,
        choices=["client_provided", "owner_permission", "licensed"],
        help="Legal basis for asset rights",
    )
    parser.add_argument("--operator", required=True, help="Human operator / reviewer name")
    parser.add_argument("--evidence", required=True, help="Free text evidence / documentation")
    parser.add_argument("--projects-dir", default="./projects", help="Root projects directory")
    parser.add_argument("--db-path", default=None, help="Optional SQLite DB path")
    args = parser.parse_args(argv)

    from app.rights import confirm_asset_rights

    confirm_asset_rights(
        args.project_id,
        asset=args.asset,
        all_assets=bool(args.all),
        basis=args.basis,
        operator=args.operator,
        evidence=args.evidence,
        projects_dir=args.projects_dir,
        db_path=args.db_path,
    )
    return 0


def _handle_import_client_assets_subcommand(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline import-client-assets",
        description="Import client-provided image originals with provenance for local review.",
    )
    parser.add_argument("--project-id", required=True, help="Target project slug ID")
    parser.add_argument("--folder", required=True, help="Folder containing client image files")
    parser.add_argument(
        "--supplied-by",
        default="not_specified",
        help="Client or operator who supplied the files (default: not_specified)",
    )
    parser.add_argument("--projects-dir", default="./projects", help="Root projects directory")
    parser.add_argument("--db-path", default=None, help="Optional SQLite database path")
    args = parser.parse_args(argv)

    projects_root = Path(args.projects_dir).resolve()
    project_root = (projects_root / args.project_id).resolve()
    if not project_root.is_relative_to(projects_root):
        parser.error("--project-id must identify a project under --projects-dir")
    if not project_root.is_dir():
        parser.error(f"Project folder does not exist: {project_root}")

    resolved_db_path = (
        Path(args.db_path).resolve()
        if args.db_path
        else projects_root / "editvista.sqlite3"
    )
    conn = connect_db(resolved_db_path)
    try:
        init_db(conn)
        if get_project(conn, args.project_id) is None:
            parser.error(
                f"Project '{args.project_id}' is not registered in {resolved_db_path}"
            )
        result = import_client_assets(
            project_root,
            args.folder,
            supplied_by=args.supplied_by,
        )
        provenance_records = _stage_downloaded_assets_for_curator(
            conn,
            project_root,
            args.project_id,
        )
    finally:
        conn.close()
    result["optimized_assets"] = len(provenance_records)
    result["contact_sheet"] = make_sheet(project_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _handle_prune_subcommand(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline prune",
        description="Run approval-gated cleanup for unreferenced staging and temporary files.",
    )
    parser.add_argument("--project-id", required=True, help="Target project slug ID")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute deletion (defaults to dry-run if omitted)",
    )
    parser.add_argument("--projects-dir", default="./projects", help="Root projects directory")
    args = parser.parse_args(argv)

    project_root = Path(args.projects_dir).resolve() / args.project_id
    safe_prune_project(project_root, dry_run=not bool(args.execute))
    return 0


def _handle_verify_subcommand(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline verify",
        description="Verify delivery/ integrity against release-manifest.json.",
    )
    parser.add_argument("--project", default=None, help="Target project slug ID or path")
    parser.add_argument("--project-id", default=None, help="Target project slug ID")
    parser.add_argument("--projects-dir", default="./projects", help="Root projects directory")
    args = parser.parse_args(argv)

    target_project = args.project or args.project_id
    if not target_project:
        parser.error("one of the arguments --project or --project-id is required")

    project_root = _resolve_project_root(target_project, args.projects_dir)
    try:
        verify_release(project_root)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Verification failed: {exc}\n")
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EditVista Pet Grooming Website Builder Engine Master Orchestrator Pipeline."
    )
    parser.add_argument("--url", required=True, help="Target business website URL")
    parser.add_argument("--name", required=True, help="Business name")
    parser.add_argument("--business-id", default=None, help="Optional explicit business slug ID")
    parser.add_argument(
        "--projects-dir",
        default="./projects",
        help="Root directory for project workspaces",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional explicit SQLite database path",
    )
    parser.add_argument(
        "--allow-private",
        action="store_true",
        help="Allow private/loopback addresses for local testing fixtures",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the last pending or failed stage using SQLite idempotency keys",
    )
    parser.add_argument(
        "--pause-for-approval",
        dest="pause_for_approval",
        action="store_true",
        default=True,
        help="Pause at the human approval gate before exporting GHL deliverables (default: true)",
    )
    parser.add_argument(
        "--no-pause-for-approval",
        dest="pause_for_approval",
        action="store_false",
        help="Do not pause for approval",
    )
    parser.add_argument(
        "--new-draft",
        action="store_true",
        help="Allow building a new draft on an approved project without modifying releases",
    )
    parser.add_argument(
        "--acknowledge-gaps",
        action="store_true",
        default=False,
        help="Acknowledge missing Minimum Viable Facts (MVF) and continue to awaiting_approval",
    )
    parser.add_argument(
        "--stop-after",
        choices=PIPELINE_STAGE_NAMES,
        default=None,
        help="Stop after completing one pipeline stage",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    qa_runner: QARunnerFn | None = None,
) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] == "approve":
        return _handle_approve_subcommand(args_list[1:])
    if args_list and args_list[0] == "confirm-rights":
        return _handle_confirm_rights_subcommand(args_list[1:])
    if args_list and args_list[0] == "import-client-assets":
        return _handle_import_client_assets_subcommand(args_list[1:])
    if args_list and args_list[0] == "prune":
        return _handle_prune_subcommand(args_list[1:])
    if args_list and args_list[0] == "verify":
        return _handle_verify_subcommand(args_list[1:])
    if args_list and args_list[0] == "cockpit":
        from app.cockpit import main as cockpit_main

        return cockpit_main(args_list[1:])
    if args_list and args_list[0] == "pilot":
        from app.pilot import main as pilot_main

        return pilot_main(args_list[1:])

    parser = build_arg_parser()
    args = parser.parse_args(args_list)

    result = run_pipeline(
        url=args.url,
        business_name=args.name,
        business_id=args.business_id,
        projects_dir=args.projects_dir,
        db_path=args.db_path,
        allow_private=bool(args.allow_private),
        resume=bool(args.resume),
        pause_for_approval=bool(args.pause_for_approval),
        qa_runner=qa_runner,
        new_draft=bool(args.new_draft),
        acknowledge_gaps=bool(args.acknowledge_gaps),
        stop_after=args.stop_after,
    )
    if args.stop_after is not None:
        print(result.model_dump_json())
    return (
        0
        if result.status in ("completed", "awaiting_approval", "stopped_after_stage")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
