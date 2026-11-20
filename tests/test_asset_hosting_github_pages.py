"""TDD tests: publish selected assets to GitHub Pages and emit paste-ready GHL URLs."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from app.asset_hosting import (
    AssetHostingConfig,
    publish_assets_to_github_pages,
    referenced_asset_paths,
)


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "pages-proj"
    opt = p / "assets/optimized"
    opt.mkdir(parents=True)
    (opt / "dog.jpg").write_bytes(b"REAL-DOG-BYTES")
    (p / "research").mkdir(parents=True)
    (p / "research/asset-manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "assets/website/dog.jpg",
                    "url": "https://source.example/dog.jpg",
                    "rights_confirmed_for_preview": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return p


def test_config_requires_all_github_pages_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.asset_hosting import load_asset_hosting_config

    # Isolate from a real local .env so this test only exercises env vars.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GHL_ASSET_HOST", raising=False)
    assert load_asset_hosting_config() is None

    monkeypatch.setenv("GHL_ASSET_HOST", "github_pages")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        load_asset_hosting_config()

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    monkeypatch.setenv("GITHUB_PAGES_REPO", "owner/site-assets")
    cfg = load_asset_hosting_config()
    assert cfg is not None
    assert cfg.repo == "owner/site-assets"
    assert cfg.base_url == "https://owner.github.io/site-assets/"


def test_publisher_creates_gh_pages_branch_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First-run setup must not require manual branch creation in GitHub UI."""
    p = _project(tmp_path)
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, status: int, payload: dict[str, object] | None = None) -> None:
            self.status_code = status
            self._payload = payload or {}
            self.text = ""

        def json(self) -> dict[str, object]:
            return self._payload

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"method": "GET", "url": url})
        if url.endswith("/branches/gh-pages"):
            return FakeResponse(404)
        if url.endswith("/repos/owner/site-assets"):
            return FakeResponse(200, {"default_branch": "main"})
        if "/git/ref/heads/main" in url:
            return FakeResponse(200, {"object": {"sha": "base-sha"}})
        return FakeResponse(404)

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"method": "POST", "url": url, "json": kwargs.get("json")})
        return FakeResponse(201, {})

    def fake_put(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"method": "PUT", "url": url})
        return FakeResponse(201, {})

    monkeypatch.setattr("app.asset_hosting.requests.get", fake_get)
    monkeypatch.setattr("app.asset_hosting.requests.post", fake_post)
    monkeypatch.setattr("app.asset_hosting.requests.put", fake_put)

    cfg = AssetHostingConfig(
        token="test-token", repo="owner/site-assets", base_url="https://owner.github.io/site-assets/"
    )
    mapping = publish_assets_to_github_pages(p, ["assets/optimized/dog.jpg"], cfg)
    assert mapping["assets/optimized/dog.jpg"].startswith("https://owner.github.io/site-assets/")
    ref_call = next(c for c in calls if c["method"] == "POST" and "git/refs" in str(c["url"]))
    assert ref_call["json"] == {"ref": "refs/heads/gh-pages", "sha": "base-sha"}


def test_preflight_treats_placeholder_token_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.asset_hosting import preflight_hosting

    monkeypatch.setenv("GHL_ASSET_HOST", "github_pages")
    monkeypatch.setenv("GITHUB_PAGES_REPO", "owner/site-assets")
    monkeypatch.setenv("GITHUB_TOKEN", "YOUR_TOKEN_HERE")
    info = preflight_hosting()
    assert info["configured"] is False
    assert "GITHUB_TOKEN" in str(info["missing"])
    assert "YOUR_TOKEN_HERE" not in json.dumps(info)


def test_publisher_enables_github_pages_before_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.asset_hosting import enable_github_pages

    calls: list[tuple[str, object]] = []

    class FakeResponse:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.text = ""

        def json(self) -> dict[str, object]:
            return {}

    def fake_put(url: str, **kwargs: object) -> FakeResponse:
        calls.append((url, kwargs.get("json")))
        return FakeResponse(201)

    monkeypatch.setattr("app.asset_hosting.requests.put", fake_put)
    cfg = AssetHostingConfig(token="t", repo="o/r", base_url="https://o.github.io/r/")
    assert enable_github_pages(cfg) is True
    pages_url, payload = calls[0]
    assert pages_url.endswith("/repos/o/r/pages")
    assert payload == {
        "build_type": "legacy",
        "source": {"branch": "gh-pages", "path": "/"},
    }


def test_enable_pages_accepts_204_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.asset_hosting import enable_github_pages

    class FakeResponse:
        status_code = 204
        text = ""

    monkeypatch.setattr(
        "app.asset_hosting.requests.put", lambda *_a, **_k: FakeResponse()
    )
    cfg = AssetHostingConfig(token="t", repo="o/r", base_url="https://o.github.io/r/")
    assert enable_github_pages(cfg) is True


def test_preflight_reads_local_dotenv_without_printing_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.asset_hosting import preflight_hosting

    for key in ("GHL_ASSET_HOST", "GITHUB_PAGES_REPO", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "GHL_ASSET_HOST=github_pages\n"
        "GITHUB_PAGES_REPO=owner/site-assets\n"
        "GITHUB_TOKEN=dotenv-secret-value\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    info = preflight_hosting()
    assert info["configured"] is True
    assert "dotenv-secret-value" not in json.dumps(info)


def test_preflight_reports_config_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.asset_hosting import preflight_hosting

    monkeypatch.setenv("GHL_ASSET_HOST", "github_pages")
    monkeypatch.setenv("GITHUB_PAGES_REPO", "owner/site-assets")
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-should-never-appear")
    info = preflight_hosting()
    assert info["configured"] is True
    assert info["repo"] == "owner/site-assets"
    assert "super-secret-should-never-appear" not in json.dumps(info)


def test_publisher_maps_relative_paths_to_public_https_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = _project(tmp_path)
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
            self.status_code = status
            self._payload = payload or {}
            self.text = ""

        def json(self) -> dict[str, Any]:
            return self._payload

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        if url.endswith("/branches/gh-pages"):
            return FakeResponse(200, {})
        return FakeResponse(404)

    def fake_put(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": "PUT", "url": url, "kwargs": kwargs})
        return FakeResponse(201, {"content": {"html_url": "x"}})

    monkeypatch.setattr("app.asset_hosting.requests.get", fake_get)
    monkeypatch.setattr("app.asset_hosting.requests.put", fake_put)

    cfg = AssetHostingConfig(
        token="test-token", repo="owner/site-assets", base_url="https://owner.github.io/site-assets/"
    )
    mapping = publish_assets_to_github_pages(p, ["assets/optimized/dog.jpg"], cfg)

    assert (
        mapping["../assets/optimized/dog.jpg"]
        == "https://owner.github.io/site-assets/pages-proj/assets/optimized/dog.jpg"
    )
    assert any(c["method"] == "PUT" for c in calls)
    put_call = next(
        c for c in calls if c["method"] == "PUT" and "content" in (c["kwargs"].get("json") or {})
    )
    assert put_call["kwargs"]["json"]["branch"] == "gh-pages"
    assert base64.b64decode(put_call["kwargs"]["json"]["content"]) == b"REAL-DOG-BYTES"
    assert put_call["kwargs"]["headers"]["Authorization"] == "Bearer test-token"


def test_publisher_refuses_unconfirmed_rights(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = _project(tmp_path)
    manifest = p / "research/asset-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data[0]["rights_confirmed_for_preview"] = False
    manifest.write_text(json.dumps(data), encoding="utf-8")

    class _Exists:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr("app.asset_hosting.requests.get", lambda *_a, **_k: _Exists())
    class _Created:
        status_code = 201

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr("app.asset_hosting.requests.put", lambda *_a, **_k: _Created())
    cfg = AssetHostingConfig(token="t", repo="o/r", base_url="https://o.github.io/r/")
    with pytest.raises(ValueError, match="rights_confirmed_for_preview"):
        publish_assets_to_github_pages(p, ["assets/optimized/dog.jpg"], cfg)


def test_referenced_asset_paths_finds_html_images() -> None:
    html = (
        '<img src="../assets/optimized/hero.jpg">'
        '<img src="assets/optimized/g1.jpg" loading="lazy">'
        '<div style="background:url(../assets/optimized/bg.png)"></div>'
        '<a href="https://external.example/x.jpg">x</a>'
    )
    paths = referenced_asset_paths(html)
    assert "assets/optimized/hero.jpg" in paths
    assert "assets/optimized/g1.jpg" in paths
    assert "assets/optimized/bg.png" in paths
    assert "https://external.example/x.jpg" not in paths


def test_approval_exports_ghl_code_with_public_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Final delivered GHL snippet must contain public URLs, never relative/local paths."""
    from app.approval import export_approved_deliverables

    p = _project(tmp_path)
    (p / "website/css").mkdir(parents=True)
    (p / "website/index.html").write_text(
        '<html><head></head><body><main><img src="../assets/optimized/dog.jpg" '
        'alt="Dog"></main></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.approval.maybe_publish_ghl_assets",
        lambda _root: {"../assets/optimized/dog.jpg": "https://o.github.io/r/p/assets/dog.jpg"},
    )

    export_approved_deliverables(p, is_approved=True)
    ghl = (p / "delivery/ghl-ready.html").read_text(encoding="utf-8")
    assert "https://o.github.io/r/p/assets/dog.jpg" in ghl
    assert "../assets/" not in ghl
    assert "127.0.0.1" not in ghl


def test_approve_release_rewrites_ghl_with_public_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real approval gate used by the cockpit must publish/rewrite too."""
    from app.approval import approve_release

    from test_approval_archival import _human_review_attestation, _scaffold_reviewable_project

    projects_dir, project_root = _scaffold_reviewable_project(tmp_path, "hosted-salon")
    monkeypatch.setattr(
        "app.approval.maybe_publish_ghl_assets",
        lambda _root: {
            "../assets/optimized/hero_dog.jpg": "https://cdn.example.com/hero_dog.jpg",
            "assets/optimized/hero_dog.jpg": "https://cdn.example.com/hero_dog.jpg",
        },
    )
    approve_release(
        "hosted-salon",
        version="v1",
        operator_name="Owner",
        notes="Public asset hosting configured.",
        projects_dir=projects_dir,
        review_attestation=_human_review_attestation(project_root),
    )
    ghl = (project_root / "delivery/ghl-ready.html").read_text(encoding="utf-8")
    assert "https://cdn.example.com/hero_dog.jpg" in ghl
    assert "../assets/" not in ghl
