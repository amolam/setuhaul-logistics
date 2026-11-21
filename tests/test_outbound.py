"""B-WF-3 durable outbound delivery (migration 0017; S4-R2-07).

The stored reply is what is sent, notification ids are stable, retries back off, ambiguous sends are never blindly
re-sent, a lost provider answer is reconciled, and duplicates are absorbed where the provider supports idempotency.
Delivery is at least once with at most one same-id resend: the tests show both the absorbed and the visible case.
"""
import hashlib
import json
import threading
import uuid

import httpx
import psycopg
import pytest

from conftest import as_role
from setuhaul.api.db import Db
from setuhaul.agent.replies import DEVANAGARI
from setuhaul.notify.outbound import (ACCEPTED, AMBIGUOUS, PERMANENT, RETRYABLE, TEMPLATES, LocalProvider,
                                      OutboundRelay, ProviderTimeout, WhatsAppCloudProvider, template_text)
from test_api import app, client, keys, post_signed  # noqa: F401
from test_approvals import pending
from test_db_requests import JAI, decide, driver_says, setup_thread


def reply(conn, t, text=None) -> int:
    """The agent's reply, stored exactly as record_reply stores it (AGENT row; hash of the stored text)."""
    text = text or f"Your slot is D1 at 10:00. ({uuid.uuid4().hex[:6]})"
    turn = driver_says(conn, t, "kab tak?")                          # a turn reply always answers a driver message
    with as_role(conn, "setuhaul_agent", t["fac"], t["drv"]) as c:
        return c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked,
                                                       reply_to,reply_generation)
                            VALUES (%s,'AGENT','WHATSAPP',%s,%s,%s,0) RETURNING message_id""",
                         (t["tid"], hashlib.sha256(text.encode()).hexdigest(), text, turn)).fetchone()[0]


def note(conn, message_id) -> dict:
    cur = conn.execute("SELECT * FROM setuhaul.notifications WHERE chat_message_id=%s", (message_id,))
    row = cur.fetchone()
    return dict(zip([d.name for d in cur.description], row)) if row else None


def note_by_shipment(conn, sid, template) -> dict:
    cur = conn.execute("SELECT * FROM setuhaul.notifications WHERE shipment_id=%s AND template_id=%s", (sid, template))
    row = cur.fetchone()
    return dict(zip([d.name for d in cur.description], row)) if row else None


def relay(dsn, provider):
    return OutboundRelay(Db(dsn), provider)


def drain(r, n=3):
    """Run the relay a few times with a large batch: other tests' events share this database."""
    out = None
    for _ in range(n):
        out = r.run_once(limit=1000)
    return out


def make_due(conn, nid, column):
    conn.execute(f"UPDATE setuhaul.notifications SET {column} = now() - interval '1 second' WHERE notification_id=%s", (nid,))


def mine(provider, nid):
    return [d for d in provider.delivered if d[2] == str(nid)]


def escalated(conn, nid):
    return conn.execute("""SELECT count(*) FROM setuhaul.escalations WHERE reason_code='NOTIFICATION_FAILED'
                           AND handoff->>'notification_id' = %s""", (str(nid),)).fetchone()[0]


# ================================================================ stored reply, stable id
def test_an_agent_reply_becomes_one_notification_with_a_stable_id(conn, dsn):
    t = setup_thread(conn)
    mid = reply(conn, t)
    p = LocalProvider()
    drain(relay(dsn, p), 2)
    n = note(conn, mid)
    ev = conn.execute("SELECT event_id FROM setuhaul.outbox WHERE event_type='AgentReplyStored' AND aggregate_id=%s",
                      (str(mid),)).fetchone()[0]
    expected = uuid.UUID(hashlib.md5(f"setuhaul-notification:{ev}:WHATSAPP:driver:{t['drv']}".encode()).hexdigest())
    assert n["notification_id"] == expected and n["template_id"] == "agent_reply"
    assert n["payload"] == {"message_id": mid}                                # never the text itself
    count = conn.execute("SELECT count(*) FROM setuhaul.notifications WHERE chat_message_id=%s", (mid,)).fetchone()[0]
    assert count == 1 and n["status"] == "SENT" and len(mine(p, n["notification_id"])) == 1


def test_s4_r2_07_the_stored_reply_is_what_is_sent_and_a_changed_row_is_refused(conn, dsn):
    t = setup_thread(conn)
    text = "Ye slots khaali hain: 1. D1 10:00. Jo slot chahiye, uska number bhej dijiye."
    mid = reply(conn, t, text)
    p = LocalProvider()
    drain(relay(dsn, p), 1)
    n = note(conn, mid)
    assert [d[1] for d in mine(p, n["notification_id"])] == [text]           # byte for byte the stored row

    mid2 = reply(conn, t, "Original stored reply text")
    r = relay(dsn, p)
    with r.db.tx("system", r._scope()) as c:                                   # enqueue only, then the row changes
        c.execute("SELECT enqueue_notifications()")
    conn.execute("UPDATE setuhaul.chat_messages SET body_masked='Something regenerated' WHERE message_id=%s", (mid2,))
    drain(r, 1)
    n2 = note(conn, mid2)
    assert (n2["status"], n2["last_error"]) == ("FAILED", "CANONICAL_MISMATCH")
    assert mine(p, n2["notification_id"]) == [] and escalated(conn, n2["notification_id"]) == 1


# ================================================================ retries
def test_retryable_errors_back_off_and_then_send(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["retry"])
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "RETRY" and n["attempts"] == 1 and n["next_retry_at"] is not None
    drain(r, 1)
    assert note(conn, mid)["status"] == "RETRY"                                # not due yet: backoff respected
    make_due(conn, n["notification_id"], "next_retry_at")
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "SENT" and n["attempts"] == 2 and len(mine(p, n["notification_id"])) == 1


def _only_mine(conn, dsn, t, script):
    """A relay whose provider applies `script` to this test's notification only."""
    mid = reply(conn, t)
    steps = list(script)

    class P(LocalProvider):
        def send(self, to, text, key):
            n = note(conn, mid)
            if n and key == str(n["notification_id"]) and steps:
                self.script = [steps.pop(0)]
            else:
                self.script = []
            return super().send(to, text, key)

    p = P()
    return mid, p, relay(dsn, p)


def test_retries_used_up_fail_and_tell_a_person(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["retry"] * 4)
    for _ in range(4):
        drain(r, 1)
        n = note(conn, mid)
        if n["status"] == "RETRY":
            make_due(conn, n["notification_id"], "next_retry_at")
    n = note(conn, mid)
    assert (n["status"], n["attempts"]) == ("FAILED", 4) and escalated(conn, n["notification_id"]) == 1
    assert mine(p, n["notification_id"]) == []


def test_a_permanent_error_fails_at_once_and_tells_a_person(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["permanent"])
    drain(r, 1)
    n = note(conn, mid)
    assert (n["status"], n["attempts"]) == ("FAILED", 1) and "131026" in n["last_error"]
    assert escalated(conn, n["notification_id"]) == 1


# ================================================================ ambiguity, reconciliation, duplicates
def test_a_timeout_is_unknown_and_a_receipt_resolves_it_without_a_resend(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["timeout"])
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "UNKNOWN" and n["unknown_resend_count"] == 0
    drain(r, 1)
    assert note(conn, mid)["status"] == "UNKNOWN"                              # inside the grace window: no resend
    with Db(dsn).tx("ingest") as c:                                           # receipt matched by our id (echoed back)
        assert c.execute("SELECT record_delivery_status(NULL,%s,'DELIVERED',NULL)", (n["notification_id"],)).fetchone()[0] \
            == "DELIVERED"
    make_due(conn, n["notification_id"], "unknown_deadline")
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "DELIVERED" and len(mine(p, n["notification_id"])) == 1


@pytest.mark.parametrize("idempotent,expected_on_phone", [(True, 1), (False, 2)])
def test_a_lost_provider_answer_is_reconciled_with_at_most_one_same_id_resend(conn, dsn, idempotent, expected_on_phone):
    """The worst case: the provider accepted, the worker died before recording it. Reconciliation → UNKNOWN → one
    resend with the same id. An idempotent provider absorbs it; WhatsApp-like providers show one identical repeat."""
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["lost"])
    p.idempotent = idempotent
    with pytest.raises(SystemExit):
        drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "SENDING"
    make_due(conn, n["notification_id"], "sending_started_at")
    conn.execute("UPDATE setuhaul.notifications SET sending_started_at = now() - interval '10 minutes' WHERE notification_id=%s",
                 (n["notification_id"],))
    drain(r, 1)
    n = note(conn, mid)
    assert (n["status"], n["last_error"]) == ("UNKNOWN", "WORKER_LOST")
    make_due(conn, n["notification_id"], "unknown_deadline")
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "SENT" and n["unknown_resend_count"] == 1
    phone = mine(p, n["notification_id"])
    assert len(phone) == expected_on_phone and len({d[1] for d in phone}) == 1      # any repeat is identical


def test_a_second_ambiguity_after_the_resend_fails_instead_of_a_third_send(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["timeout", "timeout"])
    drain(r, 1)
    n = note(conn, mid)
    make_due(conn, n["notification_id"], "unknown_deadline")
    drain(r, 1)                                                                # the one resend, also ambiguous
    n = note(conn, mid)
    assert n["status"] == "UNKNOWN" and n["unknown_resend_count"] == 1
    make_due(conn, n["notification_id"], "unknown_deadline")
    drain(r, 1)
    n = note(conn, mid)
    assert (n["status"], n["last_error"]) == ("FAILED", "UNCONFIRMED_AFTER_RESEND")
    assert len(mine(p, n["notification_id"])) <= 2 and escalated(conn, n["notification_id"]) == 1


def test_a_stale_worker_cannot_move_the_state_but_its_provider_id_is_kept(conn, dsn):
    t = setup_thread(conn)
    mid = reply(conn, t)
    db = Db(dsn)
    r = relay(dsn, LocalProvider())
    scope = r._scope()
    with db.tx("system", scope) as c:
        c.execute("SELECT enqueue_notifications(1000)")
    nid = note(conn, mid)["notification_id"]
    with db.tx("system", scope) as c:
        claimed = {row[0]: row[1] for row in c.execute("SELECT * FROM claim_notifications(1000)")}
    conn.execute("UPDATE setuhaul.notifications SET sending_started_at = now() - interval '10 minutes' WHERE notification_id=%s",
                 (nid,))
    with db.tx("system", scope) as c:
        c.execute("SELECT reconcile_notifications()")
    with db.tx("system", scope) as c:                                         # the old worker reports late
        assert c.execute("SELECT record_send_result(%s,%s,'ACCEPTED','wamid.late',NULL)",
                         (nid, claimed[nid])).fetchone()[0] == "STALE"
    n = note(conn, mid)
    assert n["status"] == "UNKNOWN" and n["provider_message_id"] == "wamid.late"
    with db.tx("ingest") as c:
        assert c.execute("SELECT record_delivery_status('wamid.late',NULL,'DELIVERED',NULL)").fetchone()[0] == "DELIVERED"
    with db.tx("system", scope) as c:                                         # release the others this test claimed
        for other, attempt in claimed.items():
            if other != nid:
                c.execute("SELECT record_send_result(%s,%s,'ACCEPTED',NULL,NULL)", (other, attempt))


def test_concurrent_relays_send_each_notification_once(conn, dsn):
    t = setup_thread(conn)
    mids = [reply(conn, t) for _ in range(6)]
    p = LocalProvider()
    r1, r2 = relay(dsn, p), relay(dsn, p)
    with r1.db.tx("system", r1._scope()) as c:
        c.execute("SELECT enqueue_notifications(1000)")
    ts = [threading.Thread(target=lambda r=r: r.run_once(limit=1000)) for r in (r1, r2)]
    [x.start() for x in ts]
    [x.join(60) for x in ts]
    for m in mids:
        n = note(conn, m)
        assert n["status"] == "SENT" and len(mine(p, n["notification_id"])) == 1


# ================================================================ sources: approvals, takeover
def test_an_approval_outcome_notifies_the_driver_with_a_fixed_template(conn, dsn):
    t, r = pending(conn)
    decide(conn, r["appointment_id"], "REJECT")
    p = LocalProvider()
    drain(relay(dsn, p), 2)
    n = note_by_shipment(conn, t["sid"], "booking_rejected")
    assert n and n["status"] == "SENT" and n["payload"]["outcome"] == "REJECTED"
    # the driver's last message was "haan kar do", so the notice is the reviewed Hinglish one (S4-R2-08)
    assert [d[1] for d in mine(p, n["notification_id"])] == [template_text("booking_rejected", "hi-Latn")]


@pytest.mark.parametrize("said,lang", [("I will be there soon, thanks", "en"),
                                       ("bhai thoda late ho jaunga, slot rakhna", "hi-Latn"),
                                       ("मैं थोड़ा लेट हो जाऊँगा", "hi")])
def test_s4_r2_08_an_outcome_notice_is_sent_in_the_conversations_language(conn, dsn, said, lang):
    t, r = pending(conn)
    driver_says(conn, t, said)                                   # the driver's latest longer message sets the language
    decide(conn, r["appointment_id"], "REJECT")
    p = LocalProvider()
    drain(relay(dsn, p), 2)
    n = note_by_shipment(conn, t["sid"], "booking_rejected")
    sent = [d[1] for d in mine(p, n["notification_id"])]
    assert sent == [template_text("booking_rejected", lang)], sent
    assert lang == "en" or sent[0] != TEMPLATES["booking_rejected"]


def test_s4_r2_08_every_outcome_notice_has_reviewed_hinglish_and_hindi():
    for key, en in TEMPLATES.items():
        hl, hi = template_text(key, "hi-Latn"), template_text(key, "hi")
        assert hl != en and hi != en, key
        assert not DEVANAGARI.search(hl) and DEVANAGARI.search(hi), key


def test_a_person_taking_over_stops_an_unsent_agent_reply(conn, dsn):
    t = setup_thread(conn)
    mid = reply(conn, t)
    conn.execute("UPDATE setuhaul.chat_threads SET human_active=true WHERE thread_id=%s", (t["tid"],))
    p = LocalProvider()
    drain(relay(dsn, p), 2)
    n = note(conn, mid)
    assert (n["status"], n["last_error"]) == ("FAILED", "HUMAN_TOOK_OVER") and mine(p, n["notification_id"]) == []


# ================================================================ receipts through the signed webhook
def test_delivery_receipts_arrive_through_the_signed_webhook(conn, dsn, client):
    t = setup_thread(conn)
    mid = reply(conn, t)
    p = LocalProvider()
    drain(relay(dsn, p), 2)
    n = note(conn, mid)
    pid = n["provider_message_id"]
    body = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": pid, "status": "delivered", "biz_opaque_callback_data": str(n["notification_id"])},
        {"id": "wamid.unknown", "status": "read"}, {"status": "nonsense"}]}}]}]}
    for _ in range(2):                                                         # repeats are harmless
        assert post_signed(client, json.dumps(body).encode()).status_code == 200
    assert note(conn, mid)["status"] == "DELIVERED"


# ================================================================ provider adapter and permissions
def test_whatsapp_adapter_is_honest_about_idempotency_and_maps_outcomes():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        code = {"ok": 200, "busy": 503, "bad": 400}[seen[-1]["text"]["body"]]
        return httpx.Response(code, json={"messages": [{"id": "wamid.X"}]} if code == 200 else {"error": {}})

    wa = WhatsAppCloudProvider("123", "token", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert wa.supports_idempotency is False
    nid = str(uuid.uuid4())
    assert (wa.send("+919800000000", "ok", nid).outcome, seen[-1]["biz_opaque_callback_data"]) == (ACCEPTED, nid)
    assert wa.send("+919800000000", "busy", nid).outcome == AMBIGUOUS              # R3-P2-02: 5xx is ambiguous
    assert wa.send("+919800000000", "bad", nid).outcome == PERMANENT

    def boom(exc):
        def h(req):
            raise exc("x", request=req)
        return WhatsAppCloudProvider("1", "t", client=httpx.Client(transport=httpx.MockTransport(h)))

    assert boom(httpx.ConnectError).send("+91", "ok", nid).outcome == RETRYABLE      # never reached Meta
    with pytest.raises(ProviderTimeout):
        boom(httpx.ReadTimeout).send("+91", "ok", nid)                              # may have reached Meta
    assert AMBIGUOUS == "AMBIGUOUS"


def test_only_the_system_sends_and_only_the_webhook_path_records_receipts(conn):
    fns = {"enqueue_notifications(integer)": {"setuhaul_system"}, "claim_notifications(integer)": {"setuhaul_system"},
           "record_send_result(uuid,integer,text,text,text)": {"setuhaul_system"},
           "reconcile_notifications(interval)": {"setuhaul_system"},
           "record_delivery_status(text,uuid,text,text)": {"setuhaul_ingest"}}
    for fn, allowed in fns.items():
        who = {r[0] for r in conn.execute("""SELECT rolname FROM pg_roles WHERE rolname LIKE 'setuhaul_%%'
                                              AND rolname NOT IN ('setuhaul_rpc')
                                              AND has_function_privilege(oid, %s::regprocedure, 'EXECUTE')""",
                                          (f"setuhaul.{fn}",))}
        assert who == allowed, (fn, who)
    with pytest.raises(psycopg.Error) as e:
        with as_role(conn, "setuhaul_agent", JAI) as c:
            c.execute("UPDATE notifications SET status='SENT'")
    assert e.value.sqlstate == "42501"


# ================================================================ R3-P2-01 a person taking over: retries, ambiguity, after the claim
def _take_over(conn, t):
    conn.execute("UPDATE setuhaul.chat_threads SET human_active=true WHERE thread_id=%s", (t["tid"],))


def test_r3_takeover_after_a_retryable_failure_stops_the_retry(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["retry"])
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "RETRY"
    _take_over(conn, t)
    make_due(conn, n["notification_id"], "next_retry_at")
    drain(r, 1)
    n = note(conn, mid)
    assert (n["status"], n["last_error"]) == ("FAILED", "HUMAN_TOOK_OVER") and mine(p, n["notification_id"]) == []


def test_r3_takeover_after_an_ambiguous_send_is_not_resent_and_a_person_is_told(conn, dsn):
    t = setup_thread(conn)
    mid, p, r = _only_mine(conn, dsn, t, ["timeout"])
    drain(r, 1)
    n = note(conn, mid)
    assert n["status"] == "UNKNOWN" and len(mine(p, n["notification_id"])) == 1   # it may be on the phone already
    _take_over(conn, t)
    make_due(conn, n["notification_id"], "unknown_deadline")
    drain(r, 1)
    n = note(conn, mid)
    assert (n["status"], n["last_error"]) == ("FAILED", "HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND")
    assert len(mine(p, n["notification_id"])) == 1 and escalated(conn, n["notification_id"]) == 1


def test_r3_takeover_after_the_claim_is_fenced_before_the_provider_call(conn, dsn):
    t = setup_thread(conn)
    mid = reply(conn, t)
    with Db(dsn).tx("system", (JAI, "FAC-GGN-01", "CENTRAL")) as c:
        c.execute("SELECT enqueue_notifications()")
    nid = note(conn, mid)["notification_id"]
    conn.execute("UPDATE setuhaul.notifications SET status='SENDING', attempts=attempts+1 WHERE notification_id=%s", (nid,))
    _take_over(conn, t)                                                        # claimed, then a person takes over
    with Db(dsn).tx("system", (JAI, "FAC-GGN-01", "CENTRAL")) as c:
        assert c.execute("SELECT begin_send(%s,1)", (nid,)).fetchone()[0] is False
        assert c.execute("SELECT begin_send(%s,7)", (nid,)).fetchone()[0] is False  # a stale attempt is fenced too
    n = note(conn, mid)
    assert (n["status"], n["last_error"]) == ("FAILED", "HUMAN_TOOK_OVER")


def test_r3_the_relay_sends_only_after_begin_send_agrees(conn, dsn):
    t = setup_thread(conn)
    mid = reply(conn, t)
    p = LocalProvider()
    drain(relay(dsn, p), 1)
    n = note(conn, mid)
    assert n["status"] == "SENT" and len(mine(p, n["notification_id"])) == 1


# ================================================================ R3-P2-02 WhatsApp errors and receipts
def test_r3_a_whatsapp_5xx_is_ambiguous_and_a_429_is_retryable():
    codes = iter([503, 500, 429])

    def handler(req):
        return httpx.Response(next(codes), json={"error": {}})

    wa = WhatsAppCloudProvider("1", "t", client=httpx.Client(transport=httpx.MockTransport(handler)))
    nid = str(uuid.uuid4())
    assert [wa.send("+91", "x", nid).outcome for _ in range(3)] == [AMBIGUOUS, AMBIGUOUS, RETRYABLE]


def test_r3_a_receipt_that_cannot_be_stored_is_not_acknowledged(app, client, monkeypatch):
    def broken(*a, **kw):
        raise psycopg.OperationalError("database unavailable")
    monkeypatch.setattr(app.state.service, "record_delivery_status", broken)
    body = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.x", "status": "delivered", "biz_opaque_callback_data": str(uuid.uuid4())}]}}]}]}
    assert post_signed(client, json.dumps(body).encode()).status_code == 503       # Meta redelivers the batch
