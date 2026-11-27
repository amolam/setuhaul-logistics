"""Phase G.1 (G1): the one redaction layer every trace passes through before it can leave the process.

`redact(value)` returns a copy that is safe to export to LangSmith (or any other trace sink):

  * keys that carry personal data, message text, credentials or session material are DROPPED (not masked), whatever
    the value: phone, driver name, message body, session token, Authorization, api_key, secret, password, ...;
  * strings are scrubbed of anything that looks like a phone number, an e-mail address, a JWT, a bearer token, an
    OpenRouter / LangSmith / OpenAI-style API key, an AWS access key id or secret key, a WhatsApp/Meta token, a
    12-digit AWS account id (also inside an ARN), and a synthetic driver phone (+999...);
  * strings are cut to MAX_STR characters, lists to MAX_ITEMS, nesting to MAX_DEPTH;
  * anything that is not JSON-like becomes its type name.

Internal ids (driver_id, shipment_id, facility_id; driver traceability) pass a strict format gate
(observability/identifiers.py): a well-formed id is exported as is, anything else as "[INVALID_ID]".

Driver message content is never exported by default. `content_allowed=True` (SETUHAUL_TRACE_CONTENT=on, synthetic data
only) lets a message's text through, after the same scrub. tests/test_phase_g1_tracing.py pins every rule.

27 Nov 2026 (fix plan issue 8, owner decision Q3): `mode="full"` (SETUHAUL_TRACE_CONTENT=full, honoured ONLY for the
synthetic pilot: langsmith.tracer_from_env) also lets the model's per-turn input and raw output, tool results and long
text through (up to MAX_STR_FULL), so a turn can be debugged from its trace. It never lets through what `off` and `on`
remove as credentials or driver identity: the DROP_KEYS below, every key that looks like a secret, token or key, and
the scrub patterns. On top, a turn may register the driver's own identifiers (`pii_terms`: name, chat id, user id,
phone, as the inbound path knows them); every occurrence inside free text is masked, in every mode. The fixed system
instructions are never exported; the trace names their version (prompt_version). tests/test_trace_content_full.py
proves the rules on synthetic examples.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from .identifiers import KEY_KIND, export_id

MAX_STR, MAX_ITEMS, MAX_DEPTH = 300, 25, 6
MAX_STR_FULL, MAX_ITEMS_FULL, MAX_DEPTH_FULL = 4000, 60, 8          # full mode: a few KB per value (Q3)
REDACTED = "[REDACTED]"
MODES = ("off", "on", "full")

# Keys whose values never leave the process (compared lower-case, '-' as '_').
DROP_KEYS = frozenset({
    "phone", "phone_e164", "driver_phone", "msisdn", "wa_id", "from", "to", "recipient", "recipient_ref",
    "driver_name", "name", "email", "address",
    "chat_id", "telegram_chat_id", "telegram_user_id", "username", "lat", "lon", "latitude", "longitude", "geohash",
    "first_name", "last_name", "full_name", "display_name", "contact", "vcard", "user_id", "external_user_id",
    "chat_ref", "cognito_sub", "ip", "source_ip", "user_agent",
    "session", "session_token", "x_setuhaul_session", "token", "access_token", "id_token", "refresh_token",
    "authorization", "cookie", "set_cookie", "api_key", "apikey", "x_api_key", "secret", "secret_string",
    "client_secret", "password", "private_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
    "meta_app_secret", "session_secret", "whatsapp_token", "openrouter_api_key", "langsmith_api_key",
    "signature", "x_hub_signature_256", "user_block", "prompt", "messages", "system",
})
# Model inputs: exported only in full mode (after the scrub); "system" (the fixed instructions) never, by version only.
MODEL_INPUT_KEYS = frozenset({"user_block", "prompt", "messages", "model_input", "raw_output"})
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
# 27 Nov 2026 (owner, DEMO-04): FULL mode (synthetic pilot only) removes only the driver's name and phone and every
# credential; everything else is exported. So full mode scrubs credential and phone shapes only (no e-mail, AWS
# account-id or ARN masking) and drops only the keys below.
_CREDENTIAL_PATTERNS = _PATTERNS[:8]
_PHONE_PATTERNS = _PATTERNS[10:12]
_FULL_PATTERNS = _CREDENTIAL_PATTERNS + _PHONE_PATTERNS
FULL_DROP_KEYS = frozenset({
    "phone", "phone_e164", "driver_phone", "msisdn", "wa_id", "driver_name", "name", "first_name", "last_name",
    "full_name", "display_name",
    "session", "session_token", "x_setuhaul_session", "token", "access_token", "id_token", "refresh_token",
    "authorization", "cookie", "set_cookie", "api_key", "apikey", "x_api_key", "secret", "secret_string",
    "client_secret", "password", "private_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
    "meta_app_secret", "session_secret", "whatsapp_token", "openrouter_api_key", "langsmith_api_key",
    "signature", "x_hub_signature_256", "system",
})
# full mode: platform ids exported as they are. A 10-digit Telegram id has a mobile number's shape, so the phone
# scrub would otherwise mask it; by key it is known to be an id, not a phone.
FULL_ID_KEYS = frozenset({"chat_id", "telegram_chat_id", "telegram_user_id", "user_id", "external_user_id",
                          "message_id", "update_id", "external_message_id"})


def scrub(s: str, max_len: int = MAX_STR, pii_terms=(), patterns=None) -> str:
    """Remove credential-, phone-, e-mail- and account-id-shaped substrings from one string (full mode: credentials and
    phones only), mask the turn's known driver identifiers (`pii_terms`, longest first, case-insensitive), cut it."""
    for term in sorted({str(t).strip() for t in pii_terms if t and len(str(t).strip()) >= 3}, key=len, reverse=True):
        s = re.sub(r"(?i)(?<![\w])" + re.escape(term) + r"(?![\w])", "[PII]", s)
    for rx, rep in (_PATTERNS if patterns is None else patterns):
        s = rx.sub(rep, s)
    return s if len(s) <= max_len else s[:max_len] + "…"


def _norm(k) -> str:
    return str(k).strip().lower().replace("-", "_")


def redact(value, *, content_allowed: bool = False, mode: str | None = None, pii_terms=(), _depth: int = 0):
    """`mode` off | on | full (None: "on" if content_allowed else "off"). See the module notes."""
    mode = mode if mode in MODES else ("on" if content_allowed else "off")
    full = mode == "full"
    content_allowed = mode in ("on", "full")
    max_depth, max_items, max_len = (MAX_DEPTH_FULL, MAX_ITEMS_FULL, MAX_STR_FULL) if full else (MAX_DEPTH, MAX_ITEMS, MAX_STR)
    kw = {"mode": mode, "pii_terms": pii_terms}
    if _depth > max_depth:
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
        return scrub(value, max_len, pii_terms, _FULL_PATTERNS if full else None)
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:(80 if full else 50)]:
            nk = _norm(k)
            if nk in MODEL_INPUT_KEYS:
                if full:                                 # the model's per-turn input / raw output, scrubbed
                    out[str(k)] = redact(v, **kw, _depth=_depth + 1)
                continue                                 # off / on: never exported
            if nk in (FULL_DROP_KEYS if full else DROP_KEYS) or "secret" in nk or "password" in nk or nk.endswith("_token") or nk.endswith("_key") \
                    or nk.startswith("token") or "credential" in nk or "api_key" in nk:
                continue
            if full and nk in FULL_ID_KEYS and (isinstance(v, int) or (isinstance(v, str) and len(v) <= 64)):
                out[str(k)] = v                     # 27 Nov 2026 (DEMO-04): an id, exported as it is
                continue
            if nk in KEY_KIND:                     # driver traceability: the id itself or [INVALID_ID], never a guess
                out[str(k)] = export_id(KEY_KIND[nk], v)
                continue
            if nk in CONTENT_KEYS and not content_allowed:
                out[str(k)] = REDACTED
                continue
            out[str(k)] = redact(v, **kw, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [redact(v, **kw, _depth=_depth + 1) for v in items[:max_items]]
        if len(items) > max_items:
            out.append(f"[+{len(items) - max_items} more]")
        return out
    return type(value).__name__


def _without_ids(obj):
    """The object without the values of id keys (driver_id, shipment_id, chat_id, ...): those are exported as ids by
    design and must never trip the guard (27 Nov 2026: 'SYN-SHP-TEST-1' matched the registered term 'test'; a 10-digit
    Telegram chat id matched the phone shape)."""
    if isinstance(obj, dict):
        return {k: (None if _norm(k) in KEY_KIND or _norm(k) in FULL_ID_KEYS else _without_ids(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_without_ids(v) for v in obj]
    return obj


def find_leaks(obj, pii_terms=(), mode: str | None = None) -> list[str]:
    """What a redacted object still contains that should not leave (tests and the export guard use it). In full mode
    it checks what full mode removes (credentials, phones, the turn's registered name and phone); internal-id values
    are never scanned."""
    import json
    text = json.dumps(_without_ids(obj), default=str, ensure_ascii=False)
    leaks = []
    for rx, _ in (_FULL_PATTERNS if mode == "full" else _PATTERNS):
        if rx.search(text):
            leaks.append(rx.pattern[:40])
    for term in {str(t).strip() for t in pii_terms if t and len(str(t).strip()) >= 3}:
        if re.search(r"(?i)(?<![\w])" + re.escape(term) + r"(?![\w])", text):
            leaks.append("pii_term")
    return leaks
