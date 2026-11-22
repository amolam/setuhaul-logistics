"""The hosted staff console, served by the API itself under /app/ (docs/release/11_STAFF_CONSOLE.md).

Same origin as the API (https://<api id>.execute-api.<region>.amazonaws.com/pilot/), so the page calls the API with
relative paths and no CORS is needed. Only a fixed allow-list of files is served, from this package's
`console_static/` folder (inside the verified Lambda zip); nothing else on disk is reachable. `config.json` is built
from public settings only (Cognito domain and public client id). Every response carries a strict content security
policy: scripts only from this origin, no inline script, no eval, no framing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

STATIC = Path(__file__).resolve().parent / "console_static"
ASSETS = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}   # the whole allow-list


def security_headers(cognito_domain: str) -> dict[str, str]:
    cognito = f"https://{cognito_domain}"
    csp = "; ".join(["default-src 'none'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
                     "img-src 'self' data:", f"connect-src 'self' {cognito}", f"form-action 'self' {cognito}",
                     "frame-ancestors 'none'", "base-uri 'none'"])
    return {"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Cross-Origin-Opener-Policy": "same-origin"}


def mount_console(app: FastAPI, cognito_domain: str | None, client_id: str | None) -> None:
    """Adds GET /app, /app/, /app/config.json, /app/assets/{app.js,app.css}. Without a Cognito domain the console
    is not served (404), so a local or test API without sign-in never shows a page that cannot work."""
    files = {name: (STATIC / "assets" / name).read_bytes() for name in ASSETS}
    version = {name: hashlib.sha256(b).hexdigest()[:12] for name, b in files.items()}
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    for name, v in version.items():                       # content-addressed asset URLs: safe to cache immutably
        index = index.replace(f'"assets/{name}"', f'"assets/{name}?v={v}"')
    headers = security_headers(cognito_domain or "invalid.example")

    def enabled():
        if not cognito_domain or not client_id:
            raise HTTPException(404, "not found")

    @app.get("/app", include_in_schema=False)
    def app_root():
        enabled()
        return RedirectResponse("app/", status_code=308)

    @app.get("/app/", include_in_schema=False)
    def app_index():
        enabled()
        return Response(index, media_type="text/html; charset=utf-8", headers={**headers, "Cache-Control": "no-cache"})

    @app.get("/app/config.json", include_in_schema=False)
    def app_config():
        enabled()
        body = json.dumps({"cognitoDomain": cognito_domain, "clientId": client_id, "apiBase": "../"})
        return Response(body, media_type="application/json", headers={**headers, "Cache-Control": "no-cache"})

    @app.get("/app/assets/{name}", include_in_schema=False)
    def app_asset(name: str, request: Request):
        enabled()
        if name not in ASSETS:                            # the allow-list: no paths, no other files
            raise HTTPException(404, "not found")
        immutable = request.query_params.get("v") == version[name]
        return Response(files[name], media_type=ASSETS[name], headers={
            **headers, "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})
