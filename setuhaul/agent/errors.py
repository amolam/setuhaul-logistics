"""Phase G.1 (G3): the structured model-failure taxonomy.

Every model failure the agent can see is one of sixteen categories. The category is what the ledger, the turn metrics,
the console and the traces record; a short `detail` (the old fine-grained reason: `credits`, `connect_timeout`,
`provider_mismatch`, ...) goes to logs and traces only. Nothing here ever holds a key, a secret value, a prompt or a
driver's words.

Outcome classes (what the console reconciles, docs/phase-g1/OPENROUTER_PILOT_DIAGNOSIS.md §5):
  NOT_STARTED       no request left the process because the client could not be built or the call was refused
                    before sending for a configuration reason (secret, budget configuration)
  BUDGET_REFUSAL    the daily budget refused the reservation: no request was sent
  PROVIDER_FAILURE  a request was attempted and the provider, the network or the answer failed
"""
from __future__ import annotations

import re
import socket
import ssl

MISSING_SECRET = "MISSING_SECRET"
EMPTY_SECRET = "EMPTY_SECRET"
SECRET_ACCESS_DENIED = "SECRET_ACCESS_DENIED"
SECRET_RETRIEVAL_ERROR = "SECRET_RETRIEVAL_ERROR"
NETWORK_ERROR = "NETWORK_ERROR"
DNS_ERROR = "DNS_ERROR"
TLS_ERROR = "TLS_ERROR"
PROVIDER_AUTH_ERROR = "PROVIDER_AUTH_ERROR"
PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
INVALID_REQUEST = "INVALID_REQUEST"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
BUDGET_CONFIGURATION_ERROR = "BUDGET_CONFIGURATION_ERROR"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

CATEGORIES = (MISSING_SECRET, EMPTY_SECRET, SECRET_ACCESS_DENIED, SECRET_RETRIEVAL_ERROR, NETWORK_ERROR, DNS_ERROR,
              TLS_ERROR, PROVIDER_AUTH_ERROR, PROVIDER_RATE_LIMIT, PROVIDER_SERVER_ERROR, MODEL_NOT_FOUND,
              REQUEST_TIMEOUT, INVALID_REQUEST, BUDGET_EXCEEDED, BUDGET_CONFIGURATION_ERROR, UNKNOWN_PROVIDER_ERROR)

SECRET_CATEGORIES = frozenset({MISSING_SECRET, EMPTY_SECRET, SECRET_ACCESS_DENIED, SECRET_RETRIEVAL_ERROR})
NOT_STARTED = "NOT_STARTED"
BUDGET_REFUSAL = "BUDGET_REFUSAL"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
OUTCOME_CLASS = {**{c: NOT_STARTED for c in SECRET_CATEGORIES}, BUDGET_CONFIGURATION_ERROR: NOT_STARTED,
                 BUDGET_EXCEEDED: BUDGET_REFUSAL,
                 **{c: PROVIDER_FAILURE for c in (NETWORK_ERROR, DNS_ERROR, TLS_ERROR, PROVIDER_AUTH_ERROR,
                                                  PROVIDER_RATE_LIMIT, PROVIDER_SERVER_ERROR, MODEL_NOT_FOUND,
                                                  REQUEST_TIMEOUT, INVALID_REQUEST, UNKNOWN_PROVIDER_ERROR)}}

# The fine-grained names used before G.1 (ledger rows written by v0.3.0 and earlier, old tests, old logs).
LEGACY = {
    "auth": PROVIDER_AUTH_ERROR, "credits": PROVIDER_AUTH_ERROR, "rate_limited": PROVIDER_RATE_LIMIT,
    "server_error": PROVIDER_SERVER_ERROR, "provider_unavailable": PROVIDER_SERVER_ERROR,
    "upstream_timeout": REQUEST_TIMEOUT, "read_timeout": REQUEST_TIMEOUT, "timeout": REQUEST_TIMEOUT,
    "connect_error": NETWORK_ERROR, "connect_timeout": NETWORK_ERROR, "transport_error": NETWORK_ERROR,
    "bad_request": INVALID_REQUEST, "config": INVALID_REQUEST, "malformed": UNKNOWN_PROVIDER_ERROR,
    "provider_mismatch": UNKNOWN_PROVIDER_ERROR, "invalid_schema": UNKNOWN_PROVIDER_ERROR,
    "model_error": UNKNOWN_PROVIDER_ERROR, "budget_exhausted": BUDGET_EXCEEDED, "meter_error": BUDGET_CONFIGURATION_ERROR,
    "duplicate": BUDGET_CONFIGURATION_ERROR,
}


def normalize(category: str | None) -> str:
    """Any category, old or new, as one of the sixteen (unknown names are UNKNOWN_PROVIDER_ERROR)."""
    if category in CATEGORIES:
        return category
    return LEGACY.get((category or "").strip().lower(), UNKNOWN_PROVIDER_ERROR)


def outcome_class(category: str | None) -> str:
    return OUTCOME_CLASS[normalize(category)]


# ---------------------------------------------------------------- HTTP answers

_CREDIT_LIMIT_403 = re.compile(r"\b(limit|budget|credits?)\b|\$\s*\d", re.I)   # R4: unchanged wording rule
_MODEL_WORDS = ("not a valid model", "model not found", "no endpoints found", "does not exist", "unknown model",
                "invalid model")


def classify_http(status: int, message: str = "") -> tuple[str, str]:
    """(category, detail) for an HTTP answer that is not a usable 200. `message` is the provider's error message (it
    never contains our key)."""
    m = (message or "").lower()
    if status == 429:
        return PROVIDER_RATE_LIMIT, "rate_limited"
    if status == 402:
        return PROVIDER_AUTH_ERROR, "credits"
    if status == 403 and _CREDIT_LIMIT_403.search(message or ""):
        return PROVIDER_AUTH_ERROR, "credits"          # R4: a spent key/workspace/guardrail limit
    if status in (401, 403):
        return PROVIDER_AUTH_ERROR, "auth"
    if status == 404 or (status == 400 and any(w in m for w in _MODEL_WORDS)):
        return MODEL_NOT_FOUND, "model_not_found"
    if status == 503:
        return PROVIDER_SERVER_ERROR, "provider_unavailable"   # no endpoint meets the pinned/ZDR rules: no re-route
    if status in (500, 502):
        return PROVIDER_SERVER_ERROR, "server_error"
    if status in (408, 504):
        return REQUEST_TIMEOUT, "upstream_timeout"
    if 400 <= status < 500:
        return INVALID_REQUEST, "bad_request"
    return UNKNOWN_PROVIDER_ERROR, f"http_{status}"


# ---------------------------------------------------------------- transport failures
def _chain(e: BaseException):
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        yield e
        e = e.__cause__ or e.__context__


def classify_transport(e: BaseException) -> tuple[str, str, bool, bool]:
    """(category, detail, sent, retry) for an httpx exception. `sent` is False only when the request provably never
    left the process (it is then neither billed nor counted against the budget); only those are retried, once."""
    import httpx
    text = " ".join(str(x) for x in _chain(e)).lower()
    dns = any(isinstance(x, socket.gaierror) for x in _chain(e)) or any(
        s in text for s in ("name or service not known", "nodename nor servname", "getaddrinfo", "temporary failure in name resolution"))
    tls = any(isinstance(x, ssl.SSLError) for x in _chain(e)) or any(
        s in text for s in ("certificate verify failed", "[ssl", "sslerror", "tlsv1", "ssl handshake"))
    if isinstance(e, httpx.ConnectTimeout):
        return NETWORK_ERROR, "connect_timeout", False, True
    if isinstance(e, (httpx.ConnectError, httpx.PoolTimeout)):
        if dns:
            return DNS_ERROR, "dns", False, True
        if tls:
            return TLS_ERROR, "tls", False, False       # a certificate problem does not fix itself in a second
        return NETWORK_ERROR, "connect_error", False, True
    if isinstance(e, httpx.TimeoutException):
        return REQUEST_TIMEOUT, "read_timeout", True, False   # may have been processed and billed: never retried
    return NETWORK_ERROR, "transport_error", True, False      # ambiguous once the request was written


# ---------------------------------------------------------------- Secrets Manager
def classify_secret_error(e: BaseException) -> str:
    """The category of a failed GetSecretValue (botocore errors are matched by code, so botocore is not imported)."""
    code = ""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")
    name = type(e).__name__
    if code == "ResourceNotFoundException" or name == "ResourceNotFoundException":
        return MISSING_SECRET
    if code in ("AccessDeniedException", "AccessDenied", "KMSAccessDeniedException", "DecryptionFailure",
                "DecryptionFailureException", "UnrecognizedClientException") or "AccessDenied" in name:
        return SECRET_ACCESS_DENIED
    return SECRET_RETRIEVAL_ERROR


def key_shape(key: str) -> str:
    """A non-reversible description of a credential's shape for diagnosis: never the value, its length or a prefix
    beyond the provider's public, documented key prefix."""
    if not key:
        return "empty"
    if key.startswith("sk-or-"):
        return "openrouter_format"
    if key.startswith("lsv2_"):
        return "langsmith_format"
    return "unrecognized_format"
