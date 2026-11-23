"""Read-only security checks against the live hosted console and API (docs/release/12_CONSOLE_PILOT_REPORT.md).

    python scripts/live_security_check.py > docs/release/evidence/security_live.json

GET/OPTIONS requests only, no credentials. Header names are matched case-insensitively (API Gateway lower-cases
them; the first capture on 22 Nov read them case-sensitively and recorded null)."""
import base64, json, re, sys, time, urllib.request, urllib.error
B = "https://w8ptr7w960.execute-api.ap-southeast-2.amazonaws.com/pilot"
def req(path, headers=None, method="GET"):
    r = urllib.request.Request(B + path, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as x: return x.status, {k.lower(): v for k, v in x.headers.items()}, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e: return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")
def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
now = int(time.time())
iss = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_CAHnTgNpz"
claims = {"iss": iss, "token_use": "access", "client_id": "x", "exp": now + 600, "iat": now,
          "cognito:groups": ["ops_lead"], "facility_scope": "FAC-JAI-01 FAC-GGN-01 FAC-OTHER-99", "sub": "forged"}
forged = {
  "alg_none": b64({"alg": "none", "typ": "JWT"}) + "." + b64(claims) + ".",
  "bad_signature_rs256": b64({"alg": "RS256", "kid": "forged"}) + "." + b64(claims) + "." + b64({"s": 1}),
  "hs256_any_key": b64({"alg": "HS256"}) + "." + b64(claims) + ".c2lnbmF0dXJl",
  "garbage": "not.a.jwt",
}
out = {}
for p in ("/console/threads", "/console/threads/anything", "/console/requests", "/console/escalations"):
    s, h, _ = req(p); out[f"no_token {p}"] = (s, h.get("cache-control"))
for k, t in forged.items():
    s, h, body = req("/console/threads", {"Authorization": "Bearer " + t}); out[f"forged {k}"] = (s, body[:80])
s, h, _ = req("/console/threads", {"Origin": "https://evil.example"}); out["cors on GET"] = h.get("access-control-allow-origin")
s, h, _ = req("/console/threads", {"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}, "OPTIONS"); out["cors preflight"] = (s, h.get("access-control-allow-origin"))
for p in ("/app/assets/../console_site.py", "/app/assets/%2e%2e%2fapp.py", "/app/.env", "/app/index.html", "/demo/sign"):
    s, _, body = req(p); out[f"path {p}"] = (s, "def " in body or "import " in body)
s, h, html = req("/app/")
out["page headers"] = {k: h.get(k.lower()) for k in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Strict-Transport-Security", "Cache-Control")}
out["inline script in html"] = bool(re.search(r"<script(?![^>]*\bsrc=)", html)) or "onclick" in html
leaks = {}
for a in re.findall(r'(?:src|href)="(assets/[^"]+)"', html):
    s, h, body = req("/app/" + a)
    leaks[a] = {"status": s, "cache": h.get("cache-control"),
                "hits": [w for w in ("client_secret", "META_APP_SECRET", "SESSION_SECRET", "AKIA", "aws_secret", "OPENROUTER", "sk-or-", "BEGIN PRIVATE", "hmac", "localStorage", "/demo/", "password") if w.lower() in body.lower()]}
out["live asset scan"] = leaks
s, _, cfg = req("/app/config.json"); out["config.json keys"] = sorted(json.loads(cfg))
out["captured_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
print(json.dumps(out, indent=1))
