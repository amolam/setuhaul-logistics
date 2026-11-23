"""Defend: the recorded scenario run (challenge brief p.5, "Your scenario run must show", items 1-6 and optional 7).

One continuous run through the integrated system on synthetic data:
- the real signed webhook, API, database roles and functions, feasibility engine, orchestrator and approval workflow
  port;
- the production rules reader (no model, no paid call);
- WhatsApp is the local provider and Step Functions is the local workflow;
- the booking path runs under the local test harness, exactly as the flow tests do. The deployed switch stays OFF.

The transcript is written to docs/challenge/evidence/scenario_run.json only when SETUHAUL_RECORD_SCENARIO=1, so an
ordinary suite run never rewrites evidence.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

from setuhaul.agent.orchestrator import Orchestrator
from test_api import IST, JAI, Driver, add_driver, app, client, dsn, keys  # noqa: F401
from test_flows import World

OUT = Path(__file__).resolve().parents[1] / "docs" / "challenge" / "evidence" / "scenario_run.json"


class Log:
    def __init__(self):
        self.t0, self.rows = datetime.now(IST), []

    def turn(self, item, who, said, r=None, note=None):
        row = {"t_ms": int((datetime.now(IST) - self.t0).total_seconds() * 1000), "item": item, "driver": who,
               "driver_says": said}
        if r is not None:
            row |= {"agent_replies": r.reply, "path": r.path}
        if note:
            row["evidence"] = note
        self.rows.append(row)
        return r


def one(dsn_, sql, *a):
    with psycopg.connect(dsn_, autocommit=True) as c:
        return c.execute(sql, a).fetchone()


def all_rows(dsn_, sql, *a):
    with psycopg.connect(dsn_, autocommit=True) as c:
        return c.execute(sql, a).fetchall()


def heavy_driver(dsn_):
    """A truck whose load no Jaipur dock can take (weight above every dock's limit): no safe automatic outcome."""
    u = uuid.uuid4().hex[:8]
    drv, phone, sid, veh = f"DRV-{u}", f"+91{uuid.uuid4().int % 10**10:010d}", f"SHP-{u}-0", f"VEH-{u}"
    with psycopg.connect(dsn_, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)", (drv, phone))
        c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,60000,false)", (veh, f"REG-{u}"))
        c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                     load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                     VALUES (%s,%s,'CAR1',%s,%s,%s,55000,45,now()-interval '3 hours',now()+interval '2 hours','IN_TRANSIT')""",
                  (sid, f"ORD-{u}", drv, veh, JAI))
    return phone, sid


def test_scenario_run_shows_all_six_items_and_ends_in_an_escalation(app, client, dsn, keys):
    log, world = Log(), World(dsn)
    orch = Orchestrator(app.state.service, app.state.sessions, model=None)       # the production rules reader
    hm = lambda m: (datetime.now(IST) + timedelta(minutes=m)).strftime("%H:%M")

    def say(item, who, d, text, note=None):
        r = log.turn(item, who, text, orch.handle(d.say(text)), note)
        print(f"[{item}] {who}: {text!r} -> {r.path} {r.reply!r}")
        return r

    # 1. A reports a delay without an arrival time: the agent asks once; the answer is enough to plan
    _, pa, _ = add_driver(dsn)
    a = Driver(client, pa)
    r = say(1, "A", a, "Stuck in traffic near Neemrana, running late")
    assert r.path == ["ask:declared_eta_local"] and "What time" in r.reply
    eta = hm(150)
    r = say(1, "A", a, f"around {eta}")
    assert "options" in r.path and r.reply.startswith("Available slots") and "Reply with the number" in r.reply
    first_list = r.reply

    # 2. A asks for later options and compares them with the first list
    later = hm(300)
    r = say(2, "A", a, f"anything after {later}?")
    assert "options" in r.path and r.reply.startswith("Available slots") and r.reply != first_list
    log.rows[-1]["evidence"] = {"first_list": first_list, "later_list": r.reply, "asked_after": later}

    # 3. B and C report delays at the same facility for the same time: one schedule, several requests
    _, pb, _ = add_driver(dsn)
    _, pc, _ = add_driver(dsn)
    b, c = Driver(client, pb), Driver(client, pc)
    rb = say(3, "B", b, f"Tyre puncture, reaching gate at {eta}, any slot?")
    rc = say(3, "C", c, f"Late due to rain, reaching at {eta}, need a slot")
    assert rb.reply.splitlines()[1] == rc.reply.splitlines()[1]         # both were offered the same first slot
    contested = rb.reply.splitlines()[1].split(". ", 1)[1]              # e.g. "Dock D1, 4:00 AM on 23 Nov"

    # 4. B and C both choose it: one request, one refusal
    say(4, "B", b, "1")
    r = say(4, "B", b, "YES")
    assert r.path == ["confirmation", "book"] and "NOT confirmed" in r.reply
    say(4, "C", c, "1")

    # 5. for C the option disappeared between offer and confirmation: told plainly, offered what is left
    r = say(5, "C", c, "YES")
    assert r.reply.startswith("That slot was just taken.") and contested not in r.reply and "Available slots" in r.reply
    held = all_rows(dsn, """SELECT a.shipment_id, a.status FROM setuhaul.appointments a JOIN setuhaul.docks d USING (dock_id)
                             WHERE d.facility_id=%s AND a.status IN ('PENDING_CONFIRMATION','CONFIRMED','HELD')
                               AND d.dock_code=%s AND to_char(lower(a.during) AT TIME ZONE 'Asia/Kolkata','FMHH12:MI AM')=%s""",
                    JAI, contested.split(",")[0].split()[-1], contested.split(", ")[1].split(" on ")[0])
    assert len(held) == 1, held
    log.rows[-1]["evidence"] = {"contested_slot": contested, "active_appointments_on_that_slot": [list(x) for x in held]}

    # the warehouse planner decides B's request in the console; B's phone gets the outcome
    appt = one(dsn, """SELECT a.appointment_id FROM setuhaul.appointments a JOIN setuhaul.chat_threads t USING (shipment_id)
                        WHERE t.thread_id=%s ORDER BY a.created_at DESC LIMIT 1""", b.thread)[0]
    from test_flows import decide_http
    world.tick()
    d = decide_http(client, keys, appt, "CONFIRM")
    assert d.status_code == 200, d.text
    world.tick()
    st = one(dsn, "SELECT status FROM setuhaul.appointments WHERE appointment_id=%s", appt)[0]
    assert st == "CONFIRMED"
    log.turn(4, "planner", "CONFIRM in the staff console", note={"appointment": str(appt), "status": st,
                                                               "b_phone_shows": world.phone(pb)})

    # 7 (optional). After these ETA updates the planner asks the facility scheduling tool for a new sequence. It is a
    # PROPOSAL: it changes nothing, and B's confirmed slot stays exactly as booked.
    from test_api import H, staff_token
    before = all_rows(dsn, "SELECT appointment_id, status, dock_id, lower(during) FROM setuhaul.appointments ORDER BY 1")
    pr = client.get(f"/console/facilities/{JAI}/schedule-proposal?rule=FACILITY_SCORE", headers=H(staff_token(keys)))
    assert pr.status_code == 200, pr.text
    prop = pr.json()
    assert prop["status"] == "PROPOSAL" and prop["not_a_booking"] is True and len(prop["input_fingerprint"]) == 64
    ours = {x["shipment_id"]: x for x in prop["assignments"]}
    b_sid = one(dsn, "SELECT shipment_id FROM setuhaul.chat_threads WHERE thread_id=%s", b.thread)[0]
    assert b_sid in ours                                                # the confirmed truck is in the plan
    assert all_rows(dsn, "SELECT appointment_id, status, dock_id, lower(during) FROM setuhaul.appointments ORDER BY 1") == before
    log.turn(7, "planner", "GET schedule proposal (FACILITY_SCORE)", note={
        "status": prop["status"], "not_a_booking": prop["not_a_booking"], "metrics": prop["metrics"],
        "sequence": [{k: x[k] for k in ("seq", "shipment_id", "action", "dock_code", "start", "reason")}
                     for x in prop["assignments"]],
        "unscheduled": prop["unscheduled"], "appointments_changed": 0})

    # 6. H's truck fits no dock at this facility: no safe automatic outcome, so a person gets a complete hand-off
    ph, sid = heavy_driver(dsn)
    h = Driver(client, ph)
    r = say(6, "H", h, f"Running late, reaching at {eta}, any slot?")
    assert "escalated:NO_FEASIBLE_SLOT" in r.path and "coordinator" in r.reply.lower()
    esc = one(dsn, """SELECT reason_code, mode, status, handoff FROM setuhaul.escalations WHERE shipment_id=%s
                       ORDER BY created_at DESC LIMIT 1""", sid)
    assert esc[0] == "NO_FEASIBLE_SLOT" and esc[2] == "OPEN" and esc[3]
    log.rows[-1]["evidence"] = {"escalation": {"reason_code": esc[0], "mode": esc[1], "status": esc[2], "handoff": esc[3]}}

    assert log.rows[-1]["item"] == 6                                     # the run ends in an escalation
    if os.environ.get("SETUHAUL_RECORD_SCENARIO") == "1":
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"recorded_at": log.t0.isoformat(timespec="seconds"),
                                   "reader": "production rules reader (no model call)",
                                   "booking": "local test harness; the deployed switch stays OFF",
                                   "turns": log.rows}, indent=1, default=str, ensure_ascii=False) + "\n",
                       encoding="utf-8")


def test_a_zero_padded_24_hour_time_keeps_its_morning_meaning():
    """Found by this scenario run (23 Nov 2026): "anything after 05:26" was read as 17:26."""
    from setuhaul.agent.rules_extractor import extract
    assert extract("anything after 05:26?").earliest_start_local == "05:26"
    assert extract("before 06:00 gate out").latest_finish_local == "06:00"
    assert extract("after 5").earliest_start_local == "17:00"           # a driver's "after 5" is still the evening
    assert extract("anything after 5:26?").earliest_start_local == "17:26"
