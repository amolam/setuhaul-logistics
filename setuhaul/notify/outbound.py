"""B-WF-3 outbound delivery (migration 0017; review R2.0 S4-R2-07; docs/operations/DEPLOYMENT_GATES.md OUTBOUND_DELIVERY).

    OutboundRelay.run_once():
      reconcile_notifications()   stuck sends → UNKNOWN; spent UNKNOWN → FAILED (+ escalation)
      enqueue_notifications()     outbox → one notification per (event, channel), stable id, consumed in the same tx
      claim_notifications()       due sends; an agent reply's body comes from the STORED row, checked against its hash
      provider.send(...)          idempotency key = notification id (honoured only where the provider supports it)
      record_send_result()        SENT | RETRY | FAILED | UNKNOWN, fenced by the attempt number

Delivery is at least once with at most one same-id resend per notification, never exactly once. `supports_idempotency`
says honestly which provider absorbs that resend; the WhatsApp Cloud API does not, so a driver may see one identical
repeat after an ambiguous send. The relay never generates text: it sends the stored reply or a fixed template.

WhatsApp's customer-service window (review E6-01). Free-form text reaches a driver only within 24 hours of the
driver's last message; after that Meta accepts only a pre-approved template. For a provider with that rule
(`service_window = True`) the relay checks the window in the database just before sending, with a safety margin:
  * inside the window: the stored reply or the notice text, as before;
  * outside, a notice: the Meta-approved template for that notice, in the driver's language if that version is
    approved, else the approved English one (`ApprovedTemplates`, configured from what Meta actually approved);
  * outside, with no approved template, or a stored agent reply (free text cannot be templated): PERMANENT
    (TEMPLATE_NOT_APPROVED / WINDOW_CLOSED), so the notification is FAILED and a NOTIFICATION_FAILED escalation tells
    a person. A late approval notice is never dropped silently.
Meta's own refusals (a template it rejects, or free text it refuses after the window: a `failed` status receipt,
error 131047) end the same way: FAILED plus an escalation, through record_send_result or record_delivery_status.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

log = logging.getLogger("setuhaul.outbound")
# Demo release: the conversation refs of the notification being sent, read by TracedTelegram for its trace metadata
OUTBOUND_TRACE: contextvars.ContextVar = contextvars.ContextVar("setuhaul_outbound_trace", default=None)

ACCEPTED, RETRYABLE, PERMANENT, AMBIGUOUS = "ACCEPTED", "RETRYABLE", "PERMANENT", "AMBIGUOUS"

# Fixed driver templates for proactive messages (B-AI-3 rule: no model writes these). Hinglish and Hindi versions were
# approved in the native-speaker review (S4-R2-08, 21 Nov 2026); the language is the conversation's (see _language).
TEMPLATES = {
    "booking_approved": "Your dock booking is confirmed by the warehouse.",
    "booking_confirmed": "Your dock booking is confirmed.",
    "booking_rejected": "The warehouse could not take that slot. Reply here and we will find another one.",
    "booking_expired": "The warehouse did not confirm in time, so that slot was released. Your earlier booking, if any, still stands. Reply here and we will help.",
    "booking_invalidated": "That slot is no longer available. Reply here and we will find another one.",
    "booking_withdrawn": "Your slot request was withdrawn as agreed with the dock office.",
    "booking_superseded": "Your booking was changed by the warehouse. Reply here if you have a question.",
}

TEMPLATES_HL = {
    "booking_approved": "Warehouse ne aapki dock booking pakki kar di hai.",
    "booking_confirmed": "Aapki dock booking pakki ho gayi hai.",
    "booking_rejected": "Warehouse wo slot nahi le paaya. Yahan reply kijiye, hum doosra slot dhoondh denge.",
    "booking_expired": "Warehouse ne time par confirm nahi kiya, isliye wo slot chhod diya gaya. Agar pehle se koi booking "
                       "thi, to wo abhi bhi pakki hai. Yahan reply kijiye, hum madad karenge.",
    "booking_invalidated": "Wo slot ab khaali nahi hai. Yahan reply kijiye, hum doosra slot dhoondh denge.",
    "booking_withdrawn": "Dock office se baat ke mutabik aapki slot request wapas le li gayi hai.",
    "booking_superseded": "Warehouse ne aapki booking badal di hai. Koi sawaal ho to yahan reply kijiye.",
}
TEMPLATES_HI = {
    "booking_approved": "वेयरहाउस ने आपकी डॉक बुकिंग पक्की कर दी है।",
    "booking_confirmed": "आपकी डॉक बुकिंग पक्की हो गई है।",
    "booking_rejected": "वेयरहाउस वह स्लॉट नहीं ले पाया। यहाँ जवाब दें, हम दूसरा स्लॉट ढूँढ देंगे।",
    "booking_expired": "वेयरहाउस ने समय पर पुष्टि नहीं की, इसलिए वह स्लॉट छोड़ दिया गया। अगर पहले से कोई बुकिंग थी, "
                       "तो वह अब भी पक्की है। यहाँ जवाब दें, हम मदद करेंगे।",
    "booking_invalidated": "वह स्लॉट अब उपलब्ध नहीं है। यहाँ जवाब दें, हम दूसरा स्लॉट ढूँढ देंगे।",
    "booking_withdrawn": "डॉक ऑफ़िस से हुई बात के अनुसार आपका स्लॉट अनुरोध वापस ले लिया गया है।",
    "booking_superseded": "वेयरहाउस ने आपकी बुकिंग बदल दी है। कोई सवाल हो तो यहाँ जवाब दें।",
}


def template_text(template: str, lang: str) -> str | None:
    """The notice in the driver's language; English when there is no reviewed version."""
    table = TEMPLATES_HL if lang == "hi-Latn" else TEMPLATES_HI if lang == "hi" else TEMPLATES
    return table.get(template) or TEMPLATES.get(template)


@dataclass
class SendResult:
    outcome: str                         # ACCEPTED | RETRYABLE | PERMANENT | AMBIGUOUS
    provider_message_id: str | None = None
    error: str | None = None


class Provider(Protocol):
    supports_idempotency: bool
    # True for a provider that delivers free-form text only inside a customer-service window (WhatsApp); such a
    # provider also has send_template(to, name, language, params, idempotency_key).
    service_window: bool

    def send(self, to: str, text: str, idempotency_key: str) -> SendResult: ...


# Body variables per notice. The reviewed notice texts are fixed, so none has a variable today. A notice that gains one
# names here the payload fields that fill {{1}}, {{2}}, ... in order; `template_params` checks the count against the text.
TEMPLATE_VARIABLES: dict[str, tuple[str, ...]] = {}


def template_params(template: str, payload: dict | None, variables: dict | None = None) -> list[str]:
    import re
    fields = (TEMPLATE_VARIABLES if variables is None else variables).get(template, ())
    placeholders = len(set(re.findall(r"\{\{(\d+)\}\}", TEMPLATES.get(template, ""))))
    if placeholders != len(fields):
        raise ValueError(f"{template}: {placeholders} placeholders, {len(fields)} variables")
    missing = [f for f in fields if (payload or {}).get(f) in (None, "")]
    if missing:
        raise ValueError(f"{template}: payload lacks {missing}")
    return [str(payload[f]) for f in fields]


@dataclass(frozen=True)
class ApprovedTemplates:
    """The WhatsApp templates Meta has approved, as the owner records them after submission ("name:language").
    Nothing is assumed approved: the default is empty, so outside the window every notice fails loudly until the
    owner records an approval. Hinglish versions (`setuhaul_<notice>_hl`) are used only if approved, under whatever
    language code Meta approved them with; no Hinglish code is assumed (review decision, E6)."""

    approved: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, raw: str | None) -> "ApprovedTemplates":
        import json
        items = json.loads(raw or "[]")
        if not isinstance(items, list) or not all(
                isinstance(i, str) and i.count(":") == 1 and all(i.split(":")) for i in items):
            raise ValueError('SETUHAUL_WA_APPROVED_TEMPLATES must be a JSON list of "name:language"')
        return cls(tuple(tuple(i.split(":")) for i in items))

    def choose(self, template: str, lang: str | None) -> tuple[str, str] | None:
        """(name, language code) to send, or None when no approved version exists."""
        def find(name, code=None):
            return next(((n, c) for n, c in self.approved if n == name and (code is None or c == code)), None)
        base = f"setuhaul_{template}"
        if lang == "hi" and (hit := find(base, "hi")):
            return hit
        if lang == "hi-Latn" and (hit := find(base + "_hl")):
            return hit
        return find(base, "en")


class ProviderTimeout(Exception):
    """The request may have reached the provider: the outcome is AMBIGUOUS, never a blind retry."""


@dataclass
class LocalProvider:
    """In-process provider for tests and local runs. `idempotent=True` behaves like a provider that honours an
    idempotency key; `script` queues outcomes ("ok", "retry", "permanent", "timeout", "lost") for the next sends.
    "lost" = the provider accepted the message but the answer never reached us (the worst case)."""

    idempotent: bool = False
    service_window: bool = False                            # True: behaves like WhatsApp (templates outside 24 h)
    script: list = field(default_factory=list)
    script_for: dict = field(default_factory=dict)          # outcomes for one recipient only: {number: [steps]}
    delivered: list = field(default_factory=list)           # what the driver's phone actually shows
    calls: int = 0
    _by_key: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def supports_idempotency(self) -> bool:
        return self.idempotent

    def send(self, to: str, text: str, idempotency_key: str) -> SendResult:
        with self._lock:
            self.calls += 1
            mine = self.script_for.get(to)
            step = mine.pop(0) if mine else (self.script.pop(0) if self.script else "ok")
            if step == "retry":
                return SendResult(RETRYABLE, error="429 rate limited")
            if step == "permanent":
                return SendResult(PERMANENT, error="131026 not a WhatsApp user")
            if self.idempotent and idempotency_key in self._by_key:
                return SendResult(ACCEPTED, self._by_key[idempotency_key])     # absorbed: nothing new on the phone
            pid = f"wamid.local.{uuid.uuid4().hex}"                         # unique, like a real provider id
            self.delivered.append((to, text, idempotency_key, pid))
            self._by_key[idempotency_key] = pid
            if step == "timeout":
                raise ProviderTimeout("read timeout")
            if step == "lost":
                raise SystemExit("worker died after the provider accepted")    # simulated crash
            return SendResult(ACCEPTED, pid)

    def send_template(self, to: str, name: str, language: str, params: list, idempotency_key: str) -> SendResult:
        return self.send(to, f"template:{name}:{language}:{'|'.join(params)}", idempotency_key)


class TestSinkProvider:
    """The internal notification sink of a synthetic pilot (`SETUHAUL_NOTIFICATION_MODE=test_sink`, the default).

    Nothing leaves the process: no network call, no phone, no Meta. Each send writes one structured log line
    (notification id, mode, a hash and the length of the text; never the text or the number) and answers ACCEPTED with
    a `testsink.` id, so the relay's state machine (SENT, retries, FAILED with an escalation) runs exactly as with a
    real provider and the database records it. A `testsink.` id is never a WhatsApp delivery and must not be reported
    as one. Numbers listed in `fail_phones` are refused permanently, so failure handling can be exercised on demand."""

    __test__ = False                       # not a pytest test class, despite the name
    supports_idempotency = False
    service_window = False

    def __init__(self, fail_phones=(), log=None):
        self.fail_phones = frozenset(fail_phones)
        self.log = log or logging.getLogger("setuhaul.outbound.test_sink")

    def _record(self, to: str, kind: str, text: str, idempotency_key: str) -> SendResult:
        import hashlib
        import json
        refused = to in self.fail_phones
        self.log.info("TEST_SINK %s", json.dumps({
            "mode": "test_sink", "kind": kind, "notification_id": idempotency_key, "refused": refused,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()[:16], "text_len": len(text)}))
        if refused:
            return SendResult(PERMANENT, error="TEST_SINK_REFUSED")
        return SendResult(ACCEPTED, f"testsink.{uuid.uuid4().hex}")

    def send(self, to: str, text: str, idempotency_key: str) -> SendResult:
        return self._record(to, "text", text, idempotency_key)

    def send_template(self, to: str, name: str, language: str, params: list, idempotency_key: str) -> SendResult:
        return self._record(to, "template", f"{name}:{language}:{'|'.join(params)}", idempotency_key)


class WhatsAppCloudProvider:
    """Meta WhatsApp Cloud API. It has no idempotency key, so `supports_idempotency` is False. Our notification id
    travels as `biz_opaque_callback_data`, which Meta echoes on status webhooks: an ambiguous send can be matched to
    its receipt without the provider's message id."""

    supports_idempotency = False
    service_window = True

    def __init__(self, phone_number_id: str, token: str, client=None, base="https://graph.facebook.com/v21.0"):
        import httpx
        self.url = f"{base}/{phone_number_id}/messages"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.http = client or httpx.Client(timeout=10)

    def send(self, to: str, text: str, idempotency_key: str) -> SendResult:
        return self._post({"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text", "text": {"body": text},
                           "biz_opaque_callback_data": idempotency_key})

    def send_template(self, to: str, name: str, language: str, params: list, idempotency_key: str) -> SendResult:
        template = {"name": name, "language": {"code": language}}
        if params:
            template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._post({"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "template",
                           "template": template, "biz_opaque_callback_data": idempotency_key})

    def _post(self, body: dict) -> SendResult:
        import httpx
        try:
            r = self.http.post(self.url, json=body, headers=self.headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return SendResult(RETRYABLE, error=type(e).__name__)              # never reached Meta
        except httpx.TimeoutException as e:
            raise ProviderTimeout(type(e).__name__) from e                     # may have reached Meta
        if r.status_code == 200:
            return SendResult(ACCEPTED, r.json()["messages"][0]["id"])
        if r.status_code == 429:                                       # throttled: Meta did not take the message
            return SendResult(RETRYABLE, error="HTTP 429")
        if r.status_code >= 500:
            # R3-P2-02: the request reached Meta and nothing in its contract says a 5xx means "not accepted". Treated
            # as ambiguous: wait for a receipt, then at most one resend with the same id; never a blind retry.
            return SendResult(AMBIGUOUS, error=f"HTTP {r.status_code}")
        # A refusal (e.g. 132001 template missing or not approved, 132000 wrong variables, 131047 outside the window):
        # PERMANENT, so the notification fails with an escalation. Meta's error code is kept for the person.
        try:
            code = (r.json().get("error") or {}).get("code")
        except (ValueError, AttributeError):
            code = None
        return SendResult(PERMANENT, error=f"HTTP {r.status_code}" + (f" meta={code}" if code else ""))


class OutboundRelay:
    WINDOW_MIN = 24 * 60                                     # WhatsApp customer-service window, minutes

    def __init__(self, db, provider: Provider, resolve_phone: Callable[[str], str] | None = None,
                 approved_templates: ApprovedTemplates | None = None, window_margin_min: int = 30, telegram=None):
        self.db, self.provider = db, provider
        self._sent: tuple[str, str | None] | None = None        # (channel, text) of the send in progress
        # Roadmap Phase 4 (0035/0036): a driver whose Telegram account is linked is reached on Telegram, through this
        # same relay (same claim, retries, UNKNOWN and FAILED handling). None = no Telegram routing (WhatsApp/SMS
        # behaviour unchanged). The transport is the test sink unless the owner authorizes real sends.
        self.telegram = telegram
        self.resolve_phone = resolve_phone or self._phone
        self.approved = approved_templates or ApprovedTemplates()
        # Our receipt time trails the driver's send time by the webhook delay; the margin keeps us on the safe side.
        self.window_min = self.WINDOW_MIN - window_margin_min

    def _window_open(self, recipient_ref: str) -> bool:
        """True when the driver's last WhatsApp message to us is recent enough for free-form text (database clock)."""
        kind, _, ident = recipient_ref.partition(":")
        if kind != "driver":
            return False
        with self.db.tx("system", self._scope(), readonly=True) as c:
            return bool(c.execute(
                """SELECT coalesce(max(m.created_at) > clock_timestamp() - make_interval(mins => %s), false)
                     FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id
                    WHERE t.driver_id = %s AND m.sender_type = 'DRIVER' AND m.channel = 'WHATSAPP'""",
                (self.window_min, ident)).fetchone()[0])

    def _telegram_route(self, nid) -> dict:
        """The linked chat (if any), the open offer the message carries and whether to offer the one-off location
        button: read by the database just before the send (telegram_delivery_for, 0036). Demo release: in the same
        transaction, the conversation and the driver message the reply answers, as opaque refs for the trace."""
        from ..observability import tracing
        with self.db.tx("system", self._scope()) as c:
            route = c.execute("SELECT telegram_delivery_for(%s)", (str(nid),)).fetchone()[0] or {}
            row = c.execute("""SELECT n.thread_id, m.reply_to FROM notifications n
                                 LEFT JOIN chat_messages m ON m.message_id = n.chat_message_id
                                WHERE n.notification_id = %s""", (str(nid),)).fetchone()
        if row:
            OUTBOUND_TRACE.set({"thread_id": tracing.ref(row[0]), "reply_to_message_ref": tracing.ref(row[1])})
        return route

    def _deliver(self, ref: str, template: str | None, payload, body: str | None, nid) -> SendResult:
        lang = None
        if body is None:
            lang = self._language(nid)
            if template_text(template, lang) is None:
                return SendResult(PERMANENT, error=f"UNKNOWN_TEMPLATE {template}")
        if self.telegram is not None:
            route = self._telegram_route(nid)
            if route.get("chat_ref"):
                from .telegram import driver_authorized, reply_markup_for
                kind, _, drv = str(ref).partition(":")
                if not driver_authorized(drv if kind == "driver" else None):
                    # live Telegram: never send to a driver who is not on the owner's pilot list (visible, not silent)
                    return SendResult(PERMANENT, error="TELEGRAM_DRIVER_NOT_AUTHORIZED")
                text = body if body is not None else template_text(template, lang)
                self._sent = ("TELEGRAM", text)
                # Telegram has no 24-hour window and no templates; the stored reply or the fixed notice goes as is
                return self.telegram.send(route["chat_ref"], text, str(nid), reply_markup=reply_markup_for(route))
        if getattr(self.provider, "service_window", False) and not self._window_open(ref):
            if body is not None:                               # a stored agent reply is free text: no template fits
                return SendResult(PERMANENT, error="WINDOW_CLOSED: agent reply outside the 24-hour window")
            choice = self.approved.choose(template, lang)
            if choice is None:
                return SendResult(PERMANENT, error=f"TEMPLATE_NOT_APPROVED {template} ({lang})")
            try:
                params = template_params(template, payload)
            except ValueError as e:
                return SendResult(PERMANENT, error=f"TEMPLATE_VARIABLES {e}")
            name, code = choice
            self._sent = ("WHATSAPP", template_text(template, lang))
            return self.provider.send_template(self.resolve_phone(ref), name, code, params, str(nid))
        text = body if body is not None else template_text(template, lang)
        self._sent = ("WHATSAPP", text)
        return self.provider.send(self.resolve_phone(ref), text, str(nid))

    def _scope(self) -> list[str]:
        with self.db.tx("system", readonly=True) as c:
            return [r[0] for r in c.execute("SELECT facility_id FROM facilities")] + ["CENTRAL"]

    def _phone(self, recipient_ref: str) -> str:
        """Pseudonymous reference → number, at send time only; numbers never enter notifications or logs."""
        kind, _, ident = recipient_ref.partition(":")
        if kind != "driver":
            raise ValueError(recipient_ref)
        with self.db.tx("system", self._scope(), readonly=True) as c:
            return c.execute("SELECT phone_e164 FROM drivers WHERE driver_id=%s", (ident,)).fetchone()[0]

    def _language(self, notification_id) -> str:
        """The conversation's language, read from the driver's last longer message in the notification's thread (the
        rule the agent uses: Devanagari -> hi, Hinglish words -> hi-Latn, otherwise en). No thread: English.
        Known limit: it is read at send time, so a resend after the driver switched language uses the new one."""
        from ..agent.replies import DEVANAGARI, detect_language
        with self.db.tx("system", self._scope(), readonly=True) as c:
            rows = c.execute("""SELECT m.body_masked FROM notifications n JOIN chat_messages m ON m.thread_id = n.thread_id
                                 WHERE n.notification_id = %s AND m.sender_type = 'DRIVER'
                                 ORDER BY m.message_id DESC LIMIT 6""", (notification_id,)).fetchall()
        for (body,) in rows:
            if body and (DEVANAGARI.search(body) or len(body.split()) > 2):
                return detect_language(body)
        return "en"

    def _store_notice(self, nid, channel: str, text: str | None, scope) -> None:
        """Demo release: a fixed notice the driver received (e.g. booking_approved) is part of the stored conversation,
        as a SYSTEM message in the notification's conversation, once per notification (external id notice:<id>).
        SYSTEM rows never enter the outbox (that trigger fires for AGENT replies only), so nothing is sent twice.
        Best effort after the send is recorded; a failure is logged, never fatal."""
        if not text:
            return
        try:
            with self.db.tx("system", scope) as c:
                c.execute("SELECT record_notice_message(%s, %s, %s)", (str(nid), channel, text[:1000]))   # 0046
        except Exception as e:                                   # noqa: BLE001
            log.warning("ALERT notice not stored in the conversation (%s)", type(e).__name__)

    def run_once(self, limit: int = 20) -> dict:
        scope = self._scope()
        with self.db.tx("system", scope) as c:
            reconciled = c.execute("SELECT reconcile_notifications()").fetchone()[0]
        with self.db.tx("system", scope) as c:
            enqueued = c.execute("SELECT enqueue_notifications()").fetchone()[0]
        with self.db.tx("system", scope) as c:                    # the claim commits before anything is sent
            claimed = c.execute("SELECT * FROM claim_notifications(%s)", (limit,)).fetchall()
        results = {}
        for nid, attempt, channel, ref, template, payload, body, resend in claimed:
            with self.db.tx("system", scope) as c:                # R3-P2-01: re-checked just before the provider call
                go = c.execute("SELECT begin_send(%s,%s)", (nid, attempt)).fetchone()[0]
            if not go:
                results[str(nid)] = "NOT_SENT"
                continue
            self._sent = None
            OUTBOUND_TRACE.set(None)
            try:
                outcome = self._deliver(ref, template, payload, body, nid)
            except ProviderTimeout as e:
                outcome = SendResult(AMBIGUOUS, error=str(e))
            with self.db.tx("system", scope) as c:
                results[str(nid)] = c.execute("SELECT record_send_result(%s,%s,%s,%s,%s)",
                                              (nid, attempt, outcome.outcome, outcome.provider_message_id,
                                               outcome.error)).fetchone()[0]
            if body is None and outcome.outcome == ACCEPTED and self._sent:
                self._store_notice(nid, *self._sent, scope)
        return {"reconciled": reconciled, "enqueued": enqueued, "claimed": len(claimed), "results": results}
