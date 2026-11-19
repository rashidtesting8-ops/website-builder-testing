"""TDD tests: cockpit and worker APIs require a token when auth is enabled."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import requests
import uvicorn
from app.cockpit import create_cockpit_app
from fastapi import FastAPI


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            requests.get(f"{base}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


def _cockpit(tmp_path: Path) -> FastAPI:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    return create_cockpit_app(projects_dir=projects, worker_base_url="http://127.0.0.1:9")


def test_cockpit_auth_blocks_unauthenticated_and_allows_valid_token(
    monkeypatch: object, tmp_path: Path
) -> None:

    monkeypatch.setenv("EDITVISTA_API_TOKEN", "test-secret-token")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_AUTH_MODE", "required")  # type: ignore[attr-defined]

    with _serve(_cockpit(tmp_path)) as base:
        # Health stays public for container probes.
        assert requests.get(f"{base}/api/health", timeout=3.0).status_code == 200

        protected = f"{base}/api/projects/no-such/summary"
        unauth = requests.get(protected, timeout=3.0)
        assert unauth.status_code == 401

        wrong = requests.get(protected, headers={"Authorization": "Bearer nope"}, timeout=3.0)
        assert wrong.status_code == 401

        right = requests.get(
            protected, headers={"Authorization": "Bearer test-secret-token"}, timeout=3.0
        )
        assert right.status_code != 401  # project lookup may 404, but auth passed

        api_key = requests.get(protected, headers={"X-API-Key": "test-secret-token"}, timeout=3.0)
        assert api_key.status_code != 401


def test_cockpit_login_sets_cookie_and_unlocks_browser_paths(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITVISTA_API_TOKEN", "test-secret-token")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_AUTH_MODE", "required")  # type: ignore[attr-defined]

    with _serve(_cockpit(tmp_path)) as base:
        bad = requests.post(f"{base}/api/login", json={"token": "wrong"}, timeout=3.0)
        assert bad.status_code == 401

        session = requests.Session()
        good = session.post(f"{base}/api/login", json={"token": "test-secret-token"}, timeout=3.0)
        assert good.status_code == 200
        assert "editvista_session" in session.cookies

        unlocked = session.get(f"{base}/api/projects/no-such/summary", timeout=3.0)
        assert unlocked.status_code != 401


def test_auth_disabled_by_default_keeps_local_preview_working(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.delenv("EDITVISTA_API_TOKEN", raising=False)  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_AUTH_MODE", "off")  # type: ignore[attr-defined]
    with _serve(_cockpit(tmp_path)) as base:
        assert requests.get(f"{base}/api/projects/no-such/summary", timeout=3.0).status_code != 401


def test_n8n_workflows_and_compose_forward_the_api_token() -> None:
    """Auth must not break the internal n8n → worker/cockpit automation path."""
    import json

    workflows = Path(__file__).parents[1] / "n8n"
    http_nodes = 0
    for path in sorted(workflows.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for node in data.get("nodes", []):
            if node.get("type") != "n8n-nodes-base.httpRequest":
                continue
            http_nodes += 1
            headers = (
                node.get("parameters", {})
                .get("options", {})
                .get("headers", {})
                .get("parameters", [])
            )
            assert any(
                entry.get("name") == "X-API-Key"
                and "$env.EDITVISTA_API_TOKEN" in str(entry.get("value", ""))
                for entry in headers
                if isinstance(entry, dict)
            ), f"{path.name}: HTTP node missing API token header"

    assert http_nodes >= 4
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "EDITVISTA_API_TOKEN=${EDITVISTA_API_TOKEN:-}" in compose


def test_worker_auth_blocks_unauthenticated_job_queries(monkeypatch: object) -> None:
    from engine.service import app as worker_app

    monkeypatch.setenv("EDITVISTA_API_TOKEN", "test-secret-token")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_AUTH_MODE", "required")  # type: ignore[attr-defined]

    # Worker app has /health, not /api/health — use a custom readiness path probe.
    port = _free_port()
    config = uvicorn.Config(app=worker_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            requests.get(f"{base}/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    try:
        assert requests.get(f"{base}/health", timeout=3.0).status_code == 200
        unauth = requests.get(f"{base}/jobs/abc", timeout=3.0)
        assert unauth.status_code == 401
        allowed = requests.get(
            f"{base}/jobs/abc", headers={"Authorization": "Bearer test-secret-token"}, timeout=3.0
        )
        assert allowed.status_code != 401
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)
