from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.claims_lint import run_claims_lint

PROJECT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "projects"
    / "lilys-club-demo-20261111"
)
pytestmark = pytest.mark.skipif(
    not (PROJECT_ROOT / "research" / "business-facts.json").is_file(),
    reason="Local Lily demo data is Git-ignored and unavailable in clean checkouts.",
)


def test_lilys_public_source_demo_contains_no_claimed_owner_confirmation() -> None:
    facts = json.loads(
        (PROJECT_ROOT / "research" / "business-facts.json").read_text(
            encoding="utf-8"
        )
    )["facts"]
    assert all(
        "owner confirmation" not in str(fact.get("source_text", "")).lower()
        and "owner confirmation" not in str(fact.get("source_text_snippet", "")).lower()
        for fact in facts
    )


def test_lilys_public_source_demo_stays_review_only() -> None:
    facts = json.loads(
        (PROJECT_ROOT / "research" / "business-facts.json").read_text(
            encoding="utf-8"
        )
    )["facts"]
    assert facts
    assert all(
        fact.get("source_url")
        and (fact.get("source_text") or fact.get("source_text_snippet"))
        and isinstance(fact.get("verified", fact.get("is_verified")), bool)
        and fact.get("last_checked_at")
        for fact in facts
    )
    assert all(
        "owner confirmation" not in str(fact.get("source_text", "")).lower()
        and "owner confirmation" not in str(fact.get("source_text_snippet", "")).lower()
        for fact in facts
    )

    contact_facts = [
        fact for fact in facts if fact["category"] in {"phone", "address"}
    ]
    assert contact_facts
    assert all(
        fact["status"] == "needs_review"
        and fact.get("verified", fact.get("is_verified")) is False
        for fact in contact_facts
    )

    selection = json.loads(
        (PROJECT_ROOT / "design" / "image-selection.json").read_text(
            encoding="utf-8"
        )
    )
    hero = selection["hero_image"]
    gallery = selection["grooming_gallery"]

    assets = json.loads(
        (PROJECT_ROOT / "research" / "asset-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    selected_filenames = {hero["filename"], *(item["filename"] for item in gallery)}
    selected_assets = [
        asset
        for asset in assets
        if Path(asset.get("file", "")).name in selected_filenames
    ]
    assert len(selected_assets) == len(selected_filenames)
    assert all(
        asset["rights_confirmed_for_preview"] is False
        for asset in selected_assets
    )

    assert not (PROJECT_ROOT / "delivery").exists()
    candidate_path = PROJECT_ROOT / "release-candidate" / "standalone.html"
    candidate_html = candidate_path.read_text(encoding="utf-8")
    assert "Local preview only. Photo reuse rights have not been confirmed" in candidate_html
    assert all(fact["claim_value"] not in candidate_html for fact in contact_facts)

    qa_report = json.loads(
        (PROJECT_ROOT / "qa" / "qa-report.json").read_text(encoding="utf-8")
    )
    assert qa_report["passed"] is True
    assert qa_report["candidate_sha256"] == hashlib.sha256(
        candidate_path.read_bytes()
    ).hexdigest()

    claims_lint = run_claims_lint(PROJECT_ROOT)
    assert claims_lint.passed is True
