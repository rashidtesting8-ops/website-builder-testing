"""Tests for the hardened optional social media collector (gallery-dl subprocess wrapper)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from app.rights import confirm_asset_rights
from PIL import Image

from engine.social_collect import (
    classify_social_failure,
    collect_social,
    derive_post_metadata,
    run_cli,
    strip_image_metadata,
)

RECORDED_SUCCESS_DUMP_JSON = json.dumps(
    [
        [2, {"category": "instagram", "username": "paws_and_claws_salon"}],
        [
            3,
            "https://scontent.cdninstagram.com/v/t51.2885-15/fresh_cut.jpg",
            {
                "category": "instagram",
                "filename": "fresh_cut",
                "extension": "jpg",
                "post_shortcode": "C8abc123",
                "post_url": "https://www.instagram.com/p/C8abc123/",
                "username": "paws_and_claws_salon",
                "date": "2026-06-15 14:22:10",
            },
        ],
    ]
)

RECORDED_LOGIN_WALL_OUTPUT = (
    "[instagram][error] HttpError: '401 Unauthorized' for 'https://www.instagram.com/demo/'\n"
    "[instagram][error] Login required to view this profile (checkpoint_required)"
)

RECORDED_RATE_LIMIT_OUTPUT = (
    "[instagram][warning] 429 Too Many Requests\n"
    "[instagram][error] Rate limit exceeded. Please wait a few minutes before you try again."
)

RECORDED_EXTRACTOR_BROKEN_OUTPUT = (
    "Traceback (most recent call last):\n"
    "  File 'gallery_dl/extractor/instagram.py', line 212\n"
    "ExtractorError: Unable to extract shared data (KeyError: 'xdt_api__v1__feed')"
)


def _make_jpeg_with_exif_gps() -> bytes:
    """Create a valid JPEG byte stream containing EXIF orientation and GPSInfo tags."""
    im = Image.new("RGB", (64, 64), color=(120, 180, 210))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation
    exif[0x010E] = "Paws Grooming Customer Dog"  # ImageDescription
    exif[0x8825] = {1: "N", 2: (37, 46, 30), 3: "W", 4: (122, 25, 10)}  # GPSInfo
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_permission_flag_required_on_cli_and_collect(tmp_path: Path) -> None:
    project = tmp_path / "salon"
    (project / "research").mkdir(parents=True)

    called = False

    def stub_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        nonlocal called
        called = True
        return 0, ""

    res = collect_social(
        project,
        "instagram",
        "https://www.instagram.com/demo/",
        runner=stub_runner,
        i_have_permission=False,
    )
    assert res["status"] == "review_needed"
    assert res["reason"] == "permission_required"
    assert res["files_added"] == 0
    assert not called

    cli_res = run_cli(
        [
            "--project",
            str(project),
            "--platform",
            "instagram",
            "--url",
            "https://www.instagram.com/demo/",
        ]
    )
    assert cli_res["status"] == "review_needed"
    assert cli_res["reason"] == "permission_required"


def test_collect_social_writes_canonical_asset_manifest(tmp_path: Path) -> None:
    project = tmp_path / "salon"
    (project / "research").mkdir(parents=True)

    def stub_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        if "--dump-json" in cmd:
            return 0, RECORDED_SUCCESS_DUMP_JSON
        (stage / "fresh_cut.jpg").write_bytes(_make_jpeg_with_exif_gps())
        return 0, "fresh_cut.jpg\n"

    result = collect_social(
        project,
        "instagram",
        "https://www.instagram.com/paws_and_claws_salon/",
        max_files=5,
        runner=stub_runner,
        i_have_permission=True,
    )
    assert result["status"] == "collected_needs_review"
    asset_manifest_path = project / "research" / "asset-manifest.json"
    asset_manifest = json.loads(asset_manifest_path.read_text(encoding="utf-8"))
    assert len(asset_manifest) == 1
    assert asset_manifest[0]["file"].startswith("assets/")
    assert asset_manifest[0]["rights_confirmed_for_preview"] is False
    assert asset_manifest[0]["platform"] == "instagram"


def test_confirm_asset_rights_creates_missing_project_record(tmp_path: Path) -> None:
    project = tmp_path / "live-social-project"
    (project / "research").mkdir(parents=True)
    (project / "assets" / "facebook").mkdir(parents=True)
    asset_file = project / "assets" / "facebook" / "demo.jpg"
    asset_file.write_bytes(b"fake-jpg-bytes")
    (project / "research" / "asset-manifest.json").write_text(
        json.dumps([
            {
                "file": "assets/facebook/demo.jpg",
                "sha256": "" + "0" * 64,
                "source_url": "https://www.facebook.com/demo",
                "rights_confirmed_for_preview": False,
            }
        ]),
        encoding="utf-8",
    )

    # Force the manifest SHA to match the real file bytes for the update path.
    import hashlib
    actual_sha = hashlib.sha256(asset_file.read_bytes()).hexdigest()
    manifest_path = project / "research" / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[0]["sha256"] = actual_sha
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    confirm_asset_rights(
        project,
        asset=actual_sha,
        basis="owner_permission",
        operator="Alice Reviewer",
        evidence="Reviewed by owner-approved business page evidence.",
        projects_dir=tmp_path,
    )

    updated = json.loads((project / "research" / "asset-manifest.json").read_text(encoding="utf-8"))
    assert updated[0]["rights_confirmed_for_preview"] is True
    assert updated[0]["rights_basis"] == "owner_permission"
    assert (tmp_path / "editvista.sqlite3").exists()


def test_success_dry_run_metadata_and_exif_stripping(tmp_path: Path) -> None:
    project = tmp_path / "salon"
    (project / "research").mkdir(parents=True)

    commands: list[list[str]] = []
    jpeg_with_gps = _make_jpeg_with_exif_gps()

    # Verify the raw fixture actually has EXIF / GPS tags before collection
    with Image.open(io.BytesIO(jpeg_with_gps)) as raw_im:
        raw_exif = raw_im.getexif()
        assert len(raw_exif) > 0
        assert 0x8825 in raw_exif

    def stub_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        commands.append(cmd)
        if "--dump-json" in cmd:
            return 0, RECORDED_SUCCESS_DUMP_JSON
        (stage / "fresh_cut.jpg").write_bytes(jpeg_with_gps)
        return 0, "fresh_cut.jpg\n"

    out = collect_social(
        project,
        "instagram",
        "https://www.instagram.com/paws_and_claws_salon/",
        max_files=5,
        days_limit=365,
        timeout=60,
        runner=stub_runner,
        i_have_permission=True,
    )

    assert out["status"] == "collected_needs_review"
    assert out["files_added"] == 1
    assert len(commands) == 2

    # 1st command is dry-run metadata fetch; 2nd is media download
    dry_cmd, dl_cmd = commands[0], commands[1]
    assert "--dump-json" in dry_cmd
    assert "--dump-json" not in dl_cmd

    for cmd in (dry_cmd, dl_cmd):
        assert "--config-ignore" in cmd
        assert "--no-input" in cmd
        assert "--http-timeout" in cmd
        assert "--range" in cmd
        assert cmd[cmd.index("--range") + 1] == "1-5"
        assert "--filesize-max" in cmd
        assert "--sleep-request" in cmd
        assert "--download-archive" in cmd
        assert "--date-after" not in cmd
        # Never pass cookie or credential flags
        for forbidden in ("--cookies", "--cookies-from-browser", "--username", "--password"):
            assert forbidden not in cmd

    manifest = json.loads(
        (project / "research" / "social-media-manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest) == 1
    asset: dict[str, Any] = manifest[0]

    assert asset["post_date"] == "2026-06-15T14:22:10+00:00"
    assert asset["post_date_verified"] is True
    assert asset["original_post_url"] == "https://www.instagram.com/p/C8abc123/"
    assert asset["author"] == "paws_and_claws_salon"
    assert asset["rights_confirmed_for_preview"] is False
    assert isinstance(asset["sha256"], str) and len(asset["sha256"]) == 64
    assert isinstance(asset["captured_at"], str) and "T" in asset["captured_at"]

    # Check optimized copy exists and has EXIF/GPS completely stripped
    opt_path = project / asset["optimized_file"]
    assert opt_path.exists()
    with Image.open(opt_path) as opt_im:
        opt_exif = opt_im.getexif()
        assert len(opt_exif) == 0
        assert 0x8825 not in opt_exif
        assert "exif" not in opt_im.info


@pytest.mark.parametrize(
    ("exit_code", "recorded_output", "expected_reason"),
    [
        (1, RECORDED_LOGIN_WALL_OUTPUT, "login_required"),
        (1, RECORDED_RATE_LIMIT_OUTPUT, "rate_limited"),
        (1, RECORDED_EXTRACTOR_BROKEN_OUTPUT, "extractor_broken"),
        (2, "fatal network error: connection reset", "unknown"),
    ],
)
def test_failure_classifications_never_raise(
    tmp_path: Path,
    exit_code: int,
    recorded_output: str,
    expected_reason: str,
) -> None:
    project = tmp_path / "salon"
    (project / "research").mkdir(parents=True)

    def failing_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        return exit_code, recorded_output

    result = collect_social(
        project,
        "instagram",
        "https://www.instagram.com/demo/",
        runner=failing_runner,
        i_have_permission=True,
    )
    assert result["status"] == "review_needed"
    assert result["reason"] == expected_reason
    assert result["files_added"] == 0


def test_corrupt_image_rejected_without_crashing(tmp_path: Path) -> None:
    project = tmp_path / "salon"
    (project / "research").mkdir(parents=True)

    def corrupt_only_runner(cmd: list[str], stage: Path) -> tuple[int, str]:
        if "--dump-json" in cmd:
            return 0, RECORDED_SUCCESS_DUMP_JSON
        (stage / "fresh_cut.jpg").write_bytes(b"corrupt-not-a-valid-jpeg-file")
        return 0, ""

    result = collect_social(
        project,
        "instagram",
        "https://www.instagram.com/demo/",
        runner=corrupt_only_runner,
        i_have_permission=True,
    )
    assert result["status"] == "review_needed"
    assert result["files_added"] == 0
    assert result["reason"] == "corrupt_or_empty_media"


def test_derive_post_metadata_unverified_when_timestamp_missing() -> None:
    meta = derive_post_metadata(
        {"filename": "dog", "username": "groomer_jane"},
        fallback_url="https://www.instagram.com/groomer_jane/",
    )
    assert meta["post_date"] is None
    assert meta["post_date_verified"] is False
    assert meta["author"] == "groomer_jane"
    assert meta["original_post_url"] == "https://www.instagram.com/groomer_jane/"


def test_classify_social_failure_unit() -> None:
    assert classify_social_failure(1, RECORDED_LOGIN_WALL_OUTPUT) == "login_required"
    assert classify_social_failure(1, RECORDED_RATE_LIMIT_OUTPUT) == "rate_limited"
    assert classify_social_failure(1, RECORDED_EXTRACTOR_BROKEN_OUTPUT) == "extractor_broken"
    assert classify_social_failure(1, "something unexpected") == "unknown"


def test_strip_image_metadata_helper(tmp_path: Path) -> None:
    src = tmp_path / "raw.jpg"
    dst = tmp_path / "clean.jpg"
    src.write_bytes(_make_jpeg_with_exif_gps())
    strip_image_metadata(src, dst)
    assert dst.exists()
    with Image.open(dst) as im:
        assert len(im.getexif()) == 0
