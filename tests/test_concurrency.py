"""Per-conversation serialization (migration 0006, review R0.7 P0).

Each test freezes one transaction part-way (it holds the conversation lock and has not committed) and proves the
other one waits, then lands in the safe order:
  * a driver message in flight → the hold waits, then sees the message and refuses the old YES;
  * a hold in flight → the inbound message waits, then lands after it and supersedes the YES for the request;
  * the same through the real service ingest path;
  * a takeover in flight → the hold waits, then refuses;
  * two simultaneous deliveries of one webhook → one stored message, one queue event.
"""
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg

from conftest import as_role
from setuhaul.api.db import Db
from setuhaul.api.security import SessionTokens
from setuhaul.api.service import BookingService
from conftest import new_shipment
from test_db_requests import JAI, confirm, err, hold, k, run, setup_thread, submit

WAIT = 1.0          # how long the blocked side must stay blocked before we release the lock


def open_tx(dsn, role, scope=None, driver=None):
    """A transaction left open on purpose (the 'paused' side)."""
    c = psycopg.connect(dsn)
    c.execute(f"SET LOCAL ROLE {role}")
    c.execute("SET LOCAL search_path = setuhaul")
    if scope is not None:
        c.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
    if driver is not None:
        c.execute("SELECT set_config('app.driver_id', %s, true)", (driver,))
    return c


def ingest_message(c, t, text):
    """What the inbound path does: take the conversation lock, then store the DRIVER message."""
    c.execute("SELECT thread_lock(%s)", (t["tid"],))
    return c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked)
                        VALUES (%s,'DRIVER','WHATSAPP',md5(%s),%s) RETURNING message_id""",
                     (t["tid"], text + uuid.uuid4().hex, text)).fetchone()[0]


def in_thread(fn):
    out = {}

    def run():
        try:
            out["ok"] = fn()
        except Exception as e:                       # noqa: BLE001 — the assertion looks at the message
            out["err"] = e
    th = threading.Thread(target=run)
    th.start()
    return th, out


def test_message_in_flight_makes_the_hold_wait_then_refuse(dsn, conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    paused = open_tx(dsn, "setuhaul_ingest", driver=t["drv"])
    ingest_message(paused, t, "actually wait")                       # stored, not yet committed

    def book():
        with psycopg.connect(dsn, autocommit=True) as c2:
            return hold(c2, t, att)
    th, out = in_thread(book)
    th.join(WAIT)
    assert th.is_alive(), "the hold must wait for the in-flight driver message"
    paused.commit()
    th.join(15)
    assert "CONFIRMATION_SUPERSEDED" in str(out.get("err")), out


def test_hold_in_flight_makes_the_message_wait_then_supersede(dsn, conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    paused = open_tx(dsn, "setuhaul_agent", JAI, t["drv"])
    r = paused.execute("SELECT hold_option(%s,%s,%s,%s,'agent')", (t["oid"], t["tid"], att, k())).fetchone()[0]
    assert r["status"] == "HELD"                                     # holds the conversation lock, not committed

    def message():
        with psycopg.connect(dsn) as c2:
            c2.execute("SET LOCAL ROLE setuhaul_ingest")
            c2.execute("SET LOCAL search_path = setuhaul")
            c2.execute("SELECT set_config('app.driver_id', %s, true)", (t["drv"],))
            mid = ingest_message(c2, t, "cancel it")
            c2.commit()
            return mid
    th, out = in_thread(message)
    th.join(WAIT)
    assert th.is_alive(), "the inbound message must wait for the in-flight hold"
    paused.commit()
    th.join(15)
    assert out.get("ok"), out
    t["att"] = att
    assert "CONFIRMATION_SUPERSEDED" in str(err(submit, conn, t, att))   # the later message wins for the request


def test_service_ingest_waits_for_an_in_flight_hold(dsn, conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    phone = conn.execute("SELECT phone_e164 FROM setuhaul.drivers WHERE driver_id=%s", (t["drv"],)).fetchone()[0]
    svc = BookingService(Db(dsn), lambda: datetime.now(timezone.utc), (JAI, "FAC-GGN-01"))
    sessions = SessionTokens(b"s" * 40)
    paused = open_tx(dsn, "setuhaul_agent", JAI, t["drv"])
    paused.execute("SELECT hold_option(%s,%s,%s,%s,'agent')", (t["oid"], t["tid"], att, k()))
    th, out = in_thread(lambda: svc.ingest(phone, f"wamid.{uuid.uuid4().hex}", "ruko, mat karo", sessions))
    th.join(WAIT)
    assert th.is_alive(), "the real ingest path takes the same lock"
    paused.commit()
    th.join(15)
    assert out.get("ok", {}).get("message_id"), out
    assert "CONFIRMATION_SUPERSEDED" in str(err(submit, conn, t, att))


def test_takeover_in_flight_makes_the_hold_wait_then_refuse(dsn, conn):
    t = setup_thread(conn)
    att = confirm(conn, t)
    paused = open_tx(dsn, "setuhaul_console", JAI)
    paused.execute("SELECT begin_staff_context('staff:ops1',ARRAY['operations'],ARRAY[%s])", (JAI,))
    paused.execute("SELECT take_over_thread(%s,'staff:ops1','driver called in')", (t["tid"],))

    def book():
        with psycopg.connect(dsn, autocommit=True) as c2:
            return hold(c2, t, att)
    th, out = in_thread(book)
    th.join(WAIT)
    assert th.is_alive()
    paused.commit()
    th.join(15)
    e = str(out.get("err"))
    assert "ATTESTATION_EXPIRED" in e or "HUMAN_ACTIVE" in e, out


def test_simultaneous_duplicate_webhooks_store_and_enqueue_once(dsn, conn):
    t = setup_thread(conn)
    phone = conn.execute("SELECT phone_e164 FROM setuhaul.drivers WHERE driver_id=%s", (t["drv"],)).fetchone()[0]
    svc = BookingService(Db(dsn), lambda: datetime.now(timezone.utc), (JAI, "FAC-GGN-01"))
    sessions, ext = SessionTokens(b"s" * 40), f"wamid.{uuid.uuid4().hex}"
    barrier, results = threading.Barrier(4), []

    def deliver():
        barrier.wait()
        results.append(svc.ingest(phone, ext, "late by an hour", sessions))
    ths = [threading.Thread(target=deliver) for _ in range(4)]
    [x.start() for x in ths]
    [x.join(30) for x in ths]
    assert sum(1 for r in results if "session_token" in r) == 1, results
    n = conn.execute("SELECT count(*) FROM setuhaul.chat_messages WHERE external_message_id=%s", (ext,)).fetchone()[0]
    assert n == 1


def test_lock_is_per_conversation_not_global(dsn, conn):
    """A paused conversation never blocks another driver's booking."""
    a, b = setup_thread(conn), setup_thread(conn)
    att_b = confirm(conn, b)
    paused = open_tx(dsn, "setuhaul_ingest", driver=a["drv"])
    ingest_message(paused, a, "hello")
    t0 = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as c2:
        assert hold(c2, b, att_b)["status"] == "HELD"
    assert time.monotonic() - t0 < WAIT
    paused.rollback()



# ---------------------------------------------------------------- B2-13: shipment switching is serialized too
def _two_shipment_thread(conn):
    t = setup_thread(conn)
    s2, _ = new_shipment(conn, JAI, driver=t["drv"])
    return t, s2


def test_switch_in_flight_makes_the_hold_wait_then_refuse(dsn, conn):
    t, s2 = _two_shipment_thread(conn)
    att = confirm(conn, t)
    paused = open_tx(dsn, "setuhaul_agent", JAI, t["drv"])
    assert paused.execute("SELECT bind_thread_shipment(%s,%s,'agent')", (t["tid"], s2)).fetchone()[0]["changed"] is True

    def book():
        with psycopg.connect(dsn, autocommit=True) as c2:
            return hold(c2, t, att)
    th, out = in_thread(book)
    th.join(WAIT)
    assert th.is_alive(), "the hold must wait for the in-flight shipment switch"
    paused.commit()
    th.join(15)
    e = str(out.get("err"))
    # B-WF-2 §4: the YES is bound to the shipment, so a switched conversation makes it invalid
    assert "ATTESTATION_EXPIRED" in e or "OPTION_NOT_FOR_THREAD" in e or "ATTESTATION_INVALID" in e, out
    n = conn.execute("SELECT count(*) FROM setuhaul.dock_occupancy WHERE shipment_id=%s AND kind='HOLD' AND active",
                     (t["sid"],)).fetchone()[0]
    assert n == 0                                                       # no stranded hold


def test_hold_in_flight_makes_the_switch_wait_then_refuse(dsn, conn):
    t, s2 = _two_shipment_thread(conn)
    att = confirm(conn, t)
    paused = open_tx(dsn, "setuhaul_agent", JAI, t["drv"])
    paused.execute("SELECT hold_option(%s,%s,%s,%s,'agent')", (t["oid"], t["tid"], att, k()))

    def switch():
        with psycopg.connect(dsn, autocommit=True) as c2:
            return run(c2, "setuhaul_agent", JAI, t["drv"], "SELECT bind_thread_shipment(%s,%s,'agent')", (t["tid"], s2))
    th, out = in_thread(switch)
    th.join(WAIT)
    assert th.is_alive(), "the switch must wait for the in-flight hold"
    paused.commit()
    th.join(15)
    assert "FINISH_CURRENT_BOOKING_FIRST" in str(out.get("err")), out
    cur = conn.execute("SELECT shipment_id FROM setuhaul.chat_threads WHERE thread_id=%s", (t["tid"],)).fetchone()[0]
    assert cur == t["sid"]                                              # the conversation stayed with the held shipment


def test_switch_rules(dsn, conn):
    t, s2 = _two_shipment_thread(conn)
    other = setup_thread(conn)
    sql = "SELECT bind_thread_shipment(%s,%s,'agent')"
    assert "FORBIDDEN" in str(err(run, conn, "setuhaul_agent", JAI, t["drv"], sql, (t["tid"], other["sid"])))
    assert "SCOPE_INCOMPLETE" in str(err(run, conn, "setuhaul_agent", "FAC-GGN-01", t["drv"], sql, (t["tid"], s2)))
    e = err(run, conn, "setuhaul_agent", JAI, t["drv"], "UPDATE chat_threads SET shipment_id=%s WHERE thread_id=%s", (s2, t["tid"]))
    assert e.sqlstate == "42501"                                          # no direct path any more
    r = run(conn, "setuhaul_agent", JAI, t["drv"], sql, (t["tid"], s2))[0]
    assert r == {"shipment_id": s2, "changed": True, "previous": t["sid"]}


# ---------------------------------------------------------------- R0.9 B2-16 / B2-17 (migration 0010)
def test_b2_16_bind_checks_the_driver_session_itself(conn):
    """The database function, not only the service, refuses a conversation that is not the session driver's."""
    t, s2 = _two_shipment_thread(conn)
    other = setup_thread(conn)                                   # another driver, same facility (so RLS shows the thread)
    for fn, args in (("bind_thread_shipment", (t["tid"], s2)), ("resolve_thread_shipment", (t["tid"], s2))):
        sql = f"SELECT {fn}(%s,%s,'agent')"
        e = str(err(run, conn, "setuhaul_agent", JAI, other["drv"], sql, args))                       # wrong driver:
        assert "FORBIDDEN" in e or "THREAD_UNKNOWN" in e, (fn, e)                    # RLS hides it, or the check refuses
        assert "FORBIDDEN" in str(err(run, conn, "setuhaul_agent", JAI, "", sql, args)), fn             # no driver
    cur = conn.execute("SELECT shipment_id FROM setuhaul.chat_threads WHERE thread_id=%s", (t["tid"],)).fetchone()[0]
    assert cur == t["sid"]


def test_b2_17_resolve_picks_requested_then_bound_then_only_live(conn):
    t, s2 = _two_shipment_thread(conn)
    sql = "SELECT resolve_thread_shipment(%s,%s,'agent')"
    r = run(conn, "setuhaul_agent", JAI, t["drv"], sql, (t["tid"], None))[0]
    assert (r["shipment_id"], r["facility_id"], r["changed"]) == (t["sid"], JAI, False)          # bound one
    assert {x[0] for x in r["live"]} == {t["sid"], s2}
    r = run(conn, "setuhaul_agent", JAI, t["drv"], sql, (t["tid"], s2))[0]
    assert (r["shipment_id"], r["changed"], r["previous"]) == (s2, True, t["sid"])               # requested: bound now
    other = setup_thread(conn)
    assert "FORBIDDEN" in str(err(run, conn, "setuhaul_agent", JAI, t["drv"], sql, (t["tid"], other["sid"])))
    u = setup_thread(conn, with_shipment=False)                                                  # unbound, one live
    r = run(conn, "setuhaul_agent", JAI, u["drv"], sql, (u["tid"], None))[0]
    assert (r["shipment_id"], r["changed"]) == (u["sid"], True)


def test_b2_17_resolve_waits_for_an_inflight_switch_and_returns_the_new_binding(dsn, conn):
    t, s2 = _two_shipment_thread(conn)
    paused = open_tx(dsn, "setuhaul_agent", JAI, t["drv"])
    paused.execute("SELECT bind_thread_shipment(%s,%s,'agent')", (t["tid"], s2))

    def resolve():
        with psycopg.connect(dsn, autocommit=True) as c2:
            return run(c2, "setuhaul_agent", JAI, t["drv"], "SELECT resolve_thread_shipment(%s,NULL,'agent')", (t["tid"],))[0]
    th, out = in_thread(resolve)
    th.join(WAIT)
    assert th.is_alive(), "resolving must wait for the in-flight switch"
    paused.commit()
    th.join(15)
    assert out["ok"]["shipment_id"] == s2 and out["ok"]["changed"] is False, out   # authoritative, not the stale read


def test_b2_17_options_for_a_shipment_the_thread_left_are_refused(conn):
    t, s2 = _two_shipment_thread(conn)
    run(conn, "setuhaul_agent", JAI, t["drv"], "SELECT bind_thread_shipment(%s,%s,'agent')", (t["tid"], s2))
    e = err(run, conn, "setuhaul_feasibility", JAI, None,
            """INSERT INTO shown_options VALUES (gen_random_uuid(),%s,%s,'DOCK-JAI-D1',
               tstzrange(now()+interval '3h', now()+interval '4h','[)'),false,1,0,now()+interval '5 min',now())""",
            (t["tid"], t["sid"]))
    assert "STALE_SHIPMENT" in str(e)
    run(conn, "setuhaul_feasibility", JAI, None,                                         # the bound shipment is fine
        """INSERT INTO shown_options VALUES (gen_random_uuid(),%s,%s,'DOCK-JAI-D1',
           tstzrange(now()+interval '3h', now()+interval '4h','[)'),false,1,0,now()+interval '5 min',now())""",
        (t["tid"], s2))


def test_s4_07_the_audit_principal_comes_from_the_session(conn):
    t, s2 = _two_shipment_thread(conn)
    run(conn, "setuhaul_agent", JAI, t["drv"], "SELECT bind_thread_shipment(%s,%s,'staff:someone-else')", (t["tid"], s2))
    who, before = conn.execute("""SELECT principal, before_state FROM setuhaul.booking_events
                                   WHERE action='BIND_THREAD_SHIPMENT' AND shipment_id=%s ORDER BY 1 DESC LIMIT 1""",
                               (s2,)).fetchone()
    assert who == f"driver:{t['drv']}" and before["claimed_principal"] == "staff:someone-else"

