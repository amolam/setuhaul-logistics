"""Facility approval mode (migration 0047), end to end on the dispatcher path: in AUTO a dispatcher's accepted offer is
still a planner's decision (0035), and a driver's own eligible selection in the same facility confirms at once."""
from __future__ import annotations

from datetime import datetime, timedelta

from test_api import IST, Driver
from test_dispatch_offers import (JAI, add_driver, app, appointments, client, dsn, keys, link, offer,  # noqa: F401
                                  offer_status, planner, q)


def _mode(client, keys, mode, reason):
    cur = client.get(f"/console/facilities/{JAI}/approval-mode", headers=planner(keys)).json()
    r = client.put(f"/console/facilities/{JAI}/approval-mode", headers=planner(keys),
                   json={"mode": mode, "expected_version": cur["version"], "reason": reason})
    assert r.status_code == 200, r.text
    return r.json()


def test_auto_keeps_dispatcher_offers_with_a_planner_and_confirms_a_drivers_own_selection(client, dsn, keys):
    _mode(client, keys, "AUTO", "auto approval trial")
    try:
        d = add_driver(dsn)
        tg = link(client, keys, dsn, d["drv"])
        o = offer(client, keys, d)
        assert "with the warehouse planner" in tg.press(o["offer_id"], "A")
        (appt, st), = appointments(dsn, d["sid"])
        assert st == "PENDING_CONFIRMATION" and offer_status(dsn, o["offer_id"]) == "ACCEPTED"
        kind, ev = q(dsn, "SELECT kind, evidence FROM approval_requests WHERE appointment_id=%s", appt)[0]
        assert kind == "PLANNER_REVIEW" and ev["approval_mode"] == "AUTO" and ev["dispatcher_offer"] is True
        assert q(dsn, "SELECT count(*) FROM booking_events WHERE action='AUTO_CONFIRM_BOOKING' AND shipment_id=%s",
                 d["sid"])[0][0] == 0

        # a driver's own selection, no mandatory approval: confirmed at once, and the driver is told so
        e = add_driver(dsn)
        drv = Driver(client, e["phone"])
        drv.say("Running 90 min late")
        eta = (datetime.now(IST) + timedelta(minutes=90)).isoformat()
        assert drv.tool("record_driver_report", message_id=drv.last_msg, declared_eta=eta, eta_precision="PRECISE",
                        delay_reason="TRAFFIC")["status"] == "OK"
        opt = drv.tool("find_feasible_options")["data"]["options"][0]["option_id"]
        assert drv.tool("select_option", option_id=opt)["status"] == "OK"
        drv.say("haan ji, kar do")
        assert drv.tool("confirm_selection", message_id=drv.last_msg)["data"]["verdict"] == "YES"
        assert drv.tool("hold_option", option_id=opt)["data"]["status"] == "HELD"
        sub = drv.tool("submit_booking_request", option_id=opt)
        assert sub["status"] == "OK" and sub["data"]["status"] == "CONFIRMED", sub
        st = drv.tool("get_booking_status")["data"]["appointments"][0]
        assert (st["status"], st["current"]) == ("CONFIRMED", True)
        assert q(dsn, "SELECT count(*) FROM approval_requests WHERE shipment_id=%s", e["sid"])[0][0] == 0
    finally:
        _mode(client, keys, "MANUAL", "end of trial")
