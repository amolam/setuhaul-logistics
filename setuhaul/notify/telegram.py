"""Telegram as a driver channel (roadmap Phase 5, backend foundation; test sink only).

One bot, many drivers. Identity is never what a chat claims: a Telegram account speaks for a driver only after it
redeemed a one-time, expiring link code that staff issued for that driver (migration 0035), and it is matched on
Telegram's numeric user id (`from.id`), never on a username or display name. Everything else here is transport:

  * webhook authenticity: Telegram echoes the secret given to setWebhook in `X-Telegram-Bot-Api-Secret-Token`;
    compared in constant time. No secret configured -> the route does not exist.
  * parsing: text, /start <code>, inline-button callbacks (offer accept / decline / alternative) and a shared location.
    Anything else is ignored. Callback data carries only an opaque offer id; the backend re-checks who may act on it.
  * idempotency: each update becomes a stored message keyed by ("TELEGRAM", "tg:<update_id>" or "tgcb:<callback id>"),
    so a redelivered webhook is deduplicated by the same unique key the WhatsApp path uses.
  * sending: `TelegramBotProvider` implements the relay's Provider contract for later use. It is NOT wired anywhere
    by default; the pilot stays on the internal test sink until the owner authorizes real sends and provides the bot
    token through Secrets Manager. The token is never logged.

Bot wording here is provisional: the conversation design awaits the owner's approval (roadmap Phase 8).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .outbound import ACCEPTED, AMBIGUOUS, PERMANENT, RETRYABLE, ProviderTimeout, SendResult

log = logging.getLogger("setuhaul.telegram")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"       # no 0/O, 1/I/L: read aloud or typed from a phone
CODE_LEN = 10                                            # ~49 bits; single use, expiring, hashed at rest
CALLBACK_RE = re.compile(r"^o:([0-9a-f-]{36}):([ADR])$")
ACTIONS = {"A": "ACCEPT", "D": "DECLINE", "R": "ALTERNATIVE"}

# Provisional texts (pending the Telegram conversation design approval)
TEXT_NOT_LINKED = ("This chat is not linked to a SetuHaul driver yet. Ask your dispatcher for a link code and send it "
                   "here as: /start YOURCODE")
TEXT_LINKED = "You are linked to SetuHaul. Booking offers and updates for your trips will arrive here."
TEXT_LINK_FAILED = "That code did not work. It may be wrong, used or expired. Ask your dispatcher for a new one."
TEXT_ACCOUNT_TAKEN = "This Telegram account is already linked. Ask your dispatcher if this is wrong."
TEXT_DRIVER_TAKEN = "Your driver profile is already linked to another Telegram account. Ask your dispatcher to reset it."
OFFER_REPLIES = {
    "SUBMITTED": "Thanks. Your slot is held and the request is with the warehouse planner. You will hear back here.",
    "ACCEPTED": "Thanks, your acceptance is recorded. A coordinator will confirm the next step.",
    "DECLINED": "Understood, you declined this offer. Your dispatcher has been told.",
    "ALTERNATIVE_REQUESTED": "Understood. Your dispatcher will send you another time.",
    "EXPIRED": "This offer has expired. Your dispatcher can send a new one.",
    "SUPERSEDED": "This offer was replaced by a newer one. Please use the latest offer.",
    "UNAVAILABLE": "This offer can no longer be booked. Your dispatcher will be in touch.",
    "SLOT_TAKEN": "Sorry, that slot was just taken. Your dispatcher will send another time.",
    "NOT_FOUND": "This button is not valid for your account.",
}
TEXT_LOCATION = {"VERIFIED": "Thanks, your location was received.",
                 "NEEDS_REVIEW": "Thanks, your location was received. A coordinator may check with you.",
                 "UNAVAILABLE": "No problem. Your delay report still stands.",
                 "INVALID": "That location could not be used (too old or imprecise). You can share it again."}


def secret_ok(header: str | None, expected: str | None) -> bool:
    return bool(expected) and header is not None and hmac.compare_digest(header.encode(), expected.encode())


def new_link_code() -> tuple[str, str]:
    """(code shown once to the dispatcher, sha256 hex stored by the database)."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return code, hash_code(code)


def normalize_code(raw: str) -> str | None:
    c = re.sub(r"[\s-]", "", raw or "").upper()
    return c if len(c) == CODE_LEN and all(ch in CODE_ALPHABET for ch in c) else None


def hash_code(code: str) -> str:
    return hashlib.sha256(("setuhaul-link:" + code).encode()).hexdigest()


def offer_buttons(offer_id: str) -> list[list[dict]]:
    """Inline keyboard for one offer (callback data <= 64 bytes, opaque id only)."""
    return [[{"text": "Accept", "callback_data": f"o:{offer_id}:A"},
             {"text": "Decline", "callback_data": f"o:{offer_id}:D"}],
            [{"text": "Another time", "callback_data": f"o:{offer_id}:R"}]]


@dataclass(frozen=True)
class TelegramEvent:
    kind: str                      # start | text | callback | location | ignored
    update_id: int | None
    user_id: str | None            # Telegram numeric user id (identity key once linked)
    chat_id: str | None
    external_id: str | None        # idempotency key of the stored message
    text: str | None = None
    code: str | None = None
    offer_id: str | None = None
    action: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    observed_at: datetime | None = None
    callback_query_id: str | None = None


def parse_update(u: dict) -> TelegramEvent:
    if not isinstance(u, dict) or not isinstance(u.get("update_id"), int):
        return TelegramEvent("ignored", None, None, None, None)
    uid = u["update_id"]
    cq = u.get("callback_query")
    if isinstance(cq, dict):
        frm, msg = cq.get("from") or {}, cq.get("message") or {}
        m = CALLBACK_RE.match(str(cq.get("data") or ""))
        if not (isinstance(frm.get("id"), int) and m):
            return TelegramEvent("ignored", uid, None, None, None)
        chat = (msg.get("chat") or {}).get("id")
        return TelegramEvent("callback", uid, str(frm["id"]), str(chat) if isinstance(chat, int) else str(frm["id"]),
                             f"tgcb:{cq.get('id') or uid}", offer_id=m.group(1), action=ACTIONS[m.group(2)],
                             callback_query_id=str(cq.get("id") or ""))
    msg = u.get("message")
    if not isinstance(msg, dict):
        return TelegramEvent("ignored", uid, None, None, None)
    frm, chat = msg.get("from") or {}, msg.get("chat") or {}
    if not isinstance(frm.get("id"), int) or frm.get("is_bot") or chat.get("type") != "private":
        return TelegramEvent("ignored", uid, None, None, None)      # groups and bots never speak for a driver
    user, chat_id, ext = str(frm["id"]), str(chat.get("id")), f"tg:{uid}"
    date = msg.get("date")
    when = datetime.fromtimestamp(date, timezone.utc) if isinstance(date, int) else None
    loc = msg.get("location")
    if isinstance(loc, dict) and "live_period" not in loc:          # one-off shares only: live tracking is refused
        return TelegramEvent("location", uid, user, chat_id, ext, latitude=loc.get("latitude"),
                             longitude=loc.get("longitude"), accuracy_m=loc.get("horizontal_accuracy"),
                             observed_at=when)
    text = msg.get("text")
    if not isinstance(text, str) or not text.strip():
        return TelegramEvent("ignored", uid, user, chat_id, None)
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        return TelegramEvent("start", uid, user, chat_id, ext, code=normalize_code(parts[1]) if len(parts) > 1 else None)
    return TelegramEvent("text", uid, user, chat_id, ext, text=text[:2000])


class TelegramGateway:
    """Bot replies to the webhook caller's chat (link results, button answers). Test-sink by default: one structured
    log line (chat id hashed, text hashed) and an in-memory record for tests; nothing leaves the process."""

    def __init__(self, provider=None):
        self.provider = provider
        self.sent: list[tuple[str, str]] = []

    def reply(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))
        if self.provider is None:
            log.info("TELEGRAM_TEST_SINK chat=%s text_sha256=%s len=%d",
                     hashlib.sha256(chat_id.encode()).hexdigest()[:12], hashlib.sha256(text.encode()).hexdigest()[:16],
                     len(text))
            return
        self.provider.send(chat_id, text, str(uuid.uuid4()))


class TelegramBotProvider:
    """Telegram Bot API sendMessage, in the relay's Provider contract. Not wired by default (see module docstring).
    Telegram has no idempotency key: a resend after an ambiguous outcome may show the driver one identical repeat."""

    supports_idempotency = False
    service_window = False

    def __init__(self, token: str, client=None, base: str = "https://api.telegram.org"):
        import httpx
        self._url = f"{base}/bot{token}/sendMessage"            # the token lives only in this URL, never in logs
        self.http = client or httpx.Client(timeout=10)

    def send(self, to: str, text: str, idempotency_key: str, buttons: list | None = None) -> SendResult:
        import httpx
        body = {"chat_id": to, "text": text}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        try:
            r = self.http.post(self._url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return SendResult(RETRYABLE, error=type(e).__name__)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(type(e).__name__) from e
        if r.status_code == 200:
            try:
                return SendResult(ACCEPTED, f"tg.{r.json()['result']['message_id']}")
            except (ValueError, KeyError, TypeError):
                return SendResult(AMBIGUOUS, error="unreadable 200")
        if r.status_code == 429:
            return SendResult(RETRYABLE, error="HTTP 429")
        if r.status_code >= 500:
            return SendResult(AMBIGUOUS, error=f"HTTP {r.status_code}")
        return SendResult(PERMANENT, error=f"HTTP {r.status_code}")        # 400 bad chat, 403 blocked by the user
