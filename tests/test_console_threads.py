"""Read-only conversation views for the hosted staff console: GET /console/threads and /console/threads/{id}.

Same staff authentication as every /console route (Cognito access token, staff group), facility isolation by the
database's row-level security (the console role, the person's facility scope), no writes, masked output.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import psycopg
import pytest

from test_api import GGN, H, IST, JAI, Driver, add_driver, app, client, dsn, keys, staff_token  # noqa: F401 (fixtures)


def converse(client, dsn, facility=JAI):
    """A real conversation: signed webhook in, the agent's tools record the delay and compute options."""
    drv, phone, (sid,) = add_driver(dsn, facility)
    d = Driver(client, phone)
    d.say("Running 90 min late, traffic")
    eta = (datetime.now(IST) + timedelta(minutes=90)).isoformat()
    assert d.tool("record_driver_report", message_id=d.last_msg, declared_eta=eta, eta_precision="PRECISE",
                  delay_reason="TRAFFIC")["status"] == "OK"
    assert d.tool("find_feasible_options")["status"] == "OK"
    return d, phone, sid


def rows(dsn, sql, *args):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_staff_see_their_facility_conversations_masked_and_newest_first(client, dsn, keys):
    d, phone, sid = converse(client, dsn, JAI)
    r = client.get("/console/threads", headers=H(staff_token(keys, facilities=(JAI,))))
    assert r.status_code == 200
    mine = next(t for t in r.json()["threads"] if t["thread_id"] == d.thread)
    assert mine["facility_id"] == JAI and mine["shipment_id"] == sid
    assert mine["driver_phone"].endswith(phone[-4:]) and mine["driver_phone"] != phone and "•" in mine["driver_phone"]
    assert phone not in r.text                                                     # the full number never leaves
    times = [t["last_message_at"] for t in r.json()["threads"]]
    assert times == sorted(times, reverse=True)


def test_the_detail_is_what_the_database_holds(client, dsn, keys):
    d, phone, sid = converse(client, dsn, JAI)
    r = client.get(f"/console/threads/{d.thread}", headers=H(staff_token(keys, facilities=(JAI,))))
    assert r.status_code == 200, r.text
    b = r.json()
    db_msgs = rows(dsn, """SELECT message_id, sender_type, body_masked FROM setuhaul.chat_messages
                            WHERE thread_id = %s ORDER BY created_at, message_id""", d.thread)
    assert [(m["message_id"], m["sender"], m["body_masked"]) for m in b["messages"]] == [tuple(x) for x in db_msgs]
    db_opts = rows(dsn, "SELECT option_id::text FROM setuhaul.shown_options WHERE thread_id = %s", d.thread)
    assert db_opts and {o["option_id"] for o in b["options"]} == {x[0] for x in db_opts}
    assert all(o["dock_code"] and o["start"] for o in b["options"])
    db_eta = rows(dsn, "SELECT derived_eta FROM setuhaul.eta_signals WHERE shipment_id = %s", sid)
    assert len(b["extracted"]["eta_signals"]) == min(len(db_eta), 5) and db_eta
    db_notes = rows(dsn, "SELECT notification_id::text, status FROM setuhaul.notifications WHERE thread_id = %s", d.thread)
    assert {(n["notification_id"], n["status"]) for n in b["notifications"]} == {tuple(x) for x in db_notes}
    assert b["thread"]["facility_id"] == JAI and b["flow"]["state"] is not None
    assert phone not in r.text and "payload" not in r.text and "recipient_ref" not in r.text


def test_another_facility_cannot_list_or_open_the_conversation(client, dsn, keys):
    d, _, _ = converse(client, dsn, JAI)
    ggn = H(staff_token(keys, facilities=(GGN,)))
    assert d.thread not in {t["thread_id"] for t in client.get("/console/threads?limit=50", headers=ggn).json()["threads"]}
    other = client.get(f"/console/threads/{d.thread}", headers=ggn)
    unknown = client.get("/console/threads/T-does-not-exist", headers=ggn)
    assert other.status_code == unknown.status_code == 404                 # outside scope looks like absent
    assert other.json() == unknown.json()
    both = H(staff_token(keys, facilities=(JAI, GGN)))
    assert client.get(f"/console/threads/{d.thread}", headers=both).status_code == 200


def test_no_scope_sees_nothing_and_no_staff_group_is_refused(client, dsn, keys):
    d, _, _ = converse(client, dsn, JAI)
    none = H(staff_token(keys, facilities=()))
    assert client.get("/console/threads", headers=none).json()["threads"] == []
    assert client.get(f"/console/threads/{d.thread}", headers=none).status_code == 404
    for h in ({}, {"Authorization": "Bearer nonsense"}, H(staff_token(keys, groups=("drivers",))),
              H(staff_token(keys, groups=("facility-FAC-JAI-01",)))):            # a facility group is not a role
        assert client.get("/console/threads", headers=h).status_code == 401
        assert client.get(f"/console/threads/{d.thread}", headers=h).status_code == 401


def test_pages_do_not_overlap_and_the_size_is_capped(client, dsn, keys):
    for _ in range(3):
        converse(client, dsn, GGN)
    h = H(staff_token(keys, facilities=(GGN,)))
    p1 = client.get("/console/threads?limit=2", headers=h).json()
    assert len(p1["threads"]) == 2 and p1["next_before"]
    p2 = client.get("/console/threads", params={"limit": 2, "before": p1["next_before"]}, headers=h).json()
    assert p2["threads"] and not {t["thread_id"] for t in p1["threads"]} & {t["thread_id"] for t in p2["threads"]}
    assert client.get("/console/threads?limit=51", headers=h).status_code == 422
    assert client.get("/console/threads?limit=0", headers=h).status_code == 422
    assert client.get("/console/threads/" + "x" * 81, headers=h).status_code == 422


def test_reading_changes_nothing(client, dsn, keys):
    d, _, _ = converse(client, dsn, JAI)
    tables = ("chat_threads", "chat_messages", "thread_flow", "thread_facts", "shown_options", "option_selections",
              "notifications", "outbox", "appointments", "approval_requests", "escalations")

    def snapshot():
        with psycopg.connect(dsn) as c:
            return {t: c.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                                 f"FROM setuhaul.{t} x").fetchone() for t in tables}
    before = snapshot()
    h = H(staff_token(keys, facilities=(JAI, GGN)))
    for _ in range(2):
        assert client.get("/console/threads", headers=h).status_code == 200
        assert client.get(f"/console/threads/{d.thread}", headers=h).status_code == 200
    assert snapshot() == before


def test_delivery_kind_and_phone_masking():
    from setuhaul.api.service import BookingService as S
    assert S._delivery("testsink.0b7f") == "test_sink" and S._delivery("wamid.HBg") == "provider" and S._delivery(None) is None
    assert S._mask_phone("+99900001234") == "+999••••1234" and S._mask_phone(None) is None


def test_the_test_sink_evidence_is_the_stored_record(client, dsn, keys):
    """A notice the outbound relay hands to the internal test sink shows as SENT with its test-sink reference;
    a queued one is not shown as delivered."""
    from setuhaul.api.db import Db
    from setuhaul.notify.outbound import OutboundRelay, TestSinkProvider
    from test_approvals import pending
    from test_db_requests import decide
    with psycopg.connect(dsn) as conn:
        t, r = pending(conn)
        decide(conn, r["appointment_id"], "REJECT")               # a driver notice is now owed
        tid = conn.execute("SELECT thread_id FROM setuhaul.chat_threads WHERE shipment_id = %s", (t["sid"],)).fetchone()
    if tid is None:
        pytest.skip("this fixture's thread has no conversation row")
    h = H(staff_token(keys, facilities=(JAI, GGN)))
    OutboundRelay(Db(dsn), TestSinkProvider()).run_once(limit=1000)
    OutboundRelay(Db(dsn), TestSinkProvider()).run_once(limit=1000)
    b = client.get(f"/console/threads/{tid[0]}", headers=h).json()
    stored = {x[0]: (x[1], x[2]) for x in rows(dsn, """SELECT notification_id::text, status, provider_message_id
                                                        FROM setuhaul.notifications WHERE thread_id = %s""", tid[0])}
    for n in b["notifications"]:
        status, pid = stored[n["notification_id"]]
        assert n["status"] == status
        assert (n["delivery"] == "test_sink") == bool(pid and pid.startswith("testsink."))
        assert n["delivery"] != "test_sink" or n["test_sink_reference"] == pid
    assert json.dumps(b).count("payload") == 0
