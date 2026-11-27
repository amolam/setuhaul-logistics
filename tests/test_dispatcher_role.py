"""Migration 0037: the dedicated `dispatcher` role (Cognito group `dispatcher`).

A dispatcher sends offers, links / unlinks Telegram, reassigns and requests cancellations. A dispatcher decides
nothing: no booking request, no PLANNER_REVIEW, no cancellation; no schedule or operations-health board. `operations`
keeps its permissions (compatibility). Checked at the API and, independently, in the database.

Own database (setuhaul_build_dispatch3): may run beside the other dispatch modules. Nothing leaves the process.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from setuhaul.api.app import Settings, create_app
from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET, H, keys, staff_token  # noqa: F401
from test_dispatch_offers import TG_SECRET, Tg, add_driver, appointments, make_database, offer_status, planner, q


@pytest.fixture(scope="module")
def dsn():
    return make_database("setuhaul_build_dispatch3")


@pytest.fixture(scope="module")
def client(dsn, keys):
    return TestClient(create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                                          session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                                          facilities=(JAI, GGN), jwks=keys["jwks"], booking_enabled=True,
                                          local_test_harness=True, telegram_webhook_secret=TG_SECRET)))


def disp(keys, fac=JAI):
    return H(staff_token(keys, groups=("dispatcher",), facilities=(fac,)))


def lead(keys, fac=JAI):
    return H(staff_token(keys, groups=("ops_lead",), facilities=(fac,)))


def link_as(client, headers, drv) -> Tg:
    r = client.post(f"/console/drivers/{drv}/telegram-link-code", headers=headers)
    assert r.status_code == 200, r.text
    tg = Tg(client)
    assert "linked to SetuHaul" in tg.text(f"/start {r.json()['code']}")
    return tg


def offer_as(client, headers, d):
    r = client.get(f"/console/dispatch/shipments/{d['sid']}/options", headers=headers)
    assert r.status_code == 200, r.text
    o = r.json()["options"][0]
    r = client.post("/console/dispatch/offers", headers=headers,
                    json={"shipment_id": d["sid"], "driver_id": d["drv"], "vehicle_id": d["veh"],
                          "dock_code": o["dock_code"], "start": o["start"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()


def test_console_me_for_a_dispatcher(client, keys):
    me = client.get("/console/me", headers=disp(keys)).json()
    assert me["roles"] == ["dispatcher"]
    perms = me["permissions"]
    # 0048 (owner decisions D1, D3, Q2; 27 Nov 2026): the dispatcher decides after a no-show, asks a planner for a new
    # time and lists free drivers; it still records no gate, dock or completion step (D1) and approves nothing
    assert {a for a, ok in perms.items() if ok} == {"view_dispatch", "send_offer", "link_driver", "request_cancellation",
                                                    "resolve_no_show", "request_reschedule", "view_free_drivers"}
    assert not any(perms[a] for a in ("record_arrival", "record_dock", "record_completion", "decide", "view_controls"))


def test_dispatcher_runs_the_booking_journey_but_cannot_approve_it(client, dsn, keys):
    d = add_driver(dsn)
    tg = link_as(client, disp(keys), d["drv"])
    o = offer_as(client, disp(keys), d)
    assert q(dsn, "SELECT offered_by_role FROM dispatch_offers WHERE offer_id=%s", o["offer_id"])[0][0] == "dispatcher"
    assert "planner" in tg.press(o["offer_id"], "A")
    assert offer_status(dsn, o["offer_id"]) == "ACCEPTED"
    (appt, status), = appointments(dsn, d["sid"])
    assert status == "PENDING_CONFIRMATION"
    r = client.post(f"/console/requests/{appt}/decision", headers=disp(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403, r.text
    assert appointments(dsn, d["sid"]) == [(appt, "PENDING_CONFIRMATION")]
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text

    # cancellation: the dispatcher requests, cannot decide; a planner rejects, then the appointment stays confirmed
    r = client.post(f"/console/appointments/{appt}/cancellation-requests", headers=disp(keys),
                    json={"reason_code": "DRIVER_UNAVAILABLE", "note": "driver sick", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    r = client.post(f"/console/cancellation-requests/{rid}/decision", headers=disp(keys), json={"decision": "APPROVE"})
    assert r.status_code == 403, r.text
    r = client.post(f"/console/cancellation-requests/{rid}/decision", headers=planner(keys), json={"decision": "REJECT"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED", r.text
    assert appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")]

    # reassignment of the confirmed appointment by the dispatcher
    other = add_driver(dsn)
    tnew = link_as(client, disp(keys), other["drv"])
    r = client.post("/console/dispatch/reassignments", headers=disp(keys),
                    json={"shipment_id": d["sid"], "driver_id": other["drv"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    assert "now assigned to you" in tnew.press(r.json()["offer_id"], "A")
    assert q(dsn, "SELECT driver_id FROM shipments WHERE shipment_id=%s", d["sid"])[0][0] == other["drv"]
    assert appointments(dsn, d["sid"]) == [(appt, "CONFIRMED")]

    # and unlinks a driver
    r = client.post(f"/console/drivers/{other['drv']}/telegram-link/revoke", headers=disp(keys),
                    json={"reason": "phone lost"})
    assert r.status_code == 200, r.text
    assert "not linked" in tnew.text("hello").lower() or "link" in tnew.text("hello").lower()


@pytest.mark.parametrize("method,path", [
    ("GET", f"/console/facilities/{JAI}/schedule"),
    ("GET", "/console/ops/health"),
    ("GET", "/console/ops/notifications"),
    ("GET", "/console/cancellation-requests"),
    ("POST", "/console/booking-kill-switch"),
])
def test_dispatcher_has_no_planner_or_operations_board(client, keys, method, path):
    r = client.request(method, path, headers=disp(keys), json={} if method == "POST" else None)
    assert r.status_code in (403, 422), (path, r.status_code, r.text)
    if r.status_code == 422:      # body validation ran first: confirm the role check refuses a well-formed body too
        r = client.request(method, path, headers=disp(keys), json={"engaged": True, "reason": "test only, refused"})
        assert r.status_code == 403, (path, r.text)


def _as_console(dsn, roles, sql, *args, db_role="setuhaul_console"):
    with psycopg.connect(dsn) as c:
        c.execute("SET search_path = setuhaul")
        if db_role:
            c.execute(f"SET ROLE {db_role}")
        c.execute("SELECT begin_staff_context(%s, %s, %s)", ("staff:db-check", list(roles), [JAI]))
        return c.execute(sql, args).fetchall()


def test_database_refuses_a_dispatcher_decision_independently_of_the_api(client, dsn, keys):
    d = add_driver(dsn)
    tg = link_as(client, lead(keys), d["drv"])
    o = offer_as(client, lead(keys), d)
    tg.press(o["offer_id"], "A")
    (appt, _), = appointments(dsn, d["sid"])
    with pytest.raises(psycopg.errors.RaiseException, match="ROLE_NOT_ALLOWED"):
        _as_console(dsn, ["dispatcher"], "SELECT decide_booking_request(%s, 'CONFIRM', %s)", appt, uuid.uuid4().hex)


def test_database_staff_context_accepts_dispatcher_and_still_rejects_unknown_roles(dsn):
    assert _as_console(dsn, ["dispatcher"], "SELECT 1") == [(1,)]                 # the console role may open it
    with pytest.raises(psycopg.errors.RaiseException, match="BAD_STAFF_CONTEXT"):
        _as_console(dsn, ["dispatch-admin"], "SELECT 1")
    # what the definer functions see (read as the owner: the console role cannot call these helpers directly)
    assert _as_console(dsn, ["dispatcher"], "SELECT (staff_ctx()).roles", db_role=None)[0][0] == ["dispatcher"]
    assert _as_console(dsn, ["dispatcher", "ops_lead"],                            # the most senior role is recorded
                       "SELECT staff_role_for(staff_ctx(), ARRAY['dispatcher','operations','ops_lead'], 'x')",
                       db_role=None)[0][0] == "ops_lead"
    with pytest.raises(psycopg.errors.RaiseException, match="ROLE_NOT_ALLOWED"):
        _as_console(dsn, ["dispatcher"], "SELECT staff_role_for(staff_ctx(), ARRAY['planner','ops_lead'], 'x')",
                    db_role=None)
    # approval rules never admit a dispatcher (the table's CHECK does not even allow the value)
    assert q(dsn, "SELECT count(*) FROM approval_role_rules WHERE role='dispatcher'") == [(0,)]


def test_operations_keeps_dispatch_permissions_for_compatibility(client, keys):
    perms = client.get("/console/me", headers=H(staff_token(keys, groups=("operations",)))).json()["permissions"]
    assert all(perms[a] for a in ("view_dispatch", "send_offer", "link_driver", "request_cancellation", "view_ops"))
    assert not perms["decide_cancellation"]
