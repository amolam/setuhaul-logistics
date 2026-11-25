"""Driver traceability (Part B): which driver and shipment a turn belonged to, in the three places a turn is seen.

  * LangSmith: the root `agent_turn` run's metadata carries driver_id, shipment_id, facility_id and channel.
  * CloudWatch: the `agent.turn` structured line carries the same ids and the same turn_ref.
  * Console: agent_turn_metrics (migration 0041) stores driver_id and shipment_id on facility rows; the Ops view
    (/console/observability, planner and ops_lead only) shows them and filters by driver.

Every id passes one format gate (setuhaul/observability/identifiers.py, the same rules in 0041). A phone number, a
name, a Telegram chat id, coordinates or message text never reaches a trace, a log line or the metrics table.

No network: the LangSmith exporter is an in-memory capture, the model is the scripted FakeModel, and the database is
the local test cluster (the test_api database).
"""
from __future__ import annotations

import json
import logging
import random
import string
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from setuhaul.agent.models import FakeModel
from setuhaul.agent.orchestrator import Orchestrator
from setuhaul.observability import identifiers as ids
from setuhaul.observability import redaction, tracing
from setuhaul.ops import telemetry
from test_api import GGN, JAI, Driver, H, app, client, dsn, keys, staff_token  # noqa: F401
from test_orchestrator import exd, in_90

TG_CHAT = "5512345678"                       # Telegram-chat-id-shaped (10 digits); must never be exported


# ================================================================ helpers
class Capture(tracing.Exporter):
    def __init__(self):
        self.batches = []

    def post(self, batch):
        self.batches.append(batch)


@pytest.fixture
def traced():
    cap = Capture()
    old = tracing.tracer()
    tracing.install(tracing.Tracer(tracing.TracerConfig(enabled=True, project="setuhaul-dev", environment="ci"), cap))
    yield cap
    tracing.install(old)


def _letters(n=8) -> str:
    """Id suffixes without digits, so a random id can never contain a 7-digit run by chance."""
    return "".join(random.choice(string.ascii_uppercase) for _ in range(n))


def add_named_driver(dsn, facility=JAI, n_shipments=1, name="Ramesh Kumar"):
    """A synthetic driver with a well-formed id (DRV-JAI-XXXXXXXX), a real-looking name and an E.164 phone."""
    u = _letters()
    drv = f"DRV-{'GGN' if facility == GGN else 'JAI'}-{u}"
    phone = f"+91{random.randint(6, 9)}{random.randint(0, 10**9 - 1):09d}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1',%s,%s)",
                  (drv, name, phone))
        sids = []
        for i in range(n_shipments):
            sid, veh = f"SHP-{u}-{i}", f"VEH-{u}-{i}"
            c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}-{i}"))
            c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,
                         destination_facility_id,load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,
                         current_status)
                         VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,now()-interval '3 hours',
                                 now()+make_interval(mins=>%s),'IN_TRANSIT')""",
                      (sid, f"ORD-{u}-{i}", drv, veh, facility, 120 + i * 600))
            sids.append(sid)
    return drv, phone, sids


def run_turn(app, client, dsn, facility=JAI, n_shipments=1, text=None, telegram=False):
    drv, phone, sids = add_named_driver(dsn, facility, n_shipments)
    d = Driver(client, phone)
    ev = d.say(text or f"Traffic. Reaching around {in_90()}. Any slot? Call me on {phone}, Ramesh Kumar, "
                       f"telegram chat {TG_CHAT}, I am at 26.9124,75.7873")
    if telegram:                     # the same turn as if it had arrived over Telegram (channel label only)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SET session_replication_role = replica")         # chat_messages is append-only
            c.execute("UPDATE setuhaul.chat_messages SET channel='TELEGRAM' WHERE message_id=%s", (ev["message_id"],))
    model = FakeModel(extractions=[exd(delay_kind="ARRIVAL_TIME", declared_eta_local=in_90(), exception_type="TRAFFIC")])
    res = Orchestrator(app.state.service, app.state.sessions, model).handle(ev)
    return drv, phone, sids, res


def root_of(cap, turn_ref):
    tracing.tracer().flush(2)
    runs = [x for b in cap.batches for x in b if x["trace_id"] == turn_ref]
    return next(x for x in runs if x["name"] == "agent_turn"), runs


def metrics_row(dsn, turn_ref):
    with psycopg.connect(dsn) as c:
        return c.execute("""SELECT facility_id, driver_id, shipment_id FROM setuhaul.agent_turn_metrics
                             WHERE turn_ref=%s""", (turn_ref,)).fetchone()


def turn_lines(caplog, turn_ref):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "setuhaul.telemetry" and json.loads(r.getMessage()).get("turn_ref") == turn_ref]


# ================================================================ the format gate (unit)
@pytest.mark.parametrize("kind,value", [
    ("driver_id", "DRV-JAI-0042"), ("driver_id", "DRV-TEST-1"), ("driver_id", "SYN-DRV-1a2b3c4d"),
    ("driver_id", "DRV-someone-else"), ("shipment_id", "SHP-TEST-1"), ("shipment_id", "SYN-SHP-1a2b3c4d-0"),
    ("facility_id", "FAC-JAI-01"), ("driver_id", "DRV-202611"),                 # 6 digits: still an id
])
def test_well_formed_ids_pass_the_gate(kind, value):
    assert ids.is_valid(kind, value) and ids.export_id(kind, value) == value and ids.stored_id(kind, value) == value


@pytest.mark.parametrize("kind,value", [
    ("driver_id", "+919876543210"), ("driver_id", "919876543210"), ("driver_id", "9876543210"),   # phones
    ("driver_id", "DRV-9876543210"), ("driver_id", "DRV-+919876543210"),       # a phone dressed as an id
    ("driver_id", TG_CHAT), ("driver_id", f"DRV-{TG_CHAT}"), ("driver_id", "DRV-1234567"),        # chat ids
    ("driver_id", "Ramesh Kumar"), ("driver_id", "DRV Ramesh"), ("driver_id", "ramesh@example.com"),  # names, e-mail
    ("driver_id", "@ramesh_k"), ("driver_id", "26.9124,75.7873"), ("driver_id", "tg9a8b7c"),     # username, coords
    ("driver_id", "SHP-TEST-1"), ("shipment_id", "DRV-TEST-1"), ("facility_id", "JAI-FAC-01"),  # wrong kind
    ("driver_id", "D"), ("driver_id", "DRV-" + "A" * 60), ("driver_id", 42), ("driver_id", ["DRV-X-1"]),
])
def test_malformed_ids_export_as_invalid_and_are_never_stored(kind, value):
    assert not ids.is_valid(kind, value)
    assert ids.export_id(kind, value) == ids.INVALID_ID                        # never the value, never a substitute
    assert ids.stored_id(kind, value) is None


def test_a_missing_id_stays_missing():
    assert ids.export_id("driver_id", None) is None and ids.export_id("shipment_id", "") is None


def test_redaction_gates_ids_at_the_export_boundary_and_still_drops_personal_keys():
    out = redaction.redact({"driver_id": "DRV-JAI-0042", "shipment_id": "+919876543210", "facility_id": "FAC-JAI-01",
                            "thread_shipment_id": "SHP-TEST-1", "nested": {"driver_id": "Ramesh Kumar"},
                            "driver_name": "Ramesh Kumar", "phone": "+919876543210", "chat_id": TG_CHAT})
    assert out["driver_id"] == "DRV-JAI-0042" and out["facility_id"] == "FAC-JAI-01"
    assert out["thread_shipment_id"] == "SHP-TEST-1"
    assert out["shipment_id"] == ids.INVALID_ID and out["nested"]["driver_id"] == ids.INVALID_ID
    assert "driver_name" not in out and "phone" not in out
    assert "chat_id" not in out and TG_CHAT not in json.dumps(out)


def test_cloudwatch_lines_gate_ids_and_drop_telegram_and_location_keys():
    rec = telemetry.emit("agent.turn", None, driver_id="DRV-JAI-0042", shipment_id="DRV-JAI-0042",
                         facility_id=JAI, chat_id=TG_CHAT, telegram_chat_id=TG_CHAT, username="ramesh_k",
                         lat=26.9124, lon=75.7873, geohash="ttnfv2u", driver_name="Ramesh", phone="+919876543210")
    assert rec["driver_id"] == "DRV-JAI-0042" and rec["shipment_id"] == ids.INVALID_ID and rec["facility_id"] == JAI
    blob = json.dumps(rec)
    for leaked in (TG_CHAT, "ramesh_k", "26.9124", "ttnfv2u", "Ramesh", "919876543210"):
        assert leaked not in blob


def test_the_gate_is_not_proof_of_synthetic_data():
    """A well-formed id passes whatever it belongs to: tracing itself must stay synthetic-only."""
    from setuhaul.observability.langsmith import tracer_from_env
    assert ids.is_valid("driver_id", "DRV-REAL-PERSON")                         # format only
    t = tracer_from_env({"SETUHAUL_LANGSMITH_TRACING": "on", "SETUHAUL_ENV": "dev", "LANGSMITH_API_KEY": "lsv2_x" * 4,
                         "SETUHAUL_DATA_CLASS": "real"})
    assert not t.enabled                                                       # real data: tracing refuses to start
    assert "not proof" in ids.__doc__


# ================================================================ a real turn: LangSmith, CloudWatch, console agree
def test_one_driver_id_identifies_the_turn_in_langsmith_cloudwatch_and_the_console(app, client, dsn, traced, caplog):
    with caplog.at_level(logging.INFO, logger="setuhaul.telemetry"):
        drv, phone, (sid,), res = run_turn(app, client, dsn)
    root, runs = root_of(traced, res.turn_ref)
    meta = root["extra"]["metadata"]
    # LangSmith: the root run names the driver, shipment, facility and channel; ids for correlation are unchanged
    assert (meta["driver_id"], meta["shipment_id"], meta["facility_id"], meta["channel"]) == (drv, sid, JAI, "whatsapp")
    assert root["id"] == res.turn_ref == meta["turn_ref"] == meta["correlation_id"]
    assert meta["conversation_ref"] and meta["conversation_ref"] != res.turn_ref   # hashed conversation id kept
    assert len(meta["conversation_ref"]) == 16 and drv not in meta["conversation_ref"]
    # CloudWatch: the agent.turn line carries the same ids under the same turn_ref
    line = next(x for x in turn_lines(caplog, res.turn_ref) if x["event"] == "agent.turn")
    assert (line["driver_id"], line["shipment_id"], line["facility_id"], line["channel"]) == (drv, sid, JAI, "whatsapp")
    assert line["correlation_id"] == res.correlation_id == res.turn_ref
    # console: the metrics row has the same ids
    assert metrics_row(dsn, res.turn_ref) == (JAI, drv, sid)
    # and nothing personal anywhere: phone, name, chat id, coordinates, message text
    blob = json.dumps(runs) + json.dumps(turn_lines(caplog, res.turn_ref))
    for leaked in (phone, phone[1:], phone[3:], "Ramesh", TG_CHAT, "26.9124", "75.7873", "Traffic. Reaching"):
        assert leaked not in blob, leaked


def test_a_telegram_turn_is_labelled_telegram_and_never_carries_the_chat_id(app, client, dsn, traced, caplog):
    with caplog.at_level(logging.INFO, logger="setuhaul.telemetry"):
        drv, phone, _, res = run_turn(app, client, dsn, telegram=True)
    root, runs = root_of(traced, res.turn_ref)
    assert root["extra"]["metadata"]["channel"] == "telegram" and root["extra"]["metadata"]["driver_id"] == drv
    assert TG_CHAT not in json.dumps(runs) + json.dumps(turn_lines(caplog, res.turn_ref))


def test_two_drivers_at_one_facility_stay_apart(app, client, dsn, traced):
    a, _, (sa,), ra = run_turn(app, client, dsn)
    b, _, (sb,), rb = run_turn(app, client, dsn)
    assert a != b and ra.turn_ref != rb.turn_ref
    assert root_of(traced, ra.turn_ref)[0]["extra"]["metadata"]["driver_id"] == a
    assert root_of(traced, rb.turn_ref)[0]["extra"]["metadata"]["driver_id"] == b
    assert metrics_row(dsn, ra.turn_ref) == (JAI, a, sa) and metrics_row(dsn, rb.turn_ref) == (JAI, b, sb)


def test_a_driver_with_several_shipments_and_no_chosen_one_has_no_shipment_id(app, client, dsn, traced, caplog):
    with caplog.at_level(logging.INFO, logger="setuhaul.telemetry"):
        drv, _, sids, res = run_turn(app, client, dsn, n_shipments=2, text="Running late, slot please")
    meta = root_of(traced, res.turn_ref)[0]["extra"]["metadata"]
    assert meta["driver_id"] == drv and meta["shipment_id"] is None             # missing, not guessed
    assert not any(s in json.dumps(meta) for s in sids)
    line = next(x for x in turn_lines(caplog, res.turn_ref) if x["event"] == "agent.turn")
    assert line["driver_id"] == drv and line["shipment_id"] is None
    row = metrics_row(dsn, res.turn_ref)
    if row[0] is None:                       # no facility known yet: the row is visible to every scope, so no driver
        assert row == (None, None, None)
    else:
        assert row[1] == drv and row[2] is None


def test_a_driver_id_that_fails_the_gate_is_exported_as_invalid_and_not_stored(app, client, dsn, caplog):
    """A data defect: the turn names its driver by a phone-shaped id. The log says [INVALID_ID]; the row stores NULL."""
    drv, _, (sid,), _ = run_turn(app, client, dsn)              # a normal turn first: the driver exists
    from setuhaul.agent import orchestrator as om
    res = om.TurnResult(reply=None, facility_id=JAI, driver_id="DRV-9876543210", shipment_id=sid,
                        turn_ref=str(uuid.uuid4()))
    with caplog.at_level(logging.INFO, logger="setuhaul.telemetry"):
        app.state.service.record_turn_metrics(res)
    line = next(x for x in turn_lines(caplog, res.turn_ref) if x["event"] == "agent.turn")
    assert line["driver_id"] == ids.INVALID_ID and "9876543210" not in json.dumps(line)
    assert metrics_row(dsn, res.turn_ref) == (JAI, None, sid)                  # stored as NULL, never the value


# ================================================================ the Ops console: column, filter, roles
def test_ops_view_shows_driver_and_shipment_and_filters_by_driver(app, client, dsn, keys):
    a, _, (sa,), ra = run_turn(app, client, dsn)
    b, _, _, rb = run_turn(app, client, dsn)
    lead = H(staff_token(keys, groups=("ops_lead",)))
    allt = client.get("/console/observability", headers=lead).json()["agent"]["recent_turns"]
    mine = {t["turn_ref"]: t for t in allt}
    assert mine[ra.turn_ref]["driver_id"] == a and mine[ra.turn_ref]["shipment_id"] == sa
    r = client.get("/console/observability", params={"driver": a}, headers=lead)
    assert r.status_code == 200 and r.json()["driver_filter"] == a
    only = r.json()["agent"]["recent_turns"]
    assert only and {t["driver_id"] for t in only} == {a} and rb.turn_ref not in {t["turn_ref"] for t in only}
    planner = client.get("/console/observability", params={"driver": a}, headers=H(staff_token(keys)))
    assert planner.status_code == 200 and {t["driver_id"] for t in planner.json()["agent"]["recent_turns"]} == {a}


@pytest.mark.parametrize("bad", ["+919876543210", "Ramesh Kumar", TG_CHAT, "SHP-TEST-1", "DRV-9876543210"])
def test_the_driver_filter_refuses_anything_that_is_not_a_driver_id(client, keys, bad):
    r = client.get("/console/observability", params={"driver": bad}, headers=H(staff_token(keys, groups=("ops_lead",))))
    assert r.status_code == 422 and bad not in r.text


@pytest.mark.parametrize("group", ["operations", "dispatcher"])
def test_roles_without_ops_metrics_still_get_nothing(client, keys, group):
    r = client.get("/console/observability", params={"driver": "DRV-JAI-0042"}, headers=H(staff_token(keys, groups=(group,))))
    assert r.status_code == 403


def test_another_facility_never_sees_the_driver(app, client, dsn, keys):
    drv, _, _, res = run_turn(app, client, dsn)
    ggn = H(staff_token(keys, facilities=(GGN,)))
    turns = client.get("/console/observability", params={"driver": drv}, headers=ggn).json()["agent"]["recent_turns"]
    assert turns == []
    body = client.get("/console/observability", headers=ggn).text
    assert drv not in body


def test_the_console_page_has_the_driver_column_and_filter():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "setuhaul/api/console_static/assets/app.js").read_text(encoding="utf-8")
    assert "<th>Driver</th><th>Shipment</th>" in js and 'id="obsdriverform"' in js and 'q.set("driver"' in js


# ================================================================ migration 0041
def test_0041_columns_are_nullable_and_old_rows_stay_valid(dsn):
    with psycopg.connect(dsn) as c:
        cols = dict(c.execute("""SELECT column_name, is_nullable FROM information_schema.columns
                                  WHERE table_schema='setuhaul' AND table_name='agent_turn_metrics'
                                    AND column_name IN ('driver_id','shipment_id')""").fetchall())
        assert cols == {"driver_id": "YES", "shipment_id": "YES"}
        cons = dict(c.execute("""SELECT conname, convalidated FROM pg_constraint
                                  WHERE conrelid='setuhaul.agent_turn_metrics'::regclass AND conname LIKE '%_format'""").fetchall())
        assert cons == {"agent_turn_metrics_driver_id_format": True, "agent_turn_metrics_shipment_id_format": True}
        # the 0027-shaped call (no driver keys) still records a row
        ref = str(uuid.uuid4())
        c.execute("SET search_path = setuhaul")
        c.execute("SELECT set_config('app.facility_scope', %s, true)", (JAI,))
        c.execute("SELECT record_agent_turn(%s)", (Jsonb({"turn_ref": ref, "facility_id": JAI, "extractor": "rules",
                                                          "tools": [], "turn_ms": 1}),))
        assert c.execute("SELECT driver_id, shipment_id FROM agent_turn_metrics WHERE turn_ref=%s", (ref,)).fetchone() \
            == (None, None)


@pytest.mark.parametrize("drv,fac,stored", [
    ("DRV-JAI-ABCD", JAI, "DRV-JAI-ABCD"), ("DRV-JAI-ABCD", None, None),       # no facility: never a driver
    ("+919876543210", JAI, None), ("DRV-9876543210", JAI, None), ("Ramesh Kumar", JAI, None), (TG_CHAT, JAI, None),
])
def test_record_agent_turn_stores_only_well_formed_ids_on_facility_rows(dsn, drv, fac, stored):
    ref = str(uuid.uuid4())
    with psycopg.connect(dsn) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("SELECT set_config('app.facility_scope', %s, true)", (JAI,))
        body = {"turn_ref": ref, "extractor": "rules", "tools": [], "turn_ms": 1, "driver_id": drv,
                "shipment_id": "SHP-TEST-1"}
        if fac:
            body["facility_id"] = fac
        c.execute("SELECT record_agent_turn(%s)", (Jsonb(body),))
        row = c.execute("SELECT driver_id, shipment_id FROM agent_turn_metrics WHERE turn_ref=%s", (ref,)).fetchone()
    assert row == (stored, "SHP-TEST-1" if fac else None)


@pytest.mark.parametrize("drv,fac", [("+919876543210", JAI), ("DRV-JAI-ABCD", None), ("DRV-5512345678", JAI)])
def test_the_table_check_refuses_a_bad_id_even_from_a_direct_insert(dsn, drv, fac):
    with psycopg.connect(dsn) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("SELECT set_config('app.facility_scope', %s, true)", (JAI,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("""INSERT INTO agent_turn_metrics (turn_ref, facility_id, extractor, tools, turn_ms, driver_id)
                         VALUES (%s, %s, 'rules', '[]', 1, %s)""", (str(uuid.uuid4()), fac, drv))


def test_0041_changes_no_grant_and_no_owner(dsn):
    with psycopg.connect(dsn) as c:
        owner = c.execute("""SELECT pg_get_userbyid(proowner) FROM pg_proc
                              WHERE proname='record_agent_turn' AND pronamespace='setuhaul'::regnamespace""").fetchone()[0]
        callers = {r[0] for r in c.execute("""SELECT grantee FROM information_schema.routine_privileges
                                               WHERE routine_schema='setuhaul' AND routine_name='record_agent_turn'
                                                 AND privilege_type='EXECUTE'""").fetchall()}
        readers = {r[0] for r in c.execute("""SELECT grantee FROM information_schema.role_table_grants
                                               WHERE table_schema='setuhaul' AND table_name='agent_turn_metrics'
                                                 AND privilege_type='SELECT'""").fetchall()}
    assert owner == "setuhaul_rpc"
    assert callers - {"setuhaul_rpc", "postgres"} == {"setuhaul_feasibility"}
    assert readers - {"setuhaul_rpc", "postgres"} == {"setuhaul_console", "setuhaul_system"}


# ================================================================ existing behaviour unchanged
def test_tracing_off_records_nothing_and_the_turn_still_measures(app, client, dsn):
    old = tracing.tracer()
    tracing.install(tracing.Tracer())                                          # the default: off
    try:
        drv, _, (sid,), res = run_turn(app, client, dsn)
        assert tracing.tracer().stats["turns"] == 0 and tracing.tracer().pending() == 0
        assert res.driver_id == drv and metrics_row(dsn, res.turn_ref) == (JAI, drv, sid)
    finally:
        tracing.install(old)
