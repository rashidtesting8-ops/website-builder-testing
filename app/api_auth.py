"""Shared API authentication for cockpit and worker (production P0 hardening).

Auth is opt-in for local development but strict in production:
- ``EDITVISTA_API_TOKEN`` — shared bearer token (secret, env only).
- ``EDITVISTA_AUTH_MODE`` — ``auto`` (default: require when token is set),
  ``required`` (fail closed if token missing), ``off`` (explicit local dev only).
- ``EDITVISTA_COOKIE_SECURE`` — set to ``1`` behind HTTPS to mark the login cookie Secure.

Accepted credentials: ``Authorization: Bearer <token>``, ``X-API-Key: <token>``,
or the HttpOnly cookie set by ``POST /api/login`` (for browser preview pages).
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "editvista_session"
DEFAULT_PUBLIC_PATHS = frozenset(
    {
        "/health", "/api/health", "/docs", "/redoc", "/openapi.json",
        "/api/docs", "/login", "/api/login",
    }
)


def configured_token() -> str:
    return os.environ.get("EDITVISTA_API_TOKEN", "").strip()


def auth_mode() -> str:
    return os.environ.get("EDITVISTA_AUTH_MODE", "auto").strip().lower()


def is_auth_enabled() -> bool:
    mode = auth_mode()
    if mode == "off":
        return False
    if mode == "required" and not configured_token():
        raise RuntimeError(
            "EDITVISTA_AUTH_MODE=required needs EDITVISTA_API_TOKEN in the environment"
        )
    return bool(configured_token())


def supplied_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return api_key
    return str(request.cookies.get(COOKIE_NAME, ""))


def verify_token(token: str) -> bool:
    expected = configured_token()
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


def install_auth(app: FastAPI, *, extra_public: tuple[str, ...] = ()) -> None:
    """Install an auth middleware; health/docs stay public, everything else is gated."""
    public = set(DEFAULT_PUBLIC_PATHS) | set(extra_public)

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next: object) -> Response:
        path = request.url.path
        if not is_auth_enabled() or path in public:
            return await call_next(request)  # type: ignore[no-any-return]
        token = supplied_token(request)
        if not verify_token(token):
            accepts_html = "text/html" in request.headers.get("accept", "")
            if accepts_html and not path.startswith("/api/"):
                # Browser navigation -> interactive login page. API clients get 401.
                return RedirectResponse(url="/login", status_code=302)
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)  # type: ignore[no-any-return]


def set_login_cookie(response: Response) -> None:
    """Attach the session cookie after a successful interactive login."""
    if not is_auth_enabled():
        return
    secure = os.environ.get("EDITVISTA_COOKIE_SECURE", "0").strip() == "1"
    response.set_cookie(
        COOKIE_NAME,
        configured_token(),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width, initial-scale=1"><title>EditVista sign in</title>
<style>body{font-family:system-ui,sans-serif;max-width:420px;margin:12vh auto;padding:1rem}
input,button{font:inherit;padding:.7rem;width:100%;box-sizing:border-box;margin-top:.7rem}
button{background:#0f1e36;color:#fff;border:0;border-radius:8px;cursor:pointer}
p{color:#b91c1c}</style></head><body>
<h1>EditVista Cockpit</h1>
<form id="f"><label for="t">API token</label>
<input id="t" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form><p id="e"></p>
<script>document.getElementById('f').addEventListener('submit',async function(ev){
ev.preventDefault();const t=document.getElementById('t').value;
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({token:t})});
if(r.ok){location.href='/'}else{document.getElementById('e').textContent='Invalid token'}});
</script></body></html>
"""


def login_page_response() -> HTMLResponse:
    return HTMLResponse(content=LOGIN_PAGE)
