"""Bounded, opt-in gallery-dl subprocess adapter for public Instagram/Facebook sources.

No logins, cookies, proxy rotation, or access-bypass features. gallery-dl runs
ONLY as an external subprocess and is never imported. When a platform requires
authentication, rate-limits, or breaks, return a review_needed result with a
classified reason and never raise to the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
ALLOWED = IMAGE_EXTENSIONS | {".mp4", ".webm", ".mov"}
SOCIAL_HOSTS = {
    "instagram": {"instagram.com", "www.instagram.com"},
    "facebook": {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "web.facebook.com",
        "fb.watch",
    },
}
MAX_ITEM_BYTES = 25 * 1024 * 1024
MAX_STAGING_BYTES = 150 * 1024 * 1024
MAX_PROJECT_BYTES = 300 * 1024 * 1024

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(cookie|sessionid|csrftoken|authorization|bearer|password|token)\s*[:=]\s*\S+"),
)

_LOGIN_PATTERNS = (
    "login required",
    "login_required",
    "checkpoint_required",
    "401 unauthorized",
    "403 forbidden",
    "authentication required",
    "requires login",
    "private account",
    "not logged in",
    "sign in to continue",
    "cookies",
)

_RATE_LIMIT_PATTERNS = (
    "429",
    "too many requests",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "please wait a few minutes",
    "throttl",
    "try again later",
)

_EXTRACTOR_BROKEN_PATTERNS = (
    "extractorerror",
    "unable to extract",
    "keyerror",
    "attributeerror",
    "typeerror",
    "jsondecodeerror",
    "parse error",
    "unexpected response",
    "traceback (most recent call last)",
    "unsupported url",
)


def cutoff_year(today: date | None = None) -> date:
    day = today or date.today()
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # Feb 29
        return day.replace(year=day.year - 1, day=28)


def check_social_url(platform: str, url: str) -> str:
    if platform not in SOCIAL_HOSTS:
        raise ValueError("Choose instagram or facebook")
    u = urlparse(url)
    if (
        u.scheme != "https"
        or u.username
        or u.password
        or (u.hostname or "").lower() not in SOCIAL_HOSTS[platform]
    ):
        raise ValueError(
            "Only public HTTPS Instagram/Facebook URLs on the expected platform are accepted"
        )
    return url


def folder_size(root: str | Path) -> int:
    root_path = Path(root)
    if not root_path.exists():
        return 0
    return sum(
        p.stat().st_size for p in root_path.rglob("*") if p.is_file() and not p.is_symlink()
    )


def file_safe(p: Path) -> bool:
    return (
        p.is_file()
        and not p.is_symlink()
        and p.suffix.lower() in ALLOWED
        and p.stat().st_size <= MAX_ITEM_BYTES
    )


def scrub_sensitive_text(text: str) -> str:
    """Remove any accidental cookie, session, or token values before logging or returning."""
    cleaned = text
    for pat in _SENSITIVE_PATTERNS:
        cleaned = pat.sub(r"\1=[REDACTED]", cleaned)
    return cleaned


def classify_social_failure(exit_code: int, output: str) -> str:
    """Classify gallery-dl failure output into a bounded failure reason."""
    lowered = (output or "").lower()
    if any(token in lowered for token in _LOGIN_PATTERNS):
        return "login_required"
    if any(token in lowered for token in _RATE_LIMIT_PATTERNS):
        return "rate_limited"
    if any(token in lowered for token in _EXTRACTOR_BROKEN_PATTERNS):
        return "extractor_broken"
    _ = exit_code
    return "unknown"


def _parse_real_timestamp(raw_val: Any) -> str | None:
    """Parse a real post timestamp from gallery-dl metadata into UTC ISO-8601 format."""
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, int | float):
        if raw_val <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(raw_val), tz=UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw_val, str):
        text = raw_val.strip()
        if not text:
            return None
        if text.isdigit():
            val = int(text)
            if val > 0:
                try:
                    return datetime.fromtimestamp(float(val), tz=UTC).isoformat()
                except (OSError, OverflowError, ValueError):
                    return None
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            else:
                dt = dt.astimezone(UTC)
            return dt.isoformat()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=UTC)
                return dt.isoformat()
            except ValueError:
                continue
    return None


def derive_post_metadata(meta: dict[str, Any], *, fallback_url: str) -> dict[str, Any]:
    """Derive post_date, post_date_verified, original_post_url, and author from metadata."""
    post_date: str | None = None
    for key in ("date", "post_date", "timestamp", "created_at", "taken_at"):
        if key in meta:
            parsed = _parse_real_timestamp(meta.get(key))
            if parsed is not None:
                post_date = parsed
                break

    original_post_url: str | None = None
    for url_key in ("post_url", "original_post_url", "webpage_url"):
        candidate = meta.get(url_key)
        if isinstance(candidate, str) and candidate.startswith("https://"):
            original_post_url = candidate
            break
    if original_post_url is None and isinstance(meta.get("post_shortcode"), str):
        shortcode = meta["post_shortcode"].strip("/")
        if shortcode and "instagram.com" in fallback_url:
            original_post_url = f"https://www.instagram.com/p/{shortcode}/"

    author: str | None = None
    for author_key in ("author", "username", "owner", "fullname", "user"):
        candidate = meta.get(author_key)
        if isinstance(candidate, str) and candidate.strip():
            author = candidate.strip()
            break
        if isinstance(candidate, dict):
            nested = candidate.get("username") or candidate.get("name")
            if isinstance(nested, str) and nested.strip():
                author = nested.strip()
                break

    return {
        "post_date": post_date,
        "post_date_verified": post_date is not None,
        "original_post_url": original_post_url or fallback_url,
        "source_post_url": original_post_url,
        "author": author,
    }


def parse_dump_json_metadata(raw_output: str) -> list[dict[str, Any]]:
    """Extract per-item metadata dictionaries from gallery-dl --dump-json output."""
    text = (raw_output or "").strip()
    if not text:
        return []

    records: list[dict[str, Any]] = []
    shared_context: dict[str, Any] = {}

    def _collect_from_parsed(parsed: Any) -> None:
        if isinstance(parsed, dict):
            records.append(dict(parsed))
            return
        if isinstance(parsed, list):
            for entry in parsed:
                if isinstance(entry, dict):
                    records.append(dict(entry))
                elif isinstance(entry, list) and entry:
                    code = entry[0]
                    dicts = [item for item in entry[1:] if isinstance(item, dict)]
                    if code == 2 and dicts:
                        shared_context.update(dicts[-1])
                    elif dicts:
                        merged = {**shared_context, **dicts[-1]}
                        records.append(merged)

    try:
        _collect_from_parsed(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str or not (line_str.startswith("{") or line_str.startswith("[")):
                continue
            try:
                _collect_from_parsed(json.loads(line_str))
            except json.JSONDecodeError:
                continue

    return records


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(target: Path, content: str) -> None:
    _atomic_write_bytes(target, content.encode("utf-8"))


def strip_image_metadata(src: Path, dest: Path) -> bytes:
    """Create an optimized copy of an image with all EXIF/GPS metadata removed."""
    with Image.open(src) as im:
        fmt = (im.format or "PNG").upper()
        clean = Image.new(im.mode, im.size)
        clean.putdata(list(im.getdata()))
        if "transparency" in im.info and fmt in {"PNG", "GIF", "WEBP"}:
            clean.info["transparency"] = im.info["transparency"]
        from io import BytesIO

        buf = BytesIO()
        save_kwargs: dict[str, Any] = {}
        if fmt in {"JPEG", "JPG"}:
            fmt = "JPEG"
            if clean.mode not in {"RGB", "L", "CMYK"}:
                clean = clean.convert("RGB")
            save_kwargs["quality"] = 90
            save_kwargs["optimize"] = True
        clean.save(buf, format=fmt, **save_kwargs)
        data = buf.getvalue()
        _atomic_write_bytes(dest, data)
        return data


def _build_gallery_dl_cmd(
    *,
    executable: str,
    cutoff_date: str,
    max_files: int,
    http_timeout: int,
    sleep_request: float,
    sleep_seconds: float,
    archive_path: Path,
    stage: Path,
    platform: str,
    url: str,
    dry_run: bool,
) -> list[str]:
    cmd = [
        executable,
        "--config-ignore",
        "--no-input",
        "--no-colors",
    ]
    if dry_run:
        cmd.append("--dump-json")
    cmd.extend(
        [
            "--range",
            f"1-{max_files}",
            "--filesize-max",
            "25M",
            "--http-timeout",
            str(http_timeout),
            "--retries",
            "1",
            "--sleep-request",
            str(sleep_request),
            "--sleep",
            str(sleep_seconds),
            "--download-archive",
            str(archive_path),
            "--directory",
            str(stage),
            url,
        ]
    )
    return cmd


def _execute_subprocess(
    cmd: list[str],
    stage: Path,
    *,
    timeout: int,
    runner: Callable[[list[str], Path], tuple[int, str]] | None,
) -> tuple[int, str]:
    if runner is not None:
        ret, out = runner(cmd, stage)
        return ret, scrub_sensitive_text(out or "")

    log = stage / f"log-{time.monotonic_ns()}.txt"
    with log.open("wb") as handle:
        proc = subprocess.Popen(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(stage),
            shell=False,
        )
        started = time.monotonic()
        exceeded = False
        while proc.poll() is None:
            if (time.monotonic() - started) > timeout or folder_size(stage) > MAX_STAGING_BYTES:
                exceeded = True
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                break
            time.sleep(0.25)
        ret = 124 if exceeded else (proc.returncode or 0)
    raw_text = log.read_text(encoding="utf8", errors="replace")
    log.unlink(missing_ok=True)
    return ret, scrub_sensitive_text(raw_text)


def _match_metadata_for_file(
    src: Path,
    index: int,
    metadata_records: list[dict[str, Any]],
) -> dict[str, Any]:
    sidecar = src.with_suffix(src.suffix + ".json")
    if sidecar.is_file():
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass

    stem = src.stem.lower()
    name = src.name.lower()
    for record in metadata_records:
        rec_filename = str(record.get("filename") or "").lower()
        rec_ext = str(record.get("extension") or "").lower().lstrip(".")
        rec_full = f"{rec_filename}.{rec_ext}" if rec_filename and rec_ext else rec_filename
        if rec_filename and (rec_filename == stem or rec_full == name):
            return record

    if index < len(metadata_records):
        return metadata_records[index]
    if len(metadata_records) == 1:
        return metadata_records[0]
    return {}


def collect_social(
    project: str | Path,
    platform: str,
    url: str,
    *,
    max_files: int = 40,
    days_limit: int = 365,
    timeout: int = 180,
    http_timeout: int = 20,
    sleep_request: float = 3.0,
    sleep_seconds: float = 1.0,
    executable: str = "gallery-dl",
    runner: Callable[[list[str], Path], tuple[int, str]] | None = None,
    i_have_permission: bool | None = None,
) -> dict[str, Any]:
    try:
        if i_have_permission is False or (i_have_permission is None and runner is None):
            return {
                "status": "review_needed",
                "reason": "permission_required",
                "files_added": 0,
            }
        check_social_url(platform, url)
        if not 1 <= max_files <= 80:
            raise ValueError("max_files must be 1..80")
        if not 1 <= days_limit <= 366:
            raise ValueError("days_limit must be 1..366")
        if not 10 <= timeout <= 600:
            raise ValueError("timeout must be 10..600")
        project_path = Path(project).resolve()
        if not project_path.is_dir() or not (project_path / "research").is_dir():
            raise ValueError("Existing project with research folder required")
        if folder_size(project_path) >= MAX_PROJECT_BYTES:
            return {
                "status": "review_needed",
                "reason": "project_storage_limit",
                "files_added": 0,
            }
        if shutil.which(executable) is None and runner is None:
            return {
                "status": "review_needed",
                "reason": "gallery_dl_not_installed",
                "files_added": 0,
            }

        cutoff = datetime.now(UTC).date().toordinal() - days_limit
        cutoff_date = date.fromordinal(cutoff).isoformat()
        dest = project_path / "assets" / platform
        optimized_dest = project_path / "assets" / "optimized"
        dest.mkdir(parents=True, exist_ok=True)
        optimized_dest.mkdir(parents=True, exist_ok=True)
        archive_path = project_path / "research" / f"social-archive-{platform}.txt"
        manifest_path = project_path / "research" / "social-media-manifest.json"
        asset_manifest_path = project_path / "research" / "asset-manifest.json"
        existing: list[dict[str, Any]] = (
            json.loads(manifest_path.read_text(encoding="utf8"))
            if manifest_path.exists()
            else []
        )
        existing_asset_manifest: list[dict[str, Any]] = (
            json.loads(asset_manifest_path.read_text(encoding="utf8"))
            if asset_manifest_path.exists()
            else []
        )
        known_hashes = {
            str(x["sha256"])
            for x in (*existing, *existing_asset_manifest)
            if "sha256" in x
        }
        rows: list[dict[str, Any]] = []
        corrupt_rejected = 0

        with tempfile.TemporaryDirectory(prefix="pet-social-") as td:
            stage = Path(td)
            # Step 1: Dry-run metadata fetch (--dump-json) before downloading media
            dry_cmd = _build_gallery_dl_cmd(
                executable=executable,
                cutoff_date=cutoff_date,
                max_files=max_files,
                http_timeout=http_timeout,
                sleep_request=sleep_request,
                sleep_seconds=sleep_seconds,
                archive_path=archive_path,
                stage=stage,
                platform=platform,
                url=url,
                dry_run=True,
            )
            dry_ret, dry_out = _execute_subprocess(
                dry_cmd,
                stage,
                timeout=timeout,
                runner=runner,
            )
            metadata_records = parse_dump_json_metadata(dry_out)

            # Check if dry-run already failed and produced no staged files or metadata
            staged_after_dry = [p for p in stage.rglob("*") if file_safe(p)]
            if dry_ret != 0 and not metadata_records and not staged_after_dry:
                reason = classify_social_failure(dry_ret, dry_out)
                return {
                    "status": "review_needed",
                    "reason": reason,
                    "files_added": 0,
                    "platform": platform,
                    "cutoff": cutoff_date,
                    "project_bytes": folder_size(project_path),
                    "extractor_exit_code": dry_ret,
                    "extractor_message": dry_out[-500:],
                }

            # Step 2: Media download pass (skip only if a legacy test runner already staged files)
            if staged_after_dry and runner is not None and not metadata_records:
                ret, stderr = dry_ret, dry_out
            else:
                dl_cmd = _build_gallery_dl_cmd(
                    executable=executable,
                    cutoff_date=cutoff_date,
                    max_files=max_files,
                    http_timeout=http_timeout,
                    sleep_request=sleep_request,
                    sleep_seconds=sleep_seconds,
                    archive_path=archive_path,
                    stage=stage,
                    platform=platform,
                    url=url,
                    dry_run=False,
                )
                ret, stderr = _execute_subprocess(
                    dl_cmd,
                    stage,
                    timeout=timeout,
                    runner=runner,
                )

            combined_msg = (dry_out + "\n" + stderr).strip()
            candidate_files = [p for p in sorted(stage.rglob("*")) if file_safe(p)]

            for idx, src in enumerate(candidate_files):
                if len(rows) >= max_files or folder_size(project_path) >= MAX_PROJECT_BYTES:
                    break
                blob = src.read_bytes()
                is_image = src.suffix.lower() in IMAGE_EXTENSIONS
                if is_image:
                    try:
                        with Image.open(src) as im:
                            im.verify()
                    except (UnidentifiedImageError, OSError, ValueError):
                        corrupt_rejected += 1
                        continue

                sha = hashlib.sha256(blob).hexdigest()
                if sha in known_hashes:
                    continue

                raw_meta = _match_metadata_for_file(src, idx, metadata_records)
                derived = derive_post_metadata(raw_meta, fallback_url=url)

                # If metadata has a verified post_date older than cutoff_date, skip
                if derived["post_date_verified"] and isinstance(derived["post_date"], str):
                    post_day = datetime.fromisoformat(derived["post_date"]).date()
                    if post_day < date.fromisoformat(cutoff_date):
                        continue

                name = f"{platform}-{sha[:16]}{src.suffix.lower()}"
                target = dest / name
                _atomic_write_bytes(target, blob)

                optimized_rel: str | None = None
                optimized_sha: str | None = None
                if is_image:
                    opt_target = optimized_dest / name
                    try:
                        opt_bytes = strip_image_metadata(src, opt_target)
                        optimized_rel = opt_target.relative_to(project_path).as_posix()
                        optimized_sha = hashlib.sha256(opt_bytes).hexdigest()
                    except (UnidentifiedImageError, OSError, ValueError):
                        target.unlink(missing_ok=True)
                        corrupt_rejected += 1
                        continue

                captured_at = datetime.now(UTC).isoformat()
                known_hashes.add(sha)
                rows.append(
                    {
                        "file": target.relative_to(project_path).as_posix(),
                        "optimized_file": optimized_rel,
                        "optimized_sha256": optimized_sha,
                        "platform": platform,
                        "source_profile_url": url,
                        "source_post_url": derived["source_post_url"],
                        "original_post_url": derived["original_post_url"],
                        "author": derived["author"],
                        "post_date": derived["post_date"],
                        "post_date_verified": derived["post_date_verified"],
                        "date_filter_requested_after": cutoff_date,
                        "sha256": sha,
                        "bytes": len(blob),
                        "captured_at": captured_at,
                        "downloaded_at": captured_at,
                        "rights_confirmed_for_preview": False,
                        "ownership_verified": False,
                    }
                )

        if rows:
            existing.extend(rows)
            _atomic_write_text(manifest_path, json.dumps(existing, indent=2))

            canonical_asset_rows = [
                {
                    "file": row["file"],
                    "optimized_file": row.get("optimized_file"),
                    "platform": row.get("platform", platform),
                    "source_url": (
                        row.get("source_post_url") or row.get("source_profile_url") or url
                    ),
                    "original_post_url": (
                        row.get("original_post_url") or row.get("source_post_url") or url
                    ),
                    "sha256": row["sha256"],
                    "bytes": row.get("bytes"),
                    "captured_at": row.get("captured_at"),
                    "post_date": row.get("post_date"),
                    "post_date_verified": bool(row.get("post_date_verified", False)),
                    "rights_confirmed_for_preview": False,
                    "ownership_verified": False,
                    "status": "downloaded",
                    "category": "social_media",
                }
                for row in rows
            ]
            existing_asset_manifest.extend(canonical_asset_rows)
            _atomic_write_text(asset_manifest_path, json.dumps(existing_asset_manifest, indent=2))

        failure_reason: str | None = None
        if not rows:
            if ret != 0 or dry_ret != 0:
                failure_reason = classify_social_failure(ret or dry_ret, combined_msg)
            elif corrupt_rejected > 0:
                failure_reason = "corrupt_or_empty_media"
            else:
                failure_reason = classify_social_failure(ret, combined_msg)

        result: dict[str, Any] = {
            "status": "collected_needs_review" if rows else "review_needed",
            "files_added": len(rows),
            "platform": platform,
            "cutoff": cutoff_date,
            "project_bytes": folder_size(project_path),
            "extractor_exit_code": ret,
            "extractor_message": combined_msg[-500:],
            "note": (
                "No platform login used. Confirm source post, published date, ownership "
                "and reuse permission before selecting any photo."
            ),
        }
        if failure_reason is not None:
            result["reason"] = failure_reason
        return result
    except Exception as exc:
        return {
            "status": "review_needed",
            "reason": classify_social_failure(1, str(exc)),
            "files_added": 0,
            "platform": platform,
            "extractor_message": scrub_sensitive_text(str(exc))[-500:],
        }


def run_cli(argv: Sequence[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--platform", required=True, choices=["instagram", "facebook"])
    p.add_argument("--url", required=True)
    p.add_argument("--max-files", type=int, default=40)
    p.add_argument("--days", type=int, default=365)
    p.add_argument(
        "--i-have-permission",
        action="store_true",
        default=False,
        help="Explicitly confirm permission to collect public social media for preview.",
    )
    args = p.parse_args(list(argv) if argv is not None else None)
    return collect_social(
        args.project,
        args.platform,
        args.url,
        max_files=args.max_files,
        days_limit=args.days,
        i_have_permission=bool(args.i_have_permission),
    )


def main() -> None:
    print(json.dumps(run_cli(), indent=2))


if __name__ == "__main__":
    main()
