"""TDD tests: deterministic dependency lock and CI build/health smoke coverage."""

from __future__ import annotations

from pathlib import Path


def test_lockfile_is_pinned_and_covers_all_extras() -> None:
    repo = Path(__file__).parents[1]
    lock = (repo / "requirements.lock.txt").read_text(encoding="utf-8")
    lines = [line.strip() for line in lock.splitlines() if line.strip()]

    assert any(line.startswith("annotated-types==") for line in lines)
    assert any(line.startswith("playwright==") for line in lines)
    assert any(line.startswith("gallery-dl==") for line in lines)
    assert not any(line.startswith("-e ") for line in lines)
    assert not any(">=" in line or "<" in line for line in lines if not line.startswith("#"))


def test_dockerfile_installs_only_the_lockfile() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock.txt" in dockerfile
    assert "-r requirements.txt" not in dockerfile
    assert "-r requirements-browser.txt" not in dockerfile
    assert "-r requirements-social.txt" not in dockerfile


def test_ci_verifies_lock_compose_build_and_health() -> None:
    ci = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "uv export" in ci
    assert "docker compose config" in ci
    assert "docker build" in ci
    assert "/health" in ci
