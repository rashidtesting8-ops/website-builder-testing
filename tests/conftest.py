"""Global test isolation: never let a developer's real .env leak into tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_public_asset_hosting(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Disable GitHub Pages publishing unless a test explicitly opts in.

    A real local .env with GHL_ASSET_HOST=github_pages would otherwise make
    unrelated approval tests attempt live GitHub API calls.
    """
    monkeypatch.setenv("GHL_ASSET_HOST", "off")
