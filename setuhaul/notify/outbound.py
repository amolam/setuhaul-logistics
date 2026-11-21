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
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

log = logging.getLogger("setuhaul.outbound")

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

    def send(self, to: str, text: str, idempotency_key: str) -> SendResult: ...


class ProviderTimeout(Exception):
    """The request may have reached the provider: the outcome is AMBIGUOUS, never a blind retry."""


@dataclass
class LocalProvider:
    """In-process provider for tests and local runs. `idempotent=True` behaves like a provider that honours an
    idempotency key; `script` queues outcomes ("ok", "retry", "permanent", "timeout", "lost") for the next sends.
    "lost" = the provider accepted the message but the answer never reached us (the worst case)."""

    idempotent: bool = False
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


class WhatsAppCloudProvider:
    """Meta WhatsApp Cloud API. It has no idempotency key, so `supports_idempotency` is False. Our notification id
    travels as `biz_opaque_callback_data`, which Meta echoes on status webhooks: an ambiguous send can be matched to
    its receipt without the provider's message id."""

    supports_idempotency = False

    def __init__(self, phone_number_id: str, token: str, client=None, base="https://graph.facebook.com/v21.0"):
        import httpx
        self.url = f"{base}/{phone_number_id}/messages"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.http = client or httpx.Client(timeout=10)

    def send(self, to: str, text: str, idempotency_key: str) -> SendResult:
        import httpx
        body = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text", "text": {"body": text},
                "biz_opaque_callback_data": idempotency_key}
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
        return SendResult(PERMANENT, error=f"HTTP {r.status_code}")


class OutboundRelay:
    def __init__(self, db, provider: Provider, resolve_phone: Callable[[str], str] | None = None):
        self.db, self.provider = db, provider
        self.resolve_phone = resolve_phone or self._phone

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
            text = body if body is not None else template_text(template, self._language(nid))
            with self.db.tx("system", scope) as c:                # R3-P2-01: re-checked just before the provider call
                go = c.execute("SELECT begin_send(%s,%s)", (nid, attempt)).fetchone()[0]
            if not go:
                results[str(nid)] = "NOT_SENT"
                continue
            if text is None:
                outcome = SendResult(PERMANENT, error=f"UNKNOWN_TEMPLATE {template}")
            else:
                try:
                    outcome = self.provider.send(self.resolve_phone(ref), text, str(nid))
                except ProviderTimeout as e:
                    outcome = SendResult(AMBIGUOUS, error=str(e))
            with self.db.tx("system", scope) as c:
                results[str(nid)] = c.execute("SELECT record_send_result(%s,%s,%s,%s,%s)",
                                              (nid, attempt, outcome.outcome, outcome.provider_message_id,
                                               outcome.error)).fetchone()[0]
        return {"reconciled": reconciled, "enqueued": enqueued, "claimed": len(claimed), "results": results}
