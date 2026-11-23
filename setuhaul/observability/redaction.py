"""Phase G.1 (G1): the one redaction layer every trace passes through before it can leave the process.

`redact(value)` returns a copy that is safe to export to LangSmith (or any other trace sink):

  * keys that carry personal data, message text, credentials or session material are DROPPED (not masked), whatever
    the value: phone, driver name, message body, session token, Authorization, api_key, secret, password, ...;
  * strings are scrubbed of anything that looks like a phone number, an e-mail address, a JWT, a bearer token, an
    OpenRouter / LangSmith / OpenAI-style API key, an AWS access key id or secret key, a WhatsApp/Meta token, a
    12-digit AWS account id (also inside an ARN), and a synthetic driver phone (+999...);
  * strings are cut to MAX_STR characters, lists to MAX_ITEMS, nesting to MAX_DEPTH;
  * anything that is not JSON-like becomes its type name.

Driver message content is never exported by default. `content_allowed=True` (SETUHAUL_TRACE_CONTENT=on, synthetic data
only) lets a message's text through, after the same scrub. tests/test_phase_g1_tracing.py pins every rule.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

MAX_STR, MAX_ITEMS, MAX_DEPTH = 300, 25, 6
REDACTED = "[REDACTED]"

# Keys whose values never leave the process (compared lower-case, '-' as '_').
DROP_KEYS = frozenset({
    "phone", "phone_e164", "driver_phone", "msisdn", "wa_id", "from", "to", "recipient", "recipient_ref",
    "driver_name", "name", "email", "address",
    "session", "session_token", "x_setuhaul_session", "token", "access_token", "id_token", "refresh_token",
    "authorization", "cookie", "set_cookie", "api_key", "apikey", "x_api_key", "secret", "secret_string",
    "client_secret", "password", "private_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
    "meta_app_secret", "session_secret", "whatsapp_token", "openrouter_api_key", "langsmith_api_key",
    "signature", "x_hub_signature_256", "user_block", "prompt", "messages", "system",
})
# Keys that carry a driver's or a person's free text: dropped unless content export is on.
CONTENT_KEYS = frozenset({"text", "body", "body_masked", "message", "driver_message", "say", "reply", "note",
                          "summary", "reason", "driver_last_message", "what_driver_was_told", "content"})

_PATTERNS = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-or-[A-Za-z0-9_-]{8,}"), "[API_KEY]"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"), "[API_KEY]"),
    (re.compile(r"\blsv2_[A-Za-z0-9_]{8,}"), "[API_KEY]"),
    (re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b"), "[AWS_KEY_ID]"),
    (re.compile(r"(?i)(aws_secret_access_key|secret[_-]?key)\s*[=:]\s*[A-Za-z0-9/+=]{20,}"), r"\1=[REDACTED]"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), "[META_TOKEN]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:)\d{12}(:)"), r"\1************\2"),
    # phones (before bare 12-digit ids, so +91XXXXXXXXXX reads as a phone): +<country> numbers, the synthetic +999
    # range, and Indian 10-digit mobiles (6-9 first digit)
    (re.compile(r"\+\d[\d\s().-]{7,}\d"), "[PHONE]"),
    (re.compile(r"(?<![\w-])(?:91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w-])"), "[PHONE]"),
    (re.compile(r"(?<![0-9A-Fa-f-])\d{12}(?![0-9A-Fa-f-])"), "************"),
]


def scrub(s: str) -> str:
    """Remove credential-, phone-, e-mail- and account-id-shaped substrings from one string, then cut it."""
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s if len(s) <= MAX_STR else s[:MAX_STR] + "…"


def _norm(k) -> str:
    return str(k).strip().lower().replace("-", "_")


def redact(value, *, content_allowed: bool = False, _depth: int = 0):
    if _depth > MAX_DEPTH:
        return "[DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:50]:
            nk = _norm(k)
            if nk in DROP_KEYS or "secret" in nk or "password" in nk or nk.endswith("_token") or nk.endswith("_key"):
                continue
            if nk in CONTENT_KEYS and not content_allowed:
                out[str(k)] = REDACTED
                continue
            out[str(k)] = redact(v, content_allowed=content_allowed, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [redact(v, content_allowed=content_allowed, _depth=_depth + 1) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"[+{len(items) - MAX_ITEMS} more]")
        return out
    return type(value).__name__


def find_leaks(obj) -> list[str]:
    """What a redacted object still contains that should not leave (tests and the export guard use it)."""
    import json
    text = json.dumps(obj, default=str)
    leaks = []
    for rx, _ in _PATTERNS:
        if rx.search(text):
            leaks.append(rx.pattern[:40])
    return leaks
