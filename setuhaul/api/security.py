"""Who is calling, verified. Three mechanisms, one per kind of caller (decision: JWT for people, IAM for services).

* **Planners and operations (console):** Cognito **access** tokens, RS256, verified against the user pool JWKS.
  API Gateway's JWT authorizer verifies first; the app verifies again (defence in depth, and it is the only check
  when running locally). Roles come from `cognito:groups`; facility scope from the `facility_scope` claim added by
  the pre-token-generation trigger. Nothing in the request body can widen either.
* **Driver conversations (agent tools):** a short-lived **session token** minted by the inbound webhook after it
  verified Meta's signature and resolved the phone number to a driver. It binds driver, thread and facility. The
  agent orchestrator carries it opaquely; the LLM never sees it and cannot choose who it acts for. In AWS the
  orchestrator → tool API hop is additionally IAM-signed (SigV4) at API Gateway.
* **WhatsApp webhook:** Meta's `X-Hub-Signature-256` HMAC over the raw body.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field

import jwt

# 0037: `dispatcher` is its own role (Cognito group `dispatcher`); `operations` keeps its permissions for compatibility.
STAFF_ROLES = {"planner", "ops_lead", "operations", "dispatcher"}


class AuthError(Exception):
    """401: the caller could not be authenticated."""


@dataclass(frozen=True)
class Principal:
    kind: str                                   # "staff" | "driver"
    sub: str
    roles: frozenset = frozenset()
    facilities: tuple[str, ...] = ()
    driver_id: str | None = None
    thread_id: str | None = None
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.driver_id or self.sub}"


class CognitoVerifier:
    """Verifies Cognito access tokens. `jwks` is a static JWKS dict (tests, air-gapped) or None to fetch from the
    pool's well-known URL with caching (production)."""

    def __init__(self, issuer: str, client_ids: set[str], jwks: dict | None = None, leeway_s: int = 30):
        self.issuer, self.client_ids, self.leeway = issuer.rstrip("/"), set(client_ids), leeway_s
        self._static = {k["kid"]: jwt.PyJWK(k) for k in (jwks or {}).get("keys", [])}
        self._client = None if jwks else jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_keys=True)

    def _key(self, token: str):
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if self._static:
            if kid not in self._static:
                raise AuthError("unknown signing key")
            return self._static[kid].key
        return self._client.get_signing_key_from_jwt(token).key

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self._key(token), algorithms=["RS256"], issuer=self.issuer, leeway=self.leeway,
                                options={"require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
                                         "verify_aud": False})          # access tokens carry client_id, not aud
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from e
        if claims["token_use"] != "access":
            raise AuthError("an access token is required")
        if claims["client_id"] not in self.client_ids:
            raise AuthError("token issued to another app client")
        roles = frozenset(g for g in claims.get("cognito:groups", []) if g in STAFF_ROLES)
        if not roles:
            raise AuthError("no console role")
        facilities = tuple(sorted(f for f in str(claims.get("facility_scope", "")).split(",") if f))
        return Principal("staff", claims["sub"], roles, facilities, extra={"username": claims.get("username")})


class SessionTokens:
    """Driver session tokens: HS256 with a server-side secret (Secrets Manager / KMS in AWS), 15-minute life,
    renewed by every verified inbound message."""

    ISS, AUD = "setuhaul-ingest", "setuhaul-tools"

    def __init__(self, secret: bytes, ttl_s: int = 900):
        if len(secret) < 32:
            raise ValueError("session secret must be at least 32 bytes")
        self.secret, self.ttl = secret, ttl_s

    def mint(self, driver_id: str, thread_id: str, facility_id: str | None) -> str:
        now = int(time.time())
        return jwt.encode({"iss": self.ISS, "aud": self.AUD, "sub": driver_id, "thr": thread_id, "fac": facility_id,
                           "iat": now, "exp": now + self.ttl, "jti": uuid.uuid4().hex}, self.secret, algorithm="HS256")

    def verify(self, token: str) -> Principal:
        try:
            c = jwt.decode(token, self.secret, algorithms=["HS256"], audience=self.AUD, issuer=self.ISS,
                           options={"require": ["exp", "iat", "sub", "thr"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid session: {type(e).__name__}") from e
        return Principal("driver", c["sub"], frozenset({"driver"}), (c["fac"],) if c.get("fac") else (),
                         driver_id=c["sub"], thread_id=c["thr"])


def meta_signature_ok(raw_body: bytes, header: str | None, app_secret: bytes) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))
