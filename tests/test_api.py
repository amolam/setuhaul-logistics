"""Step 2 acceptance tests: the HTTP API end to end against PostgreSQL.

Backlog items closed here (docs/2-design/08_signoff_and_backlog.md §4):
  B-API-1  RBAC / ABAC: a GGN planner cannot read or act on JAI (403 and 0 rows); tokens are really verified.
  B-API-2  Driver identity and shipment linking: identity comes from the verified webhook, never from tool args;
           a double-assigned driver is escalated as CONTRADICTORY_DATA (F3).
  B-API-3  Options → read-back → explicit yes → hold → submit, and 50 drivers racing for one slot (F2).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from conftest import BASE_DSN, _pg_available
from setuhaul.api.app import Settings, create_app
from setuhaul.api.security import SessionTokens
from setuhaul.db.migrate import migrate

IST = timezone(timedelta(hours=5, minutes=30))
ISSUER = "https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_TEST"
CLIENT = "console-client"
META_SECRET = b"meta-app-secret-for-tests-only-0123456789"
SESSION_SECRET = b"session-secret-for-tests-only-0123456789abcdef"
JAI, GGN = "FAC-JAI-01", "FAC-GGN-01"


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
    jwk.update(kid="test-kid", alg="RS256", use="sig")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {"private": k, "jwks": {"keys": [jwk]}, "attacker": other}


@pytest.fixture(scope="module")
def dsn():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS setuhaul_build_api WITH (FORCE)")
        c.execute("CREATE DATABASE setuhaul_build_api")
    d = f"{BASE_DSN} dbname=setuhaul_build_api"
    migrate(d)
    with psycopg.connect(d, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO carriers VALUES ('CAR1','Carrier One',true)")
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES
                     ('FAC-JAI-01','Jaipur','00:00','23:59'),('FAC-GGN-01','Gurugram','00:00','23:59')""")
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30),
                     ('FAC-GGN-01',1,15,10,20,'{"DAY":0,"NIGHT":0}',%s,30)""", (shifts, shifts))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) VALUES
                     ('DOCK-JAI-D1','FAC-JAI-01','D1','STANDARD',false,30000),('DOCK-JAI-D2','FAC-JAI-01','D2','STANDARD',false,30000),
                     ('DOCK-JAI-D3','FAC-JAI-01','D3','STANDARD',false,30000),('DOCK-GGN-D1','FAC-GGN-01','D1','STANDARD',false,30000)""")
        # hourly slots for three days on every dock, aligned to the IST hour
        base = datetime.now(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for dock in ("DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-JAI-D3", "DOCK-GGN-D1"):
            for h in range(72):
                s = base + timedelta(hours=h)
                c.execute("INSERT INTO appointment_slots VALUES (%s,%s,tstzrange(%s,%s,'[)'),'OPEN',NULL)",
                          (f"S-{dock}-{h}", dock, s, s + timedelta(hours=1)))
    return d


@pytest.fixture(scope="module")
def app(dsn, keys):
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True))   # the booking path is under test here


@pytest.fixture
def client(app):
    return TestClient(app)


def add_driver(dsn, facility=JAI, n_shipments=1, eta_h=2.0, overlap=False, eta_at: datetime | None = None) -> tuple[str, str, list[str]]:
    """eta_at fixes the planned ETA absolutely (tests that must not drift with the clock); else now() + eta_h."""
    if eta_at is not None:
        eta_h = (eta_at - datetime.now(IST)).total_seconds() / 3600
        if n_shipments == 1:
            return _add_driver_at(dsn, facility, eta_at)
    u = uuid.uuid4().hex[:8]
    drv, phone = f"DRV-{u}", f"+91{uuid.uuid4().int % 10**10:010d}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)", (drv, phone))
        sids = []
        for i in range(n_shipments):
            sid, veh = f"SHP-{u}-{i}", f"VEH-{u}-{i}"
            c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}-{i}"))
            c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                         load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                         VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,now()+make_interval(mins=>%s)-interval '5 hours',
                                 now()+make_interval(mins=>%s),'IN_TRANSIT')""",
                      (sid, f"ORD-{u}-{i}", drv, veh, facility, *([int(eta_h * 60) + (0 if overlap else i * 600)] * 2)))
            sids.append(sid)
    return drv, phone, sids


class Driver:
    """A driver on WhatsApp plus the orchestrator's view of the conversation (session token, turn counter)."""

    def __init__(self, client, phone):
        self.c, self.phone, self.turn, self.session, self.thread, self.last_msg = client, phone, 0, None, None, None

    def say(self, text: str, msg_id: str | None = None) -> dict | None:
        """Send a WhatsApp message; read what the orchestrator would receive from the internal queue."""
        mid = msg_id or f"wamid.{uuid.uuid4().hex}"
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": self.phone.lstrip("+"), "id": mid, "type": "text", "text": {"body": text}}]}}]}]}).encode()
        r = post_signed(self.c, body)
        assert r.status_code == 200 and r.json() == {"status": "received"}, r.text      # constant: no token, no hints
        ev = next((e for e in reversed(self.c.app.state.inbound) if e.get("external_message_id") == mid), None)
        if ev:
            self.session, self.thread, self.last_msg = ev["session_token"], ev["thread_id"], ev["message_id"]
        return ev

    def tool(self, name: str, **args) -> dict:
        self.turn += 1
        r = self.c.post(f"/tools/{name}", json={"turn_id": self.turn, "args": args}, headers={"X-Setuhaul-Session": self.session})
        assert r.status_code == 200, (r.status_code, r.text)
        return r.json()


def post_signed(client, body: bytes):
    sig = "sha256=" + hmac.new(META_SECRET, body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def staff_token(keys, groups=("planner",), facilities=(JAI,), **over) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": f"user-{uuid.uuid4().hex[:6]}", "token_use": "access", "client_id": CLIENT,
              "iat": now, "exp": now + 600, "cognito:groups": list(groups), "facility_scope": ",".join(facilities)}
    claims.update(over)
    return jwt.encode(claims, keys["private"], algorithm="RS256", headers={"kid": "test-kid"})


def H(token):
    return {"Authorization": f"Bearer {token}"}


def book_to_pending(client, dsn):
    drv, phone, (sid,) = add_driver(dsn)
    d = Driver(client, phone)
    d.say("Running 90 min late, slot after 12?")
    eta = (datetime.now(IST) + timedelta(minutes=90)).isoformat()
    assert d.tool("record_driver_report", message_id=d.last_msg, declared_eta=eta, eta_precision="PRECISE",
                  delay_reason="TRAFFIC")["status"] == "OK"
    opts = d.tool("find_feasible_options")
    assert opts["status"] == "OK" and opts["data"]["options"], opts
    o = opts["data"]["options"][0]["option_id"]
    rb = d.tool("select_option", option_id=o)
    assert rb["status"] == "OK" and "Shall I request it?" in rb["data"]["read_back"]
    d.say("haan ji, kar do")
    assert d.tool("confirm_selection", message_id=d.last_msg)["data"]["verdict"] == "YES"
    assert d.tool("hold_option", option_id=o)["data"]["status"] == "HELD"
    sub = d.tool("submit_booking_request", option_id=o)
    assert sub["status"] == "OK" and sub["data"]["status"] == "PENDING", sub
    return d, sid, o, sub["data"]["appointment_id"]


# ---------------------------------------------------------------- MVP path (B-API-3)
def test_delay_to_confirmed_reschedule(client, dsn, keys):
    d, sid, o, appt = book_to_pending(client, dsn)
    reqs = client.get("/console/requests", params={"facility_id": JAI}, headers=H(staff_token(keys))).json()["requests"]
    assert appt in [r["appointment_id"] for r in reqs]
    r = client.post(f"/console/requests/{appt}/decision", headers=H(staff_token(keys)),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    st = d.tool("get_booking_status")["data"]["appointments"][0]
    assert (st["appointment_id"], st["status"], st["current"]) == (appt, "CONFIRMED", True)


def test_bare_number_is_not_a_yes_and_hedged_reply_is_not_either(client, dsn):
    drv, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late by 1 hour")
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    assert d.tool("hold_option", option_id=o)["data"]["code"] == "NOT_ALLOWED_IN_OPTIONS_SHOWN"     # "2" ≠ yes
    d.tool("select_option", option_id=o)
    d.say("yes but make it 2 pm")
    assert d.tool("confirm_selection", message_id=d.last_msg)["data"]["verdict"] == "UNCLEAR"
    assert d.tool("hold_option", option_id=o)["data"]["code"] == "NOT_ALLOWED_IN_SELECTED"


def test_agent_cannot_point_confirmation_at_its_own_or_older_message(client, dsn):
    drv, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    first = d.say("yes")                                        # a "yes" sent BEFORE any option existed
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    early = d.say("yes")                                        # after the options, but BEFORE the read-back
    d.tool("select_option", option_id=o)
    for m in (first["message_id"], early["message_id"]):
        r = d.tool("confirm_selection", message_id=m)
        assert r["status"] == "NEEDS_INFO" and r["data"]["code"] == "NO_DRIVER_CONFIRMATION"
    assert d.tool("hold_option", option_id=o)["data"]["code"] == "NOT_ALLOWED_IN_SELECTED"


# ---------------------------------------------------------------- 50 drivers, one slot (F2)
def _add_driver_at(dsn, facility, eta_at: datetime) -> tuple[str, str, list[str]]:
    u = uuid.uuid4().hex[:8]
    drv, phone, sid, veh = f"DRV-{u}", f"+91{uuid.uuid4().int % 10**10:010d}", f"SHP-{u}-0", f"VEH-{u}-0"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)", (drv, phone))
        c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}-0"))
        c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                     load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                     VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,%s,%s,'IN_TRANSIT')""",
                  (sid, f"ORD-{u}-0", drv, veh, facility, eta_at - timedelta(hours=5), eta_at))
    return drv, phone, [sid]


def test_fifty_drivers_race_for_one_slot(client, dsn, app):
    # ETA at :20 of an hour 30 h out: ready ≈ :45 (ETA band + gate-to-dock), so the first slot stays the same for
    # every driver however long the setup takes (the old now()-relative ETA drifted across a slot boundary)
    race_eta = (datetime.now(IST) + timedelta(hours=30)).replace(minute=20, second=0, microsecond=0)
    drivers = []
    for _ in range(50):
        _, phone, _ = add_driver(dsn, eta_at=race_eta)          # identical trucks, same fixed ETA → same first option
        d = Driver(client, phone)
        d.say("delayed")
        o = d.tool("find_feasible_options")["data"]["options"][0]
        d.tool("select_option", option_id=o["option_id"])
        d.say("confirm")
        assert d.tool("confirm_selection", message_id=d.last_msg)["data"]["verdict"] == "YES"
        drivers.append((d, o))
    assert len({(o["dock_code"], o["start"]) for _, o in drivers}) == 1, "all fifty were offered the same slot"
    barrier, results = threading.Barrier(50), []

    def go(d, o):
        d.c = TestClient(app)
        barrier.wait()
        results.append(d.tool("hold_option", option_id=o["option_id"]))

    ts = [threading.Thread(target=go, args=x) for x in drivers]
    [t.start() for t in ts]
    [t.join() for t in ts]
    statuses = [r["status"] for r in results]
    assert statuses.count("OK") == 1 and statuses.count("LOST") == 49, statuses
    assert all(r["data"]["fresh_options"] for r in results if r["status"] == "LOST")   # losers get new options at once


# ---------------------------------------------------------------- identity and scope (B-API-1, B-API-2)
def test_ggn_planner_cannot_see_or_decide_jai(client, dsn, keys):
    _, _, _, appt = book_to_pending(client, dsn)
    ggn = staff_token(keys, facilities=(GGN,))
    assert client.get("/console/requests", params={"facility_id": JAI}, headers=H(ggn)).status_code == 403
    assert all(r["facility_id"] == GGN for r in client.get("/console/requests", headers=H(ggn)).json()["requests"])
    r = client.post(f"/console/requests/{appt}/decision", headers=H(ggn), json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 404                                 # RLS: the row does not exist for them


@pytest.mark.parametrize("case", ["none", "tampered", "expired", "wrong_issuer", "id_token", "other_client",
                                  "attacker_key", "no_role", "hs256_confusion"])
def test_console_rejects_bad_tokens(client, keys, case):
    good = staff_token(keys)
    tok = {
        "none": None,
        "tampered": good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB"),
        "expired": staff_token(keys, exp=int(time.time()) - 120, iat=int(time.time()) - 900),
        "wrong_issuer": staff_token(keys, iss="https://evil.example"),
        "id_token": staff_token(keys, token_use="id"),
        "other_client": staff_token(keys, client_id="someone-else"),
        "attacker_key": jwt.encode(jwt.decode(good, options={"verify_signature": False}), keys["attacker"], algorithm="RS256",
                                   headers={"kid": "test-kid"}),
        "no_role": staff_token(keys, **{"cognito:groups": ["drivers"]}),
        "hs256_confusion": jwt.encode(jwt.decode(good, options={"verify_signature": False}), "public-key-as-secret-xxxxxxxxxxxxxxxx",
                                      algorithm="HS256", headers={"kid": "test-kid"}),
    }[case]
    r = client.get("/console/requests", headers=H(tok) if tok else {})
    assert r.status_code == 401, (case, r.text)


def _far(days_extra: int = 0):
    base = datetime.now(IST) + timedelta(days=40 + uuid.uuid4().int % 300 + days_extra)
    return base.isoformat(), (base + timedelta(hours=1)).isoformat()


CONSOLE_ACTIONS = {
    "list_requests": lambda c, h: c.get("/console/requests", headers=h),
    "decide": lambda c, h: c.post("/console/requests/APT-none/decision", headers=h,
                                  json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex}),
    "block_dock": lambda c, h: c.post("/console/docks/DOCK-JAI-D1/blocks", headers=h, json=dict(
        zip(("start", "end"), _far()), event_id=f"DE-{uuid.uuid4().hex[:8]}", type="MAINTENANCE", reason="matrix test")),
    "pause": lambda c, h: c.post(f"/console/facilities/{JAI}/pause", headers=h, json={"paused": False, "reason": "matrix"}),
    "takeover": lambda c, h: c.post("/console/threads/THR-none/takeover", headers=h, json={"reason": "matrix"}),
    "handback": lambda c, h: c.post("/console/threads/THR-none/handback", headers=h, json={"summary": "matrix test summary"}),
    "withdraw": lambda c, h: c.post("/console/requests/APT-none/withdraw", headers=h, json={
        "reason_code": "OPERATIONS_DECISION", "note": "matrix test", "idempotency_key": uuid.uuid4().hex}),
    "booking_hold": lambda c, h: c.post("/console/holds/999999999/clear", headers=h, json={"note": "matrix test"}),
    "policy_report": lambda c, h: c.get("/console/policy/outcomes", headers=h),
}
MATRIX = {  # role -> actions allowed (must equal setuhaul.api.service.PERMISSIONS). Which request KINDS each may decide
    # is enforced by the database (approval_role_rules, B-WF-2 §8), not by this matrix.
    "planner": {"list_requests", "decide", "block_dock", "withdraw", "policy_report", "takeover", "handback"},  # R3.1-P2
    "ops_lead": {"list_requests", "decide", "block_dock", "pause", "takeover", "handback", "withdraw", "booking_hold",
                 "policy_report"},
    "operations": {"list_requests", "decide", "withdraw", "takeover", "handback"},
}


def test_matrix_matches_code():
    from setuhaul.api.service import PERMISSIONS
    assert {r: {a for a, roles in PERMISSIONS.items() if r in roles} for r in MATRIX} == MATRIX


@pytest.mark.parametrize("role", sorted(MATRIX))
@pytest.mark.parametrize("action", sorted(CONSOLE_ACTIONS))
def test_console_role_action_matrix(client, keys, role, action):
    r = CONSOLE_ACTIONS[action](client, H(staff_token(keys, groups=(role,))))
    if action in MATRIX[role]:
        assert r.status_code != 403, (role, action, r.text)        # allowed: may still be 404 / 409 for a dummy target
    else:
        assert r.status_code == 403, (role, action, r.text)


def test_driver_cannot_use_another_drivers_shipment(client, dsn):
    _, phone_a, _ = add_driver(dsn)
    _, _, (sid_b,) = add_driver(dsn)
    a = Driver(client, phone_a)
    a.say("late")
    r = client.post("/tools/find_feasible_options", json={"turn_id": 1, "args": {"shipment_id": sid_b}},
                    headers={"X-Setuhaul-Session": a.session})
    assert r.status_code == 403


def test_tool_args_cannot_carry_identity(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    r = d.tool("find_feasible_options", driver_id="DRV-someone-else", thread_id="THR-other")
    assert r["status"] == "NEEDS_INFO" and set(r["missing_fields"]) >= {"driver_id", "thread_id"}


def test_session_tokens_are_verified(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    forged = SessionTokens(b"x" * 40).mint("DRV-attacker", d.thread, JAI)
    expired = SessionTokens(SESSION_SECRET, ttl_s=-60).mint(d.c.app.state.sessions.verify(d.session).driver_id, d.thread, JAI)
    for tok in (forged, expired, d.session[:-3] + "abc", ""):
        r = client.post("/tools/get_driver_context", json={"turn_id": 1, "args": {}}, headers={"X-Setuhaul-Session": tok})
        assert r.status_code == 401


def test_decision_tools_do_not_exist_for_the_agent(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    for t in ("decide_booking_request", "block_dock", "set_automation_paused", "approve", "run_sql", "place_hold"):
        r = client.post(f"/tools/{t}", json={"turn_id": 9, "args": {}}, headers={"X-Setuhaul-Session": d.session})
        assert r.status_code == 404 and r.json()["error"] == "TOOL_NOT_AVAILABLE_TO_AGENT"


def test_double_assigned_driver_goes_to_a_person(client, dsn):
    """F3: the same driver on two overlapping shipments. No bookable options; escalate CONTRADICTORY_DATA."""
    _, phone, (s1, s2) = add_driver(dsn, n_shipments=2, overlap=True)
    d = Driver(client, phone)
    d.say("running late")
    assert d.tool("find_feasible_options")["missing_fields"] == ["shipment_id"]       # two live shipments: ask which
    r = d.tool("find_feasible_options", shipment_id=s1)
    assert r["status"] == "REQUIRES_HUMAN" and r["requires_human"]["reason_code"] == "CONTRADICTORY_DATA"
    assert "options" not in r["data"]


# ---------------------------------------------------------------- webhook
def test_webhook_signature_and_duplicates(client, dsn):
    _, phone, _ = add_driver(dsn)
    body = b'{"entry":[]}'
    assert client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"}).status_code == 401
    assert client.post("/webhooks/whatsapp", content=body).status_code == 401
    d = Driver(client, phone)
    first = d.say("late by 30 min", msg_id="wamid.SAME")
    d.say("late by 30 min", msg_id="wamid.SAME")                          # provider retry
    with psycopg.connect(dsn) as c:
        n = c.execute("SELECT count(*) FROM setuhaul.chat_messages WHERE external_message_id='wamid.SAME'").fetchone()[0]
    assert first["message_id"] and n == 1
    assert sum(1 for e in client.app.state.inbound if e.get("external_message_id") == "wamid.SAME") == 1


def test_unknown_number_learns_nothing(client):
    d = Driver(client, "+910000000000")
    assert d.say("hello") is None                               # same constant 200 answer; nothing queued


# ---------------------------------------------------------------- humans decide, with re-validation (T27)
def test_confirm_is_revalidated_against_new_eta(client, dsn, keys):
    d, sid, o, appt = book_to_pending(client, dsn)
    d.say("sorry, breakdown, now tomorrow morning")
    later = (datetime.now(IST) + timedelta(hours=20)).isoformat()
    d.tool("record_driver_report", message_id=d.last_msg, declared_eta=later, eta_precision="PRECISE", delay_reason="BREAKDOWN")
    r = client.post(f"/console/requests/{appt}/decision", headers=H(staff_token(keys)),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409 and r.json()["error"] == "NOT_FEASIBLE", r.text


def test_takeover_silences_the_agent(client, dsn, keys):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    ops = H(staff_token(keys, groups=("operations",)))
    assert client.post(f"/console/threads/{d.thread}/takeover", headers=ops, json={"reason": "driver asked"}).status_code == 200
    for tool, args in (("find_feasible_options", {}), ("get_driver_context", {}), ("get_booking_status", {}),
                       ("record_driver_report", {"message_id": d.last_msg}),
                       ("escalate_to_human", {"reason_code": "DRIVER_REQUEST", "driver_last_message": "x", "what_driver_was_told": "y"})):
        r = d.tool(tool, **args)
        assert r["status"] == "REQUIRES_HUMAN" and r["data"]["code"] == "HUMAN_ACTIVE", tool
    assert client.post(f"/console/threads/{d.thread}/handback", headers=ops,
                       json={"summary": "Spoke to driver, will arrive at 4 pm"}).status_code == 200
    assert d.tool("get_driver_context")["status"] == "OK"


def test_safety_goes_straight_to_a_person(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("accident on highway, I am hurt")
    r = d.tool("record_driver_report", message_id=d.last_msg, delay_reason="ACCIDENT", safety_concern=True)
    assert r["status"] == "REQUIRES_HUMAN" and r["requires_human"]["reason_code"] == "SAFETY"
    with psycopg.connect(dsn) as c:                            # durable hand-off, and the thread is now a person's
        row = c.execute("""SELECT e.reason_code, e.mode, t.human_active FROM setuhaul.escalations e
                           JOIN setuhaul.chat_threads t USING (thread_id) WHERE e.escalation_id=%s""",
                        (r["data"]["escalation_id"],)).fetchone()
    assert row == ("SAFETY", "TAKE_OVER", True)
    assert d.tool("find_feasible_options")["data"]["code"] == "HUMAN_ACTIVE"


def test_no_coordinator_on_shift_escalates(client, dsn):
    """GGN has no coordinator (TTL 0): the request is refused by the database and becomes REQUIRES_HUMAN."""
    _, phone, _ = add_driver(dsn, facility=GGN)
    d = Driver(client, phone)
    d.say("late")
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    d.tool("select_option", option_id=o)
    d.say("ok")
    d.tool("confirm_selection", message_id=d.last_msg)
    assert d.tool("hold_option", option_id=o)["status"] == "OK"
    r = d.tool("submit_booking_request", option_id=o)
    assert r["status"] == "REQUIRES_HUMAN", r



# ---------------------------------------------------------------- step 2 review corrections
def test_webhook_never_returns_a_session_token(client, dsn):
    _, phone, _ = add_driver(dsn)
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": phone.lstrip("+"), "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": "hi"}}]}}]}]}).encode()
    r = post_signed(client, body)
    assert r.json() == {"status": "received"} and "session" not in r.text and "THR-" not in r.text


def test_one_bad_message_does_not_fail_the_batch(client, dsn):
    _, phone, _ = add_driver(dsn)
    good = f"wamid.{uuid.uuid4().hex}"
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.broken", "type": "text"},                                     # no sender, no text
        {"from": phone.lstrip("+"), "id": good, "type": "text", "text": {"body": "late"}}]}}]}]}).encode()
    r = post_signed(client, body)
    assert r.status_code == 200
    assert any(e.get("external_message_id") == good for e in client.app.state.inbound)
    assert post_signed(client, b"not json").status_code == 200


def test_eta_report_needs_a_driver_message_from_this_thread(client, dsn):
    _, pa, _ = add_driver(dsn)
    _, pb, _ = add_driver(dsn)
    a, b = Driver(client, pa), Driver(client, pb)
    a.say("late")
    foreign = b.say("something")["message_id"]
    o = a.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    rb = a.tool("select_option", option_id=o)["data"]["read_back_message_id"]     # an AGENT message
    eta = (datetime.now(IST) + timedelta(hours=2)).isoformat()
    for m in (foreign, rb, 999999999):
        r = a.tool("record_driver_report", message_id=m, declared_eta=eta, eta_precision="PRECISE")
        assert r["status"] == "FORBIDDEN" and r["data"]["code"] == "MESSAGE_NOT_FROM_DRIVER", (m, r)


def test_datetimes_must_be_timezone_aware_and_ordered(client, dsn, keys):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    naive = (datetime.now(IST) + timedelta(hours=3)).replace(tzinfo=None).isoformat()
    r = d.tool("record_driver_report", message_id=d.last_msg, declared_eta=naive, eta_precision="PRECISE")
    assert r["status"] == "NEEDS_INFO" and "declared_eta" in r["missing_fields"]
    es, lf = (datetime.now(IST) + timedelta(hours=5)).isoformat(), (datetime.now(IST) + timedelta(hours=4)).isoformat()
    assert d.tool("find_feasible_options", earliest_start=es, latest_finish=lf)["status"] == "NEEDS_INFO"
    h = H(staff_token(keys))
    start = datetime.now(IST) + timedelta(days=300)
    bad = [{"start": (start + timedelta(hours=1)).isoformat(), "end": start.isoformat()},
           {"start": start.replace(tzinfo=None).isoformat(), "end": (start + timedelta(hours=1)).replace(tzinfo=None).isoformat()}]
    for b in bad:
        r = client.post("/console/docks/DOCK-JAI-D1/blocks", headers=h, json=dict(b, event_id="DE-bad", type="MAINTENANCE", reason="x y z"))
        assert r.status_code == 422, r.text


def test_takeover_after_hold_stops_submit(client, dsn, keys):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    d.tool("select_option", option_id=o)
    d.say("yes")
    d.tool("confirm_selection", message_id=d.last_msg)
    assert d.tool("hold_option", option_id=o)["data"]["status"] == "HELD"
    client.post(f"/console/threads/{d.thread}/takeover", headers=H(staff_token(keys, groups=("ops_lead",))), json={"reason": "check"})
    for tool in ("submit_booking_request", "hold_option", "release_hold"):
        r = d.tool(tool, option_id=o)
        assert r["status"] == "REQUIRES_HUMAN", (tool, r)


def test_agent_take_over_escalation_hands_thread_to_a_person(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("I want to talk to a person")
    r = d.tool("escalate_to_human", reason_code="DRIVER_REQUEST", mode="TAKE_OVER", driver_last_message="talk to a person",
               what_driver_was_told="Connecting you")
    assert r["status"] == "OK" and r["data"]["human_active"] is True
    assert d.tool("get_driver_context")["data"]["code"] == "HUMAN_ACTIVE"


def test_unassigned_escalation_goes_to_central_queue(client, dsn, keys):
    _, phone, _ = add_driver(dsn, n_shipments=2)                 # thread has no shipment yet
    d = Driver(client, phone)
    d.say("help, which one?")
    r = d.tool("escalate_to_human", reason_code="DRIVER_REQUEST", driver_last_message="help", what_driver_was_told="ok")
    assert r["data"]["queue"] == "CENTRAL"
    eid = r["data"]["escalation_id"]
    jai = client.get("/console/escalations", headers=H(staff_token(keys))).json()["escalations"]
    central = client.get("/console/escalations",
                         headers=H(staff_token(keys, groups=("operations",), facilities=("CENTRAL",)))).json()["escalations"]
    assert eid not in [str(e["escalation_id"]) for e in jai] and eid in [str(e["escalation_id"]) for e in central]


def test_report_and_escalation_are_idempotent_per_turn(client, dsn):
    _, phone, (sid,) = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late, 3 pm")
    eta = (datetime.now(IST) + timedelta(hours=3)).isoformat()
    args = {"message_id": d.last_msg, "declared_eta": eta, "eta_precision": "PRECISE", "delay_reason": "TRAFFIC"}
    for _ in range(2):                                           # orchestrator retry: same turn, same args
        r = client.post("/tools/record_driver_report", json={"turn_id": 7, "args": args}, headers={"X-Setuhaul-Session": d.session})
        assert r.json()["status"] == "OK"
    esc = {"reason_code": "DRIVER_REQUEST", "driver_last_message": "x", "what_driver_was_told": "y"}
    ids = {client.post("/tools/escalate_to_human", json={"turn_id": 8, "args": esc},
                       headers={"X-Setuhaul-Session": d.session}).json()["data"]["escalation_id"] for _ in range(2)}
    with psycopg.connect(dsn) as c:
        n = c.execute("SELECT count(*) FROM setuhaul.eta_signals WHERE shipment_id=%s AND source='DRIVER_DECLARED'", (sid,)).fetchone()[0]
    assert n == 1 and len(ids) == 1


def test_driver_can_switch_to_own_other_shipment_only_when_idle(client, dsn):
    # The second shipment's ETA is now + 12 h, often late at night. Since B-FEAS-1 the engine also offers the next
    # morning's slots, so this test no longer depends on the time of day it runs.
    _, phone, (s1, s2) = add_driver(dsn, n_shipments=2)
    d = Driver(client, phone)
    d.say("late")
    assert d.tool("find_feasible_options", shipment_id=s1)["status"] == "OK"
    assert d.tool("find_feasible_options", shipment_id=s2)["status"] == "OK"         # idle: thread moves to s2
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    d.tool("select_option", option_id=o)
    d.say("yes")
    d.tool("confirm_selection", message_id=d.last_msg)
    assert d.tool("hold_option", option_id=o)["data"]["status"] == "HELD"
    r = client.post("/tools/find_feasible_options", json={"turn_id": 50, "args": {"shipment_id": s1}},
                    headers={"X-Setuhaul-Session": d.session})
    assert r.status_code == 409 and r.json()["error"] == "FINISH_CURRENT_BOOKING_FIRST"


def test_expired_option_cannot_be_selected(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE setuhaul.shown_options SET expires_at = now() - interval '1 min' WHERE option_id=%s", (o,))
    assert d.tool("select_option", option_id=o)["status"] == "OPTION_EXPIRED"



# ---------------------------------------------------------------- final step 2 corrections (0005)
def _to_read_back(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    d.say("late")
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    d.tool("select_option", option_id=o)
    return d, o


def test_api_yes_then_no_cannot_book(client, dsn):
    d, o = _to_read_back(client, dsn)
    yes = d.say("haan")["message_id"]
    no = d.say("nahi, ruko")["message_id"]
    r = d.tool("confirm_selection", message_id=yes)
    assert r["status"] == "NEEDS_INFO" and r["data"]["code"] == "NO_DRIVER_CONFIRMATION"     # superseded
    assert d.tool("confirm_selection", message_id=no)["data"]["verdict"] == "NO"
    assert d.tool("hold_option", option_id=o)["status"] != "OK"


def test_api_yes_confirmed_then_hesitation_blocks_hold(client, dsn):
    d, o = _to_read_back(client, dsn)
    yes = d.say("yes")["message_id"]
    assert d.tool("confirm_selection", message_id=yes)["data"]["verdict"] == "YES"
    d.say("actually wait")                                         # after the flow recorded the yes
    r = d.tool("hold_option", option_id=o)
    assert r["status"] == "NEEDS_INFO" and r["data"]["code"] == "CONFIRMATION_SUPERSEDED", r


def test_webhook_retry_recovers_a_failed_attestation(client, dsn, monkeypatch):
    import setuhaul.api.service as svcmod
    real, calls = svcmod.classify_confirmation, {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient classifier failure")
        return real(text)

    d, o = _to_read_back(client, dsn)
    monkeypatch.setattr(svcmod, "classify_confirmation", flaky)
    first = d.say("yes", msg_id="wamid.RETRY-ME")
    assert first["attestation_status"] == "FAILED"
    assert d.tool("confirm_selection", message_id=first["message_id"])["status"] == "NEEDS_INFO"
    again = d.say("yes", msg_id="wamid.RETRY-ME")                  # Meta retries the same webhook
    assert again["recovered"] is True and again["attestation_status"] == "DONE"
    assert again["message_id"] == first["message_id"]
    assert d.tool("confirm_selection", message_id=first["message_id"])["data"]["verdict"] == "YES"
    assert d.tool("hold_option", option_id=o)["data"]["status"] == "HELD"
    with psycopg.connect(dsn) as c:
        n_msg = c.execute("SELECT count(*) FROM setuhaul.chat_messages WHERE external_message_id='wamid.RETRY-ME'").fetchone()[0]
        n_att = c.execute("SELECT count(*) FROM setuhaul.confirmation_attestations WHERE confirmation_message_id=%s",
                          (first["message_id"],)).fetchone()[0]
        rec = c.execute("SELECT attestation_status, attempts FROM setuhaul.inbound_receipts WHERE message_id=%s",
                        (first["message_id"],)).fetchone()
    assert (n_msg, n_att, rec) == (1, 1, ("DONE", 2))


def test_reconciler_recovers_without_a_retry(client, dsn, monkeypatch, app):
    import setuhaul.api.service as svcmod
    d, o = _to_read_back(client, dsn)
    monkeypatch.setattr(svcmod, "classify_confirmation", lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    first = d.say("yes")
    assert first["attestation_status"] == "FAILED"
    monkeypatch.undo()
    out = app.state.service.reconcile_inbound(older_than_s=0)
    assert out.get("DONE", 0) >= 1
    assert d.tool("confirm_selection", message_id=first["message_id"])["data"]["verdict"] == "YES"



def test_booking_switch_is_off_by_default(dsn, keys):
    """Production default: the agent can find options and read back, but cannot hold or request a slot."""
    off = TestClient(create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                                         session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                                         facilities=(JAI, GGN), jwks=keys["jwks"])))
    _, phone, _ = add_driver(dsn)
    d = Driver(off, phone)
    d.say("late")
    o = d.tool("find_feasible_options")["data"]["options"][0]["option_id"]
    assert d.tool("select_option", option_id=o)["status"] == "OK"
    d.say("yes")
    assert d.tool("confirm_selection", message_id=d.last_msg)["data"]["verdict"] == "YES"
    for tool in ("hold_option", "submit_booking_request"):
        r = d.tool(tool, option_id=o)
        assert r["status"] == "REQUIRES_HUMAN" and r["data"]["code"] == "BOOKING_DISABLED", tool
    assert Settings.__dataclass_fields__["booking_enabled"].default is False


# ---------------------------------------------------------------- F1, found by the G3 replay (B-EV-2)
def test_redelivered_report_with_a_new_id_is_a_duplicate_but_a_repeated_yes_is_not(client, dsn):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    report = "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"
    assert d.say(report) is not None
    assert d.say(report) is None                                 # same report, new provider id, seconds later: no second turn
    assert d.say("yes") is not None and d.say("yes") is not None  # short replies are repeated on purpose


def test_masking_hides_phone_numbers_but_keeps_order_references(client, dsn):
    """Live G3 replay (B-EV-2): masking every 6+ digit run turned ORD-260804-020 into ORD-######-020."""
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    ev = d.say("ORD-260804-020 is late, call me on 9876543210")
    p = client.app.state.sessions.verify(ev["session_token"])
    body = client.app.state.service.read_driver_message(p, ev["message_id"])
    assert "ORD-260804-020" in body and "9876543210" not in body


# ---------------------------------------------------------------- R0.9 B2-17 through BookingService (migration 0010)
def _bind_in(dsn, drv, thread, sid, commit=True):
    c = psycopg.connect(dsn)
    c.execute("SET LOCAL ROLE setuhaul_agent")
    c.execute("SET LOCAL search_path = setuhaul")
    c.execute("SELECT set_config('app.facility_scope', %s, true)", (JAI,))
    c.execute("SELECT set_config('app.driver_id', %s, true)", (drv,))
    c.execute("SELECT bind_thread_shipment(%s,%s,'other-turn')", (thread, sid))
    if commit:
        c.commit()
        c.close()
    return c


def test_b2_17_a_turn_waits_for_an_inflight_switch_and_uses_the_new_shipment(client, dsn):
    """Before 0010 the service read the binding before the lock and went on to plan for the old shipment."""
    drv, phone, (s1, s2) = add_driver(dsn, n_shipments=2)
    d = Driver(client, phone)
    d.say("late")
    assert d.tool("find_feasible_options", shipment_id=s1)["data"]["shipment_id"] == s1
    paused = _bind_in(dsn, drv, d.thread, s2, commit=False)          # another turn is switching, not yet committed
    out = {}
    th = threading.Thread(target=lambda: out.update(r=d.tool("find_feasible_options")))
    th.start()
    th.join(1.0)
    assert th.is_alive(), "the turn must wait for the conversation lock"
    paused.commit()
    paused.close()
    th.join(30)
    assert out["r"]["status"] == "OK" and out["r"]["data"]["shipment_id"] == s2, out


def test_b2_17_options_are_not_shown_for_a_shipment_the_thread_just_left(client, dsn, monkeypatch):
    drv, phone, (s1, s2) = add_driver(dsn, n_shipments=2)
    d = Driver(client, phone)
    d.say("late")
    assert d.tool("find_feasible_options", shipment_id=s1)["status"] == "OK"
    svc = client.app.state.service
    real = svc._engine

    def engine_then_switch(*a, **kw):                               # a concurrent turn moves the thread meanwhile
        eng = real(*a, **kw)
        monkeypatch.setattr(svc, "_engine", real)
        _bind_in(dsn, drv, d.thread, s2)
        return eng
    monkeypatch.setattr(svc, "_engine", engine_then_switch)
    r = d.tool("find_feasible_options")
    assert r["status"] == "ERROR" and r["data"]["code"] == "STALE_SHIPMENT", r
    with psycopg.connect(dsn) as c:
        n = c.execute("""SELECT count(*) FROM setuhaul.shown_options WHERE thread_id=%s AND shipment_id=%s
                         AND expires_at > now()""", (d.thread, s1)).fetchone()[0]
        state = c.execute("SELECT state FROM setuhaul.thread_flow WHERE thread_id=%s", (d.thread,)).fetchone()[0]
    assert n == 0 and state == "NO_OPTIONS"                         # nothing live for s1, flow not moved to OPTIONS


# ================================================================ Phase G: health check
def test_health_reports_the_database_and_the_booking_switch_and_nothing_secret(dsn, keys):
    off = TestClient(create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                                         session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                                         facilities=(JAI, GGN), jwks=keys["jwks"], environment="pilot-ap-southeast-2")))
    r = off.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "booking_enabled": False, "booking_state": "off",
                        "environment": "pilot-ap-southeast-2", "version": "0.2.0"}
    assert "password" not in r.text and "host=" not in r.text and "postgres" not in r.text


def test_health_is_503_when_the_database_does_not_answer(keys):
    down = TestClient(create_app(Settings(dsn="host=127.0.0.1 port=1 user=nobody connect_timeout=1",
                                          cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                                          session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                                          facilities=(JAI, GGN), jwks=keys["jwks"])))
    r = down.get("/health")
    assert r.status_code == 503 and r.json()["database"] == "unreachable" and r.json()["booking_enabled"] is False
    assert "port=1" not in r.text and "nobody" not in r.text
