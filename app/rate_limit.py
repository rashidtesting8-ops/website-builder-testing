"""Bounded in-process rate limiting for cockpit and worker APIs (production P0).

Protects expensive endpoints (intake / pipeline runs) from abuse. Limits are
read from the environment on every request so operators can tune them without
rebuilding. Health/docs probes are always exempt.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

_WINDOW_SECONDS = 60.0
_LOCK = threading.Lock()
_BUCKETS: dict[tuple[str, str, str], deque[float]] = {}

EXEMPT_PATHS = frozenset(
    {
        "/health", "/api/health", "/docs", "/redoc", "/openapi.json",
        "/api/docs", "/login", "/api/login",
    }
)
HEAVY_PATHS = frozenset({"/api/intake", "/pipeline/run"})


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _limit_for(path: str) -> int:
    if path in HEAVY_PATHS:
        raw = os.environ.get("EDITVISTA_INTAKE_RATE_LIMIT", "10")
    else:
        raw = os.environ.get("EDITVISTA_RATE_LIMIT_PER_MINUTE", "120")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def install_rate_limit(app: FastAPI, *, extra_exempt: tuple[str, ...] = ()) -> None:
    """Attach a sliding-window limiter keyed by client IP + endpoint class."""
    exempt = set(EXEMPT_PATHS) | set(extra_exempt)
    namespace = uuid.uuid4().hex[:8]

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next: object) -> Response:
        path = request.url.path
        limit = _limit_for(path)
        if limit <= 0 or path in exempt:
            return await call_next(request)  # type: ignore[no-any-return]

        bucket_class = "heavy" if path in HEAVY_PATHS else "api"
        # Per-app namespace isolates service instances and ephemeral test servers.
        key = (namespace, _client_key(request), bucket_class)
        now = time.monotonic()
        with _LOCK:
            hits = _BUCKETS.setdefault(key, deque())
            while hits and now - hits[0] > _WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(_WINDOW_SECONDS - (now - hits[0])))
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)
        return await call_next(request)  # type: ignore[no-any-return]
