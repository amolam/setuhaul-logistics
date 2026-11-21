"""SetuHaul local demo console: a person walks the nine business flows (F1-F9) by hand, end to end.

What is real here: the signed WhatsApp webhook, the HTTP API and its staff tokens, the database with its roles,
functions and triggers, the agent orchestrator, the approval workflow port and the outbound relay.
What stands in for AWS / Meta, exactly as in the flow tests:
  * the AI model     -> the rules extractor (no model call, no cost); replies come from the stored templates
  * Cognito          -> staff tokens signed with a key generated at start-up (never leaves this process)
  * SQS              -> LocalQueue
  * Step Functions   -> LocalApprovalWorkflow
  * WhatsApp         -> LocalProvider (the "phone" panel shows what it delivered)

The booking path runs under the local harness only (local_test_harness=True). The production booking switch
SETUHAUL_BOOKING_ENABLED is not read and stays OFF. Uses its own database, setuhaul_demo, never the test databases.

    python demo/server.py --reset     # fresh database, migrated and seeded
    python demo/server.py             # keep the data from last time
    then open http://127.0.0.1:8765/demo
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import psycopg
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Body, HTTPException
from fastapi.responses import FileResponse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from setuhaul.agent.orchestrator import Orchestrator  # noqa: E402
from setuhaul.api.app import Settings, create_app  # noqa: E402
from setuhaul.api.db import Db  # noqa: E402
from setuhaul.db.migrate import migrate  # noqa: E402
from setuhaul.notify.outbound import LocalProvider, OutboundRelay  # noqa: E402
from setuhaul.workflow.approvals import ApprovalOutboxRelay, LocalApprovalWorkflow  # noqa: E402

log = logging.getLogger("demo")
IST = timezone(timedelta(hours=5, minutes=30))
BASE_DSN = os.environ.get("SETUHAUL_DEMO_DSN", "host=localhost port=55432 user=postgres")
DBNAME = "setuhaul_demo"
DSN = f"{BASE_DSN} dbname={DBNAME}"
PORT = int(os.environ.get("SETUHAUL_DEMO_PORT", "8765"))
ISSUER = "https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_DEMO"
CLIENT = "demo-console"
META_SECRET = os.urandom(32)            # per run: the demo signs its own webhooks, nothing outside can
SESSION_SECRET = os.urandom(32)
JAI, GGN = "FAC-JAI-01", "FAC-GGN-01"
RESULTS = HERE / "checklist_results.json"
DEMO_RULE = "demo-auto-standard"

# ---------------------------------------------------------------- database
DRIVERS = [("Ramesh Kumar", JAI, 120), ("Suresh Yadav", JAI, 150), ("Imran Khan", JAI, 180),
           ("Gurpreet Singh", JAI, 210), ("Anil Sharma", GGN, 120)]


def reset_db() -> None:
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DBNAME}")
    migrate(DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO carriers VALUES ('CAR1','Demo Carriers',true)")
        # open all day so every flow works whatever the time; 5-minute approval window so F3 (expiry) is watchable
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time) VALUES
                     ('FAC-JAI-01','Jaipur DC','00:00','23:59'),('FAC-GGN-01','Gurugram DC','00:00','23:59')""")
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":5,"NIGHT":5}',%s,30),
                     ('FAC-GGN-01',1,15,10,20,'{"DAY":5,"NIGHT":5}',%s,30)""", (shifts, shifts))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) VALUES
                     ('DOCK-JAI-D1','FAC-JAI-01','D1','STANDARD',false,30000),('DOCK-JAI-D2','FAC-JAI-01','D2','STANDARD',false,30000),
                     ('DOCK-JAI-D3','FAC-JAI-01','D3','STANDARD',false,30000),('DOCK-GGN-D1','FAC-GGN-01','D1','STANDARD',false,30000)""")
        base = datetime.now(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for dock in ("DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-JAI-D3", "DOCK-GGN-D1"):
            for h in range(96):
                s = base + timedelta(hours=h)
                c.execute("INSERT INTO appointment_slots VALUES (%s,%s,tstzrange(%s,%s,'[)'),'OPEN',NULL)",
                          (f"S-{dock}-{h}", dock, s, s + timedelta(hours=1)))
    for name, fac, eta in DRIVERS:
        add_driver(name, fac, eta)


def add_driver(name: str, facility: str, eta_min: int) -> dict:
    u = uuid.uuid4().hex[:6].upper()
    drv, phone, sid, veh = f"DRV-{u}", f"+9198{uuid.uuid4().int % 10**8:08d}", f"SHP-{u}", f"VEH-{u}"
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1',%s,%s)", (drv, name, phone))
        c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"RJ14-{u}"))
        c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                     load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,current_status)
                     VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,now()+make_interval(mins=>%s)-interval '5 hours',
                             now()+make_interval(mins=>%s),'IN_TRANSIT')""",
                  (sid, f"ORD-{datetime.now(IST):%y%m%d}-{u}", drv, veh, facility, eta_min, eta_min))
    return {"driver_id": drv, "phone": phone, "shipment_id": sid}


def q(sql: str, *args) -> list[dict]:
    """Read-only superuser view for the demo panels (the console itself goes through the real API)."""
    with psycopg.connect(DSN, autocommit=True) as c:
        cur = c.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def jsonable(rows):
    return json.loads(json.dumps(rows, default=str))


# ---------------------------------------------------------------- the running deployment
class Demo:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="demo-kid", alg="RS256", use="sig")
        self.app = create_app(Settings(dsn=DSN, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                                       session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                                       facilities=(JAI, GGN), jwks={"keys": [jwk]},
                                       booking_enabled=True, local_test_harness=True))
        self.db = Db(DSN)
        self.workflow = LocalApprovalWorkflow(self.db)
        self.approvals = ApprovalOutboxRelay(self.db, self.workflow)
        self.provider = LocalProvider()
        self.outbound = OutboundRelay(self.db, self.provider)
        self.orch = Orchestrator(self.app.state.service, self.app.state.sessions, model=None)   # rules, no AI
        self.lock = threading.Lock()
        self.timeline: list[dict] = []        # what each phone shows: sent by the driver / delivered to the phone
        self.activity: list[dict] = []        # agent turns, worker errors
        self.last_sent: dict[str, dict] = {}  # phone -> {msg_id, text}
        self.last_event: dict[str, dict] = {}  # phone -> the last queue event the agent handled
        self._seen_delivered = 0

    def token(self, role: str, facility: str, user: str) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": user, "token_use": "access", "client_id": CLIENT, "iat": now, "exp": now + 8 * 3600,
                  "cognito:groups": [role], "facility_scope": facility}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "demo-kid"})

    def note(self, kind: str, **kw):
        with self.lock:
            self.activity.append({"t": datetime.now(IST).strftime("%H:%M:%S"), "kind": kind, **kw})
            del self.activity[:-300]

    def work_once(self):
        q_ = self.app.state.inbound
        while (ev := q_.receive()) is not None:
            phone = self._phone_of(ev)
            try:
                res = self.orch.handle(ev)
                self.last_event[phone] = ev
                if res is None:
                    self.note("agent", phone=phone, text="(turn refused: already handled)")
                else:
                    self.note("agent", phone=phone, path=" -> ".join(res.path), extractor=getattr(res, "extractor", None),
                              escalation=getattr(res, "escalation_id", None))
            except Exception as e:                               # shown, never hidden
                self.note("error", phone=phone, text=f"agent: {e}", trace=traceback.format_exc()[-1500:])
        for step, fn in (("approval relay", self.approvals.run_once), ("workflow", self.workflow.tick),
                         ("reconcile", self.workflow.reconcile), ("outbound", lambda: self.outbound.run_once(limit=1000))):
            try:
                fn()
            except Exception as e:
                self.note("error", text=f"{step}: {e}", trace=traceback.format_exc()[-1500:])
        with self.lock:
            new = self.provider.delivered[self._seen_delivered:]
            self._seen_delivered += len(new)
            for to, text, _key, _pid in new:
                self.timeline.append({"t": datetime.now(IST).strftime("%H:%M:%S"), "phone": to, "dir": "in", "text": text})

    def _phone_of(self, ev: dict) -> str:
        rows = q("""SELECT d.phone_e164 FROM setuhaul.chat_threads t JOIN setuhaul.drivers d USING (driver_id)
                    WHERE t.thread_id=%s""", ev.get("thread_id"))
        return rows[0]["phone_e164"] if rows else "?"

    def loop(self):
        while True:
            self.work_once()
            time.sleep(1)


def signed_post(body: dict) -> httpx.Response:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(META_SECRET, raw, hashlib.sha256).hexdigest()
    return httpx.post(f"http://127.0.0.1:{PORT}/webhooks/whatsapp", content=raw, timeout=30,
                      headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def build(demo: Demo):
    app = demo.app

    @app.get("/demo")
    def page():
        return FileResponse(HERE / "console.html", headers={"Cache-Control": "no-store"})

    @app.get("/demo/drivers")
    def drivers():
        return jsonable(q("""SELECT d.driver_name AS name, d.phone_e164 AS phone, s.shipment_id, s.order_reference,
                                    s.destination_facility_id AS facility, s.original_eta_ts AS eta, s.load_weight_kg
                               FROM setuhaul.drivers d JOIN setuhaul.shipments s USING (driver_id)
                              ORDER BY s.destination_facility_id <> 'FAC-JAI-01', d.driver_name"""))

    @app.post("/demo/drivers")
    def new_driver(b: dict = Body(...)):
        return add_driver(b.get("name") or "New Driver", b.get("facility") or JAI, int(b.get("eta_min") or 120))

    @app.post("/demo/token")
    def token(b: dict = Body(...)):
        role = b.get("role", "planner")
        if role not in ("planner", "operations", "ops_lead"):
            raise HTTPException(400, "unknown role")
        return {"token": demo.token(role, b.get("facility", JAI), b.get("user") or f"demo-{role}")}

    @app.post("/demo/say")
    def say(b: dict = Body(...)):
        """The driver sends a WhatsApp message: a signed webhook, exactly what Meta would post."""
        phone, text = b["phone"], b.get("text", "")
        duplicate = bool(b.get("duplicate"))
        if duplicate:
            last = demo.last_sent.get(phone)
            if not last:
                raise HTTPException(409, "nothing sent yet")
            mid, text = last["msg_id"], last["text"]
        else:
            mid = f"wamid.demo.{uuid.uuid4().hex}"
            demo.last_sent[phone] = {"msg_id": mid, "text": text}
            with demo.lock:
                demo.timeline.append({"t": datetime.now(IST).strftime("%H:%M:%S"), "phone": phone, "dir": "out", "text": text})
        r = signed_post({"entry": [{"changes": [{"value": {"messages": [
            {"from": phone.lstrip("+"), "id": mid, "type": "text", "text": {"body": text}}]}}]}]})
        demo.note("webhook", phone=phone, text=f"{'DUPLICATE ' if duplicate else ''}{r.status_code} {r.text}")
        out = {"status": r.status_code, "body": r.json()}
        if duplicate:                     # F6: make the invisible visible, from the database, not from a promise
            time.sleep(1.5)
            out["copies"] = q("""SELECT count(*) AS n FROM setuhaul.chat_messages
                                 WHERE channel='WHATSAPP' AND external_message_id=%s""", mid)[0]["n"]
            with demo.lock:
                demo.timeline.append({"t": datetime.now(IST).strftime("%H:%M:%S"), "phone": phone, "dir": "sys",
                                      "text": f"WhatsApp delivered \"{text}\" AGAIN with the same message id. Webhook "
                                              f"answered {r.status_code}. Copies stored in the database: {out['copies']}."})
        return out

    @app.post("/demo/redeliver")
    def redeliver(b: dict = Body(...)):
        """F6: the queue hands the agent the same event again (SQS at-least-once)."""
        ev = demo.last_event.get(b["phone"])
        if not ev:
            raise HTTPException(409, "no event handled yet for this phone")
        res = demo.orch.handle(ev)
        out = "refused (already handled)" if res is None else "HANDLED AGAIN: " + " -> ".join(res.path)
        demo.note("redeliver", phone=b["phone"], text=out)
        replies = q("""SELECT count(*) AS n FROM setuhaul.chat_messages m WHERE m.sender_type='AGENT'
                         AND m.reply_to = %s""", ev.get("message_id"))[0]["n"]
        with demo.lock:
            demo.timeline.append({"t": datetime.now(IST).strftime("%H:%M:%S"), "phone": b["phone"], "dir": "sys",
                                  "text": f"The queue handed the agent the same message again. Agent: {out}. "
                                          f"Replies stored for that message: {replies}."})
        return {"result": out, "replies": replies}

    @app.get("/demo/phone/{phone}")
    def phone_view(phone: str):
        with demo.lock:
            return [m for m in demo.timeline if m["phone"] == phone]

    @app.get("/demo/state/{phone}")
    def state(phone: str):
        return jsonable({
            "thread": q("""SELECT t.thread_id, t.status, t.human_active, f.state AS flow_state
                             FROM setuhaul.chat_threads t JOIN setuhaul.drivers d USING (driver_id)
                             LEFT JOIN setuhaul.thread_flow f ON f.thread_id = t.thread_id WHERE d.phone_e164=%s""", phone),
            "appointments": q("""SELECT a.appointment_id, k.dock_code, lower(a.during) AS start, a.status,
                                        a.confirmation_basis AS basis, r.status AS approval, r.outcome_reason, r.decided_by
                                   FROM setuhaul.appointments a JOIN setuhaul.docks k USING (dock_id)
                                   JOIN setuhaul.shipments s USING (shipment_id) JOIN setuhaul.drivers d USING (driver_id)
                                   LEFT JOIN setuhaul.approval_requests r ON r.appointment_id = a.appointment_id
                                  WHERE d.phone_e164=%s ORDER BY a.created_at DESC LIMIT 5""", phone),
            "notifications": q("""SELECT n.notification_id, n.template_id, n.status, n.attempts
                                    FROM setuhaul.notifications n
                                   WHERE (n.shipment_id IN (SELECT s.shipment_id FROM setuhaul.shipments s JOIN setuhaul.drivers d USING (driver_id)
                                      WHERE d.phone_e164=%s)
                   OR n.thread_id IN (SELECT t.thread_id FROM setuhaul.chat_threads t JOIN setuhaul.drivers d USING (driver_id)
                                      WHERE d.phone_e164=%s)) ORDER BY n.created_at DESC LIMIT 8""", phone, phone),
        })

    @app.get("/demo/threads")
    def threads():
        return jsonable(q("""SELECT t.thread_id, d.driver_name, d.phone_e164 AS phone, t.shipment_id,
                                    s.destination_facility_id AS facility, t.status, t.human_active
                               FROM setuhaul.chat_threads t JOIN setuhaul.drivers d USING (driver_id)
                               LEFT JOIN setuhaul.shipments s ON s.shipment_id = t.shipment_id ORDER BY d.driver_name"""))

    @app.get("/demo/activity")
    def activity():
        with demo.lock:
            return demo.activity[-80:][::-1]

    @app.post("/demo/next-send")
    def next_send(b: dict = Body(...)):
        """F7: script what the WhatsApp stand-in does with this phone's next message."""
        if b["outcome"] not in ("timeout", "lost", "retry", "permanent", "ok"):
            raise HTTPException(400, "bad outcome")
        demo.provider.script_for[b["phone"]] = [b["outcome"]]
        demo.note("provider", phone=b["phone"], text=f"next send will be: {b['outcome']}")
        return {"ok": True}

    @app.post("/demo/receipt")
    def receipt(b: dict = Body(...)):
        """F7: Meta's signed delivery receipt for this phone's latest UNKNOWN message."""
        rows = q("""SELECT n.notification_id FROM setuhaul.notifications n
                    WHERE (n.shipment_id IN (SELECT s.shipment_id FROM setuhaul.shipments s JOIN setuhaul.drivers d USING (driver_id)
                                      WHERE d.phone_e164=%s)
                   OR n.thread_id IN (SELECT t.thread_id FROM setuhaul.chat_threads t JOIN setuhaul.drivers d USING (driver_id)
                                      WHERE d.phone_e164=%s)) AND n.status='UNKNOWN' ORDER BY n.created_at DESC LIMIT 1""", b["phone"], b["phone"])
        if not rows:
            raise HTTPException(409, "no message in UNKNOWN state for this phone")
        nid = str(rows[0]["notification_id"])
        r = signed_post({"entry": [{"changes": [{"value": {"statuses": [
            {"id": f"wamid.receipt.{uuid.uuid4().hex[:8]}", "status": "delivered", "biz_opaque_callback_data": nid}]}}]}]})
        demo.note("receipt", phone=b["phone"], text=f"delivered receipt for {nid}: {r.status_code}")
        return {"status": r.status_code, "notification_id": nid}

    @app.post("/demo/policy-rule")
    def policy_rule(b: dict = Body(...)):
        """F8: switch the demo-only auto-confirm rule for STANDARD requests at JAI on or off (a new rule version)."""
        on = bool(b.get("on"))
        v = (q("SELECT coalesce(max(version),0) AS v FROM setuhaul.confirmation_rules WHERE rule_id=%s", DEMO_RULE)[0]["v"]) + 1
        with psycopg.connect(DSN, autocommit=True) as c:
            if on:
                c.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome,
                             auto_eligible,authorised_by) VALUES (%s,%s,%s,1,'{"kind":"STANDARD"}','POLICY_CONFIRM',true,
                             'DEMO-ONLY')""", (DEMO_RULE, v, JAI))
            else:
                c.execute("""INSERT INTO setuhaul.confirmation_rules (rule_id,version,facility_id,priority,condition,outcome)
                             VALUES (%s,%s,%s,1,'{"retired": true}','NEEDS_PLANNER')""", (DEMO_RULE, v, JAI))
        demo.note("policy", text=f"demo auto-confirm rule {'ON' if on else 'OFF'} (version {v})")
        return {"on": on, "version": v}

    @app.get("/demo/policy-rule")
    def policy_state():
        rows = q("""SELECT outcome FROM setuhaul.confirmation_rules WHERE rule_id=%s ORDER BY version DESC LIMIT 1""", DEMO_RULE)
        return {"on": bool(rows) and rows[0]["outcome"] == "POLICY_CONFIRM"}

    @app.post("/demo/decide-twice")
    def decide_twice(b: dict = Body(...)):
        """F9: a planner confirms and an operations coordinator rejects the same request at the same moment."""
        appt, fac = b["appointment_id"], b.get("facility", JAI)
        gate, out = threading.Barrier(2), []

        def one(role, decision):
            tok = demo.token(role, fac, f"demo-{role}-f9")
            gate.wait()
            r = httpx.post(f"http://127.0.0.1:{PORT}/console/requests/{appt}/decision", timeout=30,
                           headers={"Authorization": f"Bearer {tok}"},
                           json={"decision": decision, "idempotency_key": uuid.uuid4().hex, "note": "F9 demo"})
            out.append({"who": role, "decision": decision, "status": r.status_code, "body": r.json()})
        ts = [threading.Thread(target=one, args=("planner", "CONFIRM")),
              threading.Thread(target=one, args=("operations", "REJECT"))]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        demo.note("F9", text="; ".join(f"{o['who']} {o['decision']} -> {o['status']}" for o in out))
        return out

    @app.get("/demo/checklist")
    def checklist():
        return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}

    @app.post("/demo/checklist")
    def save_checklist(b: dict = Body(...)):
        data = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
        data[b["flow"]] = {"result": b["result"], "note": b.get("note", ""),
                           "at": datetime.now(IST).isoformat(timespec="seconds")}
        RESULTS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="drop and recreate the demo database, then seed it")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        exists = c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DBNAME,)).fetchone()
    if a.reset or not exists:
        log.info("creating %s (migrate + seed)", DBNAME)
        reset_db()
    else:
        migrate(DSN)                      # forward-only: applies any migration added since the demo database was made
    demo = Demo()
    build(demo)
    threading.Thread(target=demo.loop, daemon=True, name="workers").start()
    print(f"\n  SetuHaul demo console: http://127.0.0.1:{PORT}/demo   (local only; production booking switch OFF)\n")
    uvicorn.run(demo.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
