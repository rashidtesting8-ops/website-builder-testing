from __future__ import annotations

from pathlib import Path


def test_docker_demo_includes_browser_qa_and_review_cockpit() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")

    assert "mcr.microsoft.com/playwright/python:v1.55.0-noble" in dockerfile
    assert "requirements-browser.txt" in dockerfile
    assert "n8nio/n8n:2.47.3" in compose
    assert "cockpit:" in compose
    assert "127.0.0.1:8888:8888" in compose
    assert "COCKPIT_BASE_URL=http://127.0.0.1:8888" in compose
    assert "N8N_SECURE_COOKIE=false" in compose
    assert "N8N_RUNNERS_ENABLED=false" in compose
    assert "condition: service_healthy" in compose
    assert "http://127.0.0.1:5678/healthz" in compose
    assert "/api/health" in compose


def test_n8n_demo_workflow_uses_reachable_operator_cockpit_url() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    workflow = (
        repo_root / "n8n" / "04-interactive-one-click-luxury-demo.json"
    ).read_text(encoding="utf-8")

    assert "http://127.0.0.1:8888/review/" in workflow
