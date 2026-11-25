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
    "REASSIGNED": ("Thanks. The trip is now assigned to you, with its existing appointment time. Your dispatcher and the "
                   "warehouse have been told."),
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


TEXT_TOO_MANY = "Too many attempts. Please wait 15 minutes and try again, or ask your dispatcher for help."
TEXT_AMBIGUOUS = ("I'm not sure whether that is a yes. Please tap Accept to take the offered slot, or Another time if "
                  "you need a different one.")
TEXT_AMBIGUOUS_REASSIGN = ("I'm not sure whether that is a yes. Please tap \"Accept trip and time\" to take this trip "
                           "at the time shown, or Decline.")
NOT_NOW = "Not now"
SHARE_LOCATION = "Share location once"


def offer_buttons(offer_id: str, kind: str = "INITIAL_BOOKING") -> list[list[dict]]:
    """Inline keyboard for one offer (callback data <= 64 bytes, opaque id only). A reassignment of a confirmed
    appointment asks the driver to accept the trip AND its existing time; it has no "another time"."""
    if kind == "CONFIRMED_REASSIGNMENT":
        return [[{"text": "Accept trip and time", "callback_data": f"o:{offer_id}:A"},
                 {"text": "Decline", "callback_data": f"o:{offer_id}:D"}]]
    return [[{"text": "Accept", "callback_data": f"o:{offer_id}:A"},
             {"text": "Decline", "callback_data": f"o:{offer_id}:D"}],
            [{"text": "Another time", "callback_data": f"o:{offer_id}:R"}]]


def location_keyboard() -> dict:
    """Telegram's native location request is a REPLY keyboard button (request_location), not an inline callback:
    nothing is read until the driver taps it and Telegram asks them to confirm. One tap, one fix; never live."""
    return {"keyboard": [[{"text": SHARE_LOCATION, "request_location": True}], [{"text": NOT_NOW}]],
            "one_time_keyboard": True, "resize_keyboard": True}


def reply_markup_for(route: dict) -> dict | None:
    if route.get("offer_id"):
        return {"inline_keyboard": offer_buttons(str(route["offer_id"]), route.get("offer_kind") or "INITIAL_BOOKING")}
    if route.get("ask_location"):
        return location_keyboard()
    return None


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
        if msg and (msg.get("chat") or {}).get("type", "private") != "private":
            return TelegramEvent("ignored", uid, None, None, None)      # buttons only count in the driver's private chat
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


def chat_tag(chat_id) -> str:
    """A short, non-reversible tag of a chat id (the raw id is never written to notifications)."""
    return hashlib.sha256(f"setuhaul-tgchat:{chat_id}".encode()).hexdigest()[:12]


def provider_ref(chat_id, message_id: int) -> str:
    """notifications.provider_message_id for a Telegram send. Telegram message ids count per chat, so the chat tag
    keeps (channel, provider_message_id) unique across drivers."""
    return f"tg.{chat_tag(chat_id)}.{int(message_id)}"


def message_id_for_chat(ref: str | None, chat_id) -> int | None:
    """The Telegram message id in `ref` if it was sent to THIS chat; otherwise None (another account, old format)."""
    m = re.fullmatch(r"tg\.([0-9a-f]{12})\.([0-9]{1,12})", ref or "")
    if not m or m.group(1) != chat_tag(chat_id):
        return None
    return int(m.group(2))


def _sink_log(kind: str, chat_id: str, text: str = "", key: str = "") -> None:
    log.info("TELEGRAM_TEST_SINK %s chat=%s text_sha256=%s len=%d id=%s", kind,
             hashlib.sha256(chat_id.encode()).hexdigest()[:12], hashlib.sha256(text.encode()).hexdigest()[:16],
             len(text), key)


class TelegramTestSink:
    """The relay's Telegram transport in the synthetic pilot (the default): nothing leaves the process. It answers
    ACCEPTED with a `testsink.tg.` id, so the relay's SENT / RETRY / UNKNOWN / FAILED handling runs as with the real
    Bot API; `script` queues outcomes ("ok", "retry", "permanent", "timeout") for tests. A test-sink id is never a
    Telegram delivery."""

    __test__ = False
    supports_idempotency = False
    service_window = False

    def __init__(self, script=None):
        self.script = list(script or [])
        self.sent: list[dict] = []

    def send(self, to: str, text: str, idempotency_key: str, reply_markup: dict | None = None) -> SendResult:
        step = self.script.pop(0) if self.script else "ok"
        if step == "retry":
            return SendResult(RETRYABLE, error="HTTP 429")
        if step == "permanent":
            return SendResult(PERMANENT, error="HTTP 403")                 # e.g. the driver blocked the bot
        self.sent.append({"chat": to, "text": text, "key": idempotency_key, "reply_markup": reply_markup})
        _sink_log("send", to, text, idempotency_key)
        if step == "timeout":
            raise ProviderTimeout("read timeout")
        # per-chat tag like a real send, but its own prefix: the console tells the sink apart from the real Bot API
        return SendResult(ACCEPTED, f"tgsink.{chat_tag(to)}.{10**9 + secrets.randbelow(10**9)}")


class TelegramGateway:
    """Bot replies to the webhook caller's chat (link results, button answers) and clearing an answered offer's
    buttons. Test-sink by default: one structured log line (chat id hashed, text hashed) and an in-memory record for
    tests; nothing leaves the process."""

    def __init__(self, provider=None):
        self.provider = provider
        self.sent: list[tuple[str, str]] = []
        self.cleared: list[tuple[str, int]] = []

    def reply(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        self.sent.append((chat_id, text))
        if self.provider is None:
            _sink_log("reply", chat_id, text)
            return
        self.provider.send(chat_id, text, str(uuid.uuid4()), reply_markup=reply_markup)

    def answer_callback(self, callback_query_id: str) -> None:
        """Stops the tapped button's spinner (answerCallbackQuery). Carries no text: the reply message says it all."""
        if self.provider is None or not callback_query_id:
            return
        self.provider.answer_callback(callback_query_id)

    def clear_buttons(self, chat_id: str, message_id: int) -> None:
        """After a valid answer the offer message keeps its text and loses its buttons (editMessageReplyMarkup)."""
        self.cleared.append((chat_id, message_id))
        if self.provider is None:
            _sink_log("clear_buttons", chat_id)
            return
        self.provider.clear_buttons(chat_id, message_id)


class TelegramBotProvider:
    """Telegram Bot API sendMessage, in the relay's Provider contract. Not wired by default (see module docstring).
    Telegram has no idempotency key: a resend after an ambiguous outcome may show the driver one identical repeat."""

    supports_idempotency = False
    service_window = False

    def __init__(self, token: str, client=None, base: str = "https://api.telegram.org"):
        import httpx
        self._base = f"{base}/bot{token}"                     # the token lives only in this URL, never in logs
        self._url = f"{self._base}/sendMessage"
        self.http = client or httpx.Client(timeout=10)

    def clear_buttons(self, chat_id: str, message_id: int) -> None:
        try:                                                  # best effort: the backend already refuses stale taps
            self.http.post(f"{self._base}/editMessageReplyMarkup",
                           json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})
        except Exception:                                     # noqa: BLE001
            log.warning("could not clear an answered offer's buttons")

    def answer_callback(self, callback_query_id: str) -> None:
        try:                                                  # best effort: only the client's spinner depends on it
            self.http.post(f"{self._base}/answerCallbackQuery", json={"callback_query_id": callback_query_id})
        except Exception:                                     # noqa: BLE001
            log.warning("could not answer a callback query")

    def send(self, to: str, text: str, idempotency_key: str, reply_markup: dict | None = None) -> SendResult:
        import httpx
        body = {"chat_id": to, "text": text}
        if reply_markup:
            body["reply_markup"] = reply_markup
        try:
            r = self.http.post(self._url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return SendResult(RETRYABLE, error=type(e).__name__)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(type(e).__name__) from e
        if r.status_code == 200:
            try:
                return SendResult(ACCEPTED, provider_ref(to, r.json()["result"]["message_id"]))
            except (ValueError, KeyError, TypeError):
                return SendResult(AMBIGUOUS, error="unreadable 200")
        if r.status_code == 429:
            return SendResult(RETRYABLE, error="HTTP 429")
        if r.status_code >= 500:
            return SendResult(AMBIGUOUS, error=f"HTTP {r.status_code}")
        return SendResult(PERMANENT, error=f"HTTP {r.status_code}")        # 400 bad chat, 403 blocked by the user


REMOVE_KEYBOARD = {"remove_keyboard": True}      # after a location share or "Not now": the one-time keyboard goes away


class AllowlistedTelegram:
    """TEST-BOT mode: wraps the real Bot API provider so that ONLY the configured test chats are ever contacted.
    Any other chat is refused before a network call (PERMANENT, so the relay records FAILED with an escalation:
    visible, never silent). A link redeemed from a non-allowlisted account therefore gets nothing either."""

    supports_idempotency = False
    service_window = False

    def __init__(self, provider, allowed: frozenset[str]):
        if not allowed:
            raise RuntimeError("CONFIG: the Telegram test allowlist is empty")
        self.provider, self.allowed = provider, frozenset(allowed)

    def allows(self, chat_id) -> bool:
        return str(chat_id) in self.allowed

    def send(self, to: str, text: str, idempotency_key: str, reply_markup: dict | None = None) -> SendResult:
        if not self.allows(to):
            log.warning("TELEGRAM_TEST_BOT refused a chat outside the allowlist chat=%s",
                        hashlib.sha256(str(to).encode()).hexdigest()[:12])
            return SendResult(PERMANENT, error="NOT_ALLOWLISTED")
        return self.provider.send(to, text, idempotency_key, reply_markup=reply_markup)

    def clear_buttons(self, chat_id: str, message_id: int) -> None:
        if self.allows(chat_id):
            self.provider.clear_buttons(chat_id, message_id)

    def answer_callback(self, callback_query_id: str) -> None:
        self.provider.answer_callback(callback_query_id)        # answers the tap that was already allowlisted inbound


TEST_BOT_ENVS = ("local", "dev")
_CHAT_ID_RE = re.compile(r"^[1-9][0-9]{4,14}$")                # a private chat id is a positive integer


def telegram_from_env(env: dict, secrets_client=None):
    """The ONE place Telegram delivery is chosen (relay and webhook replies). Returns (transport, allowlist):

      off        (None, None)                    no Telegram routing
      test_sink  (TelegramTestSink(), None)      default; nothing leaves the process
      test_bot   (AllowlistedTelegram, ids)      the real Bot API for a dedicated TEST bot, only to allowlisted chats.
                 Needs ALL of: SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED=true, SETUHAUL_ENV in {local, dev},
                 SETUHAUL_TELEGRAM_TEST_CHAT_IDS (1-10 numeric ids), and the token from Secrets Manager
                 (SETUHAUL_TELEGRAM_BOT_SECRET_ID) or, locally, SETUHAUL_TELEGRAM_TEST_BOT_TOKEN. It ignores
                 SETUHAUL_TELEGRAM_LIVE_AUTHORIZED and is refused in pilot / staging / production.
      live       (TelegramBotProvider, None)     production-style; needs SETUHAUL_TELEGRAM_LIVE_AUTHORIZED=true and the
                 token from Secrets Manager only. Not used in this cycle.

    Anything missing or unknown raises (fail closed). The token is never logged or returned."""
    mode = env.get("SETUHAUL_TELEGRAM_DELIVERY", "test_sink")
    if mode == "off":
        return None, None
    if mode == "test_sink":
        return TelegramTestSink(), None
    if mode == "test_bot":
        if env.get("SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED", "false").lower() != "true":
            raise RuntimeError("CONFIG: test_bot needs SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED=true")
        if env.get("SETUHAUL_ENV") not in TEST_BOT_ENVS:
            raise RuntimeError(f"CONFIG: test_bot runs only with SETUHAUL_ENV in {TEST_BOT_ENVS}")
        ids = [x.strip() for x in env.get("SETUHAUL_TELEGRAM_TEST_CHAT_IDS", "").split(",") if x.strip()]
        if not ids or len(ids) > 10 or not all(_CHAT_ID_RE.match(x) for x in ids):
            raise RuntimeError("CONFIG: SETUHAUL_TELEGRAM_TEST_CHAT_IDS must list 1-10 numeric private chat ids")
        if env.get("SETUHAUL_TELEGRAM_BOT_SECRET_ID"):
            token = _token_from_secret(env, secrets_client)
        else:
            token = (env.get("SETUHAUL_TELEGRAM_TEST_BOT_TOKEN") or "").strip()
        if not re.match(r"^[0-9]{5,15}:[A-Za-z0-9_-]{30,}$", token):
            raise RuntimeError("CONFIG: no usable Telegram test-bot token")
        allow = frozenset(ids)
        return AllowlistedTelegram(TelegramBotProvider(token), allow), allow
    if mode == "live":
        if env.get("SETUHAUL_TELEGRAM_LIVE_AUTHORIZED", "false").lower() != "true":
            raise RuntimeError("CONFIG: SETUHAUL_TELEGRAM_DELIVERY=live needs SETUHAUL_TELEGRAM_LIVE_AUTHORIZED=true")
        if not env.get("SETUHAUL_TELEGRAM_BOT_SECRET_ID"):
            raise RuntimeError("CONFIG: SETUHAUL_TELEGRAM_BOT_SECRET_ID is required for live Telegram delivery")
        return TelegramBotProvider(_token_from_secret(env, secrets_client)), None
    raise RuntimeError(f"CONFIG: unknown SETUHAUL_TELEGRAM_DELIVERY {mode!r}")


def _token_from_secret(env: dict, client) -> str:
    import json
    if client is None:
        import boto3
        client = boto3.client("secretsmanager", region_name=env.get("SETUHAUL_AWS_REGION", "ap-southeast-2"))
    try:
        raw = client.get_secret_value(SecretId=env["SETUHAUL_TELEGRAM_BOT_SECRET_ID"])["SecretString"]
        return json.loads(raw)["token"] if raw.strip().startswith("{") else raw.strip()
    except Exception as e:
        raise RuntimeError(f"CONFIG: Telegram bot secret unreadable ({type(e).__name__})") from None
