"""TDD tests: immutable-safe M5 re-approval (fresh draft + next version)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.approval import next_release_version
from app.pipeline import _reset_draft_directories

from engine.exporter import check_used_asset_rights


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_draft_reset_preserves_frozen_delivery_versions_and_archives(tmp_path: Path) -> None:
    project = tmp_path / "paws-spa"
    for rel in (
        "delivery/release-manifest.json",
        "versions/v1/release-manifest.json",
        "versions/v1/approved-homepage.html",
        "_archives/paws-spa-approved.zip",
        "website/sections/hero.html",
        "qa/qa-report.json",
        "assets/optimized/old.jpg",
    ):
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"content:{rel}".encode())

    frozen = {
        rel: _file_hash(project / rel)
        for rel in (
            "delivery/release-manifest.json",
            "versions/v1/release-manifest.json",
            "versions/v1/approved-homepage.html",
        )
    }
    archive_path = project / "_archives/paws-spa-approved.zip"
    archive_before = _file_hash(archive_path)

    _reset_draft_directories(project)

    for rel, digest in frozen.items():
        assert (project / rel).is_file(), f"Frozen file lost: {rel}"
        assert _file_hash(project / rel) == digest
    assert _file_hash(archive_path) == archive_before
    assert not (project / "website").exists()
    assert not (project / "qa").exists()
    assert not (project / "assets").exists()


def test_rights_check_matches_optimized_copy_by_sha(tmp_path: Path) -> None:
    """An optimized copy with the same SHA as a confirmed original must be allowed."""
    import hashlib
    import json

    project = tmp_path / "salon"
    (project / "assets/facebook").mkdir(parents=True)
    (project / "assets/optimized").mkdir(parents=True)
    payload = b"real-dog-bytes"
    (project / "assets/facebook/dog.jpg").write_bytes(payload)
    (project / "assets/optimized/dog.jpg").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (project / "research").mkdir()
    (project / "research/asset-manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "assets/facebook/dog.jpg",
                    "sha256": digest,
                    "rights_confirmed_for_preview": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    html = project / "release-candidate.html"
    html.write_text('<img src="../assets/optimized/dog.jpg" alt="Dog">', encoding="utf-8")

    assert check_used_asset_rights(project, source_html_path=html) == []


def test_manifest_index_keeps_confirmed_row_over_unconfirmed_duplicate(tmp_path: Path) -> None:
    """A stale unconfirmed social row must not overwrite a confirmed asset row."""
    import hashlib
    import json

    project = tmp_path / "salon"
    (project / "research").mkdir(parents=True)
    payload = b"dog"
    digest = hashlib.sha256(payload).hexdigest()
    rel = "assets/facebook/dog.jpg"
    confirmed = {"file": rel, "sha256": digest, "rights_confirmed_for_preview": True}
    stale = {"file": rel, "sha256": digest, "rights_confirmed_for_preview": False}
    (project / "research/asset-manifest.json").write_text(json.dumps([confirmed]), encoding="utf-8")
    (project / "research/social-media-manifest.json").write_text(
        json.dumps([stale]), encoding="utf-8"
    )

    from engine.exporter import _load_manifest_index

    index = _load_manifest_index(project)
    assert index["assets/optimized/dog.jpg"]["rights_confirmed_for_preview"] is True


def test_next_release_version_uses_max_plus_one(tmp_path: Path) -> None:
    project = tmp_path / "salon"
    assert next_release_version(project) == "v1"

    versions = project / "versions"
    for version in ("v1", "v2", "v3"):
        (versions / version).mkdir(parents=True)
    assert next_release_version(project) == "v4"

    (versions / "v10").mkdir()
    assert next_release_version(project) == "v11"
