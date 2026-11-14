"""Read-only Milestone 1 provenance auditor for a collected project directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SOCIAL_HOSTS = {
    "facebook.com",
    "instagram.com",
}
SCHEMA_ALIASES = {
    "asset source_url": "url",
    "asset originating_page": "source_page",
    "claim source_text": "source_text_snippet",
    "claim verified": "is_verified",
}


def _add_violation(
    violations: list[dict[str, Any]],
    *,
    artifact: str,
    code: str,
    message: str,
    index: int | None = None,
) -> None:
    violation: dict[str, Any] = {
        "artifact": artifact,
        "code": code,
        "message": message,
    }
    if index is not None:
        violation["index"] = index
    violations.append(violation)


def _load_json(
    path: Path,
    *,
    artifact: str,
    violations: list[dict[str, Any]],
) -> Any | None:
    if not path.is_file():
        _add_violation(
            violations,
            artifact=artifact,
            code=f"{artifact}_missing",
            message=f"Required file is missing: {path.name}",
        )
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _add_violation(
            violations,
            artifact=artifact,
            code=f"{artifact}_unreadable",
            message=f"Could not read {path.name}: {exc}",
        )
        return None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _http_url(value: object) -> bool:
    if not _nonempty(value):
        return False
    parsed = urlparse(str(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _asset_url(row: dict[str, Any], canonical: str, alias: str) -> object:
    value = row.get(canonical)
    return value if _nonempty(value) else row.get(alias)


def _audit_asset(
    project_root: Path,
    row: object,
    index: int,
    violations: list[dict[str, Any]],
) -> None:
    artifact = "asset-manifest.json"
    if not isinstance(row, dict):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="asset_row_invalid",
            message="Asset entry must be a JSON object.",
        )
        return

    client_supplied = row.get("source") == "client_supplied"
    if row.get("status") == "client_supplied" and not client_supplied:
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="client_source_missing",
            message="A client-supplied asset must have source=client_supplied.",
        )
    if client_supplied:
        if not _nonempty(row.get("original_filename")):
            _add_violation(
                violations,
                artifact=artifact,
                index=index,
                code="client_original_filename_missing",
                message="A client-supplied asset needs its original filename.",
            )
        if not _nonempty(row.get("supplied_by")):
            _add_violation(
                violations,
                artifact=artifact,
                index=index,
                code="client_supplied_by_missing",
                message="A client-supplied asset needs a supplied_by value.",
            )

    source_url = _asset_url(row, "source_url", "url")
    if not _asset_source_valid(row, source_url):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="source_url_missing",
            message=(
                "Asset needs an HTTP(S) source_url or a matching client-supplied source URI "
                "(or collector url alias)."
            ),
        )

    originating_page = _asset_url(row, "originating_page", "source_page")
    if not _asset_source_valid(row, originating_page):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="originating_page_missing",
            message=(
                "Asset needs an HTTP(S) originating_page or a matching client-supplied source URI "
                "(or collector source_page alias)."
            ),
        )

    expected_sha = row.get("sha256")
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="sha256_missing_or_invalid",
            message="Asset needs a 64-character hexadecimal sha256.",
        )

    relative_file = row.get("file")
    resolved_file: Path | None = None
    if not _nonempty(relative_file):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="asset_file_missing",
            message="Asset needs a non-empty project-relative file path.",
        )
    else:
        try:
            resolved_file = (project_root / str(relative_file)).resolve()
            resolved_file.relative_to(project_root)
        except (OSError, RuntimeError, ValueError):
            _add_violation(
                violations,
                artifact=artifact,
                index=index,
                code="asset_path_outside_project",
                message="Asset file path is invalid or resolves outside the project directory.",
            )
            resolved_file = None

    if resolved_file is not None:
        if not resolved_file.is_file():
            _add_violation(
                violations,
                artifact=artifact,
                index=index,
                code="asset_file_not_found",
                message=f"Asset file does not exist: {relative_file}",
            )
        elif isinstance(expected_sha, str) and SHA256_RE.fullmatch(expected_sha):
            actual_sha = hashlib.sha256(resolved_file.read_bytes()).hexdigest()
            if actual_sha.lower() != expected_sha.lower():
                _add_violation(
                    violations,
                    artifact=artifact,
                    index=index,
                    code="sha256_mismatch",
                    message=(
                        f"Manifest SHA-256 does not match file bytes for {relative_file}."
                    ),
                )

    if not _nonempty(row.get("captured_at")):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="captured_at_missing",
            message="Asset needs a non-empty captured_at timestamp.",
        )

    rights = row.get("rights_confirmed_for_preview")
    if "rights_confirmed_for_preview" not in row:
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="rights_flag_missing",
            message="rights_confirmed_for_preview must be explicitly present (default false).",
        )
    elif not isinstance(rights, bool):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="rights_flag_invalid",
            message="rights_confirmed_for_preview must be a boolean.",
        )

    dimensions = row.get("dimensions")
    valid_dimensions = (
        isinstance(dimensions, list)
        and len(dimensions) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in dimensions)
    )
    if not valid_dimensions:
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="dimensions_missing",
            message="Asset needs positive integer width and height in dimensions.",
        )

    source_host = urlparse(str(source_url)).hostname or "" if _nonempty(source_url) else ""
    page_host = (
        urlparse(str(originating_page)).hostname or ""
        if _nonempty(originating_page)
        else ""
    )
    platform = str(row.get("platform", "")).strip().lower()
    file_path = str(relative_file).replace("\\", "/").lower() if _nonempty(relative_file) else ""
    is_social = (
        source_host.lower().removeprefix("www.") in SOCIAL_HOSTS
        or page_host.lower().removeprefix("www.") in SOCIAL_HOSTS
        or platform in {"instagram", "facebook"}
        or "/assets/instagram/" in f"/{file_path}"
        or "/assets/facebook/" in f"/{file_path}"
    )
    if is_social and not (
        _nonempty(row.get("post_date"))
        or row.get("date_unverified") is True
        or row.get("post_date_unverified") is True
    ):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="social_date_unverified_missing",
            message="Social asset needs a post_date or an explicit true date_unverified flag.",
        )


def _is_client_source_reference(row: dict[str, Any], value: object) -> bool:
    if row.get("source") != "client_supplied" or not _nonempty(value):
        return False
    expected_sha = row.get("sha256")
    original_filename = row.get("original_filename")
    parsed = urlparse(str(value))
    path_parts = parsed.path.lstrip("/").split("/", 1)
    return (
        parsed.scheme == "client-supplied"
        and parsed.netloc == "sha256"
        and isinstance(expected_sha, str)
        and SHA256_RE.fullmatch(expected_sha) is not None
        and len(path_parts) == 2
        and path_parts[0].lower() == expected_sha.lower()
        and _nonempty(original_filename)
        and unquote(path_parts[1]) == original_filename
    )


def _asset_source_valid(row: dict[str, Any], value: object) -> bool:
    if row.get("source") == "client_supplied":
        return _is_client_source_reference(row, value)
    return _http_url(value)


def _claim_value(row: dict[str, Any], canonical: str, alias: str) -> object:
    value = row.get(canonical)
    return value if _nonempty(value) else row.get(alias)


def _audit_claim(
    row: object,
    index: int,
    violations: list[dict[str, Any]],
) -> None:
    artifact = "business-facts.json"
    if not isinstance(row, dict):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="claim_row_invalid",
            message="Claim entry must be a JSON object.",
        )
        return

    source_url = row.get("source_url")
    source_text = _claim_value(row, "source_text", "source_text_snippet")
    verified = row.get("verified", row.get("is_verified"))
    has_source_url = _http_url(source_url)
    has_source_text = _nonempty(source_text)

    if not has_source_url:
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="claim_source_url_missing",
            message="Claim needs a non-empty HTTP(S) source_url.",
        )
    if not has_source_text:
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="claim_source_text_missing",
            message="Claim needs source_text (or collector source_text_snippet alias).",
        )
    if not isinstance(verified, bool):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="claim_verified_missing_or_invalid",
            message="Claim needs an explicit boolean verified field.",
        )
    if not _nonempty(row.get("last_checked_at")):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="claim_last_checked_at_missing",
            message="Claim needs a non-empty last_checked_at timestamp.",
        )

    status = row.get("status")
    unsupported = not has_source_url or not has_source_text
    if unsupported and (status != "needs_review" or verified is True):
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="unsupported_claim_not_needs_review",
            message="Unsupported claims must have status needs_review and must not be verified.",
        )
    elif status == "needs_review" and verified is True:
        _add_violation(
            violations,
            artifact=artifact,
            index=index,
            code="needs_review_claim_marked_verified",
            message="A needs_review claim must not be marked verified.",
        )


def audit_project(project_dir: str | Path) -> dict[str, Any]:
    """Audit project provenance without modifying any project files."""
    project_root = Path(project_dir).resolve()
    violations: list[dict[str, Any]] = []
    uncollected: list[dict[str, Any]] = []
    assets_checked = 0
    if not project_root.is_dir():
        _add_violation(
            violations,
            artifact="project",
            code="project_dir_missing",
            message=f"Project directory does not exist: {project_root}",
        )
        assets: list[Any] = []
        claims: list[Any] = []
    else:
        assets_path = project_root / "research" / "asset-manifest.json"
        facts_path = project_root / "research" / "business-facts.json"
        assets_data = _load_json(
            assets_path,
            artifact="asset_manifest",
            violations=violations,
        )
        facts_data = _load_json(
            facts_path,
            artifact="business_facts",
            violations=violations,
        )
        if assets_data is None:
            assets = []
        elif isinstance(assets_data, list):
            assets = assets_data
        else:
            _add_violation(
                violations,
                artifact="asset-manifest.json",
                code="asset_manifest_invalid",
                message="Asset manifest root must be a JSON list.",
            )
            assets = []

        if facts_data is None:
            claims = []
        elif isinstance(facts_data, dict) and isinstance(facts_data.get("facts"), list):
            claims = facts_data["facts"]
        else:
            _add_violation(
                violations,
                artifact="business-facts.json",
                code="business_facts_invalid",
                message="Business facts must be a JSON object with a facts list.",
            )
            claims = []

        for index, asset in enumerate(assets):
            if isinstance(asset, dict) and asset.get("status") in {
                "blocked_by_robots",
                "failed",
            }:
                status = str(asset["status"])
                reason = asset.get("error")
                if not _nonempty(reason):
                    reason = (
                        "robots.txt disallowed the URL or access could not be verified"
                        if status == "blocked_by_robots"
                        else "Media download failed without an error message"
                    )
                uncollected.append(
                    {
                        "index": index,
                        "status": status,
                        "source_url": _asset_url(asset, "source_url", "url"),
                        "originating_page": _asset_url(
                            asset,
                            "originating_page",
                            "source_page",
                        ),
                        "reason": reason,
                    }
                )
                continue
            assets_checked += 1
            _audit_asset(project_root, asset, index, violations)
        for index, claim in enumerate(claims):
            _audit_claim(claim, index, violations)

    return {
        "project_id": project_root.name,
        "project_dir": str(project_root),
        "schema_aliases": SCHEMA_ALIASES,
        "summary": {
            "assets_checked": assets_checked,
            "assets_uncollected": len(uncollected),
            "claims_checked": len(claims),
            "violations": len(violations),
        },
        "uncollected": uncollected,
        "violations": violations,
    }


def _human_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    passed = summary["violations"] == 0
    lines = [
        f"{'PASS' if passed else 'FAIL'} Milestone 1 provenance audit: {report['project_id']}",
        f"Assets checked: {summary['assets_checked']}",
        f"Uncollected assets: {summary['assets_uncollected']}",
        f"Claims checked: {summary['claims_checked']}",
        f"Violations: {summary['violations']}",
    ]
    for violation in report["violations"]:
        location = violation["artifact"]
        if "index" in violation:
            location += f"[{violation['index']}]"
        lines.append(f"- {location} {violation['code']}: {violation['message']}")
    for row in report["uncollected"]:
        lines.append(
            f"- asset-manifest.json[{row['index']}] {row['status']}: {row['reason']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir",
        required=True,
        help="Path to projects/<business-id>/",
    )
    args = parser.parse_args(argv)
    report = audit_project(args.project_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(_human_summary(report), file=sys.stderr)
    return 0 if not report["violations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
