"""Publish selected, rights-confirmed assets to GitHub Pages for paste-ready GHL code.

The GHL "Custom HTML/JavaScript" element renders on GHL's own domain, so relative
``../assets/...`` paths (or a localhost CDN) produce broken images. This module
uploads only the assets actually referenced by the approved page to a public
GitHub Pages site and returns a URL map for the GHL exporter (brief §2/§J).

Secrets are read from environment variables only and are never logged or persisted.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

_API_BASE = "https://api.github.com"
_DEFAULT_BRANCH = "gh-pages"
_REQUEST_TIMEOUT = 30
_SRC_PATTERN = re.compile(
    r"(?:src|poster|href)\s*=\s*(['\"])(.*?)\1|url\(\s*(['\"]?)([^)]*?)\3\s*\)",
    re.I,
)
_LOCAL_ASSET_RE = re.compile(r"^(?:\.\./)+assets/|^assets/|^/assets/")


@dataclass(frozen=True)
class AssetHostingConfig:
    """Runtime configuration for public asset hosting."""

    token: str = field(repr=False)
    repo: str
    branch: str = _DEFAULT_BRANCH
    base_url: str = ""

    @property
    def safe_token(self) -> str:
        return "***"


def load_asset_hosting_config() -> AssetHostingConfig | None:
    """Return config only when ``GHL_ASSET_HOST=github_pages`` is explicitly set."""
    if os.environ.get("GHL_ASSET_HOST", "").strip().lower() != "github_pages":
        return None
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise ValueError("GHL_ASSET_HOST=github_pages requires GITHUB_TOKEN in the environment")
    repo = os.environ.get("GITHUB_PAGES_REPO", "").strip()
    if not repo or "/" not in repo:
        raise ValueError("GHL_ASSET_HOST=github_pages requires GITHUB_PAGES_REPO as 'owner/repo'")
    branch = os.environ.get("GITHUB_PAGES_BRANCH", _DEFAULT_BRANCH).strip() or _DEFAULT_BRANCH
    base_url = os.environ.get("GITHUB_PAGES_BASE_URL", "").strip()
    if not base_url:
        owner, name = repo.split("/", 1)
        base_url = f"https://{name}/" if name.endswith(".github.io") else (
            f"https://{owner}.github.io/{name}/"
        )
    return AssetHostingConfig(
        token=token, repo=repo, branch=branch, base_url=base_url.rstrip("/") + "/"
    )


def referenced_asset_paths(html_text: str) -> set[str]:
    """Return normalized relative asset paths referenced by HTML/CSS (external URLs ignored)."""
    found: set[str] = set()
    for match in _SRC_PATTERN.finditer(html_text):
        raw = (match.group(2) or match.group(4) or "").strip()
        if not _LOCAL_ASSET_RE.match(raw):
            continue
        normalized = raw.lstrip("/")
        while normalized.startswith("../"):
            normalized = normalized[3:]
        if normalized.startswith("assets/"):
            found.add(normalized)
    return found


def _rights_by_filename(project_root: Path) -> dict[str, bool]:
    rights: dict[str, bool] = {}
    for rel in ("research/asset-manifest.json", "research/social-media-manifest.json"):
        path = project_root / rel
        if not path.is_file():
            continue
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload = payload.get("assets") or payload.get("items") or []
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("file", "")).replace("\\", "/").rsplit("/", 1)[-1]
            if name:
                rights[name] = bool(item.get("rights_confirmed_for_preview", False))
    return rights


def _headers(config: AssetHostingConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _content_api_url(config: AssetHostingConfig, repo_path: str) -> str:
    return f"{_API_BASE}/repos/{config.repo}/contents/{quote(repo_path, safe='/')}"


def publish_assets_to_github_pages(
    project_root: Path,
    asset_paths: list[str] | set[str],
    config: AssetHostingConfig,
) -> dict[str, str]:
    """Upload referenced assets and return a URL map usable by the GHL exporter.

    Fails closed when rights for any asset are not explicitly confirmed.
    """
    project_id = project_root.name
    rights = _rights_by_filename(project_root)
    mapping: dict[str, str] = {}

    for asset_path in sorted(set(asset_paths)):
        local = project_root / asset_path
        if not local.is_file():
            raise ValueError(f"Cannot publish missing asset: {asset_path}")
        filename = local.name
        if rights.get(filename) is not True:
            raise ValueError(
                "GHL asset publish blocked: rights_confirmed_for_preview "
                f"is not true for {filename}"
            )

        repo_path = f"{project_id}/{asset_path.lstrip('/')}"
        url = _content_api_url(config, repo_path)
        get_resp = requests.get(
            url,
            params={"ref": config.branch},
            headers=_headers(config),
            timeout=_REQUEST_TIMEOUT,
        )
        payload: dict[str, Any] = {
            "message": f"Publish {project_id} GHL asset: {filename}",
            "content": base64.b64encode(local.read_bytes()).decode("ascii"),
            "branch": config.branch,
        }
        if get_resp.status_code == 200:
            existing = get_resp.json()
            if isinstance(existing, dict) and existing.get("sha"):
                payload["sha"] = str(existing["sha"])
        elif get_resp.status_code != 404:
            raise RuntimeError(
                f"GitHub Pages asset lookup failed ({get_resp.status_code}) for {filename}"
            )

        put_resp = requests.put(
            url, json=payload, headers=_headers(config), timeout=_REQUEST_TIMEOUT
        )
        if put_resp.status_code not in (200, 201):
            hint = (
                " Ensure GITHUB_PAGES_BRANCH exists and GitHub Pages is enabled."
                if put_resp.status_code in (404, 409, 422)
                else ""
            )
            raise RuntimeError(
                f"GitHub Pages asset upload failed ({put_resp.status_code}) for {filename}.{hint}"
            )

        public_url = f"{config.base_url}{quote(repo_path, safe='/')}"
        mapping[asset_path] = public_url
        mapping[f"../{asset_path}"] = public_url
        mapping[f"/{asset_path}"] = public_url

    return mapping


def maybe_publish_ghl_assets(project_root: str | Path) -> dict[str, str]:
    """Publish when configured; otherwise return an empty map (local preview unchanged)."""
    config = load_asset_hosting_config()
    if config is None:
        return {}
    root = Path(project_root).resolve()
    html_path = root / "website/index.html"
    if not html_path.is_file():
        return {}
    css_path = root / "website/css/styles.css"
    html_text = html_path.read_text(encoding="utf-8")
    if css_path.is_file():
        html_text += "\n" + css_path.read_text(encoding="utf-8")
    asset_paths = referenced_asset_paths(html_text)
    if not asset_paths:
        return {}
    mapping = publish_assets_to_github_pages(root, asset_paths, config)
    evidence = root / "release-candidate"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "ghl-asset-urls.json").write_text(
        json.dumps(
            {
                "host": "github_pages",
                "repo": config.repo,
                "branch": config.branch,
                "base_url": config.base_url,
                "urls": mapping,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return mapping


def is_public_asset_snippet(snippet: str) -> bool:
    """True when a GHL snippet contains no local/relative asset references."""
    lowered = snippet.lower()
    return not any(
        bad in lowered
        for bad in ("../assets/", 'src="assets/', "127.0.0.1", "http://localhost", "https://localhost")
    )


def assert_public_asset_urls(snippet: str) -> None:
    """Fail if a paste-ready GHL snippet still contains local/relative asset refs."""
    if not is_public_asset_snippet(snippet):
        raise ValueError("GHL snippet is not paste-ready: contains local/relative asset URLs")
