"""TDD tests: abuse rate limiting and opt-in TLS reverse proxy config."""

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
    server = uvicorn.Server(
        uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
    )
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


def test_intake_is_rate_limited(
    monkeypatch: object, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITVISTA_AUTH_MODE", "off")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_INTAKE_RATE_LIMIT", "2")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_RATE_LIMIT_PER_MINUTE", "100")  # type: ignore[attr-defined]

    with _serve(_cockpit(tmp_path)) as base:
        payload = {"url": "https://example.com/"}
        first = requests.post(f"{base}/api/intake", json=payload, timeout=3.0)
        second = requests.post(f"{base}/api/intake", json=payload, timeout=3.0)
        third = requests.post(f"{base}/api/intake", json=payload, timeout=3.0)
        # Worker at port 9 is unreachable -> 502 for allowed calls; 429 once capped.
        assert first.status_code == 502
        assert second.status_code == 502
        assert third.status_code == 429
        assert "Retry-After" in third.headers
        # Probes stay exempt.
        assert requests.get(f"{base}/api/health", timeout=3.0).status_code == 200


def test_rate_limit_disabled_when_zero(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("EDITVISTA_AUTH_MODE", "off")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_INTAKE_RATE_LIMIT", "0")  # type: ignore[attr-defined]
    monkeypatch.setenv("EDITVISTA_RATE_LIMIT_PER_MINUTE", "0")  # type: ignore[attr-defined]
    with _serve(_cockpit(tmp_path)) as base:
        for _ in range(4):
            assert requests.post(
                f"{base}/api/intake", json={"url": "https://example.com/"}, timeout=3.0
            ).status_code == 502


def test_tls_proxy_config_is_opt_in_and_hardened() -> None:
    repo = Path(__file__).parents[1]
    compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    caddyfile = (repo / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "profiles:" in compose and "- proxy" in compose
    assert "caddy:2" in compose
    assert '"80:80"' in compose and '"443:443"' in compose
    assert "{$EDITVISTA_DOMAIN}" in caddyfile
    assert "reverse_proxy cockpit:8888" in caddyfile
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Strict-Transport-Security",
    ):
        assert header in caddyfile
