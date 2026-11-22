"""Option A end-to-end checks against the deployed AWS pilot (synthetic data, booking off, test-sink notifications).

    python scripts/e2e_aws.py SCENARIO [SCENARIO ...] --out docs/release/evidence/option_a.json

Runs with the operator's AWS CLI session (read the stack outputs, query the database as the owner through the Data
API, read the webhook signing secret). It creates SYN- drivers and shipments only, posts signed synthetic WhatsApp
webhooks to the real API Gateway URL, waits for the real Lambdas, and records for each scenario: input, API request
id, Lambda log lines (CloudWatch), database rows, expected versus actual, PASS/FAIL. Nothing is sent to Meta or a
phone: the outbound function uses the internal test sink, and a `testsink.` id is not a WhatsApp delivery.

Scenarios: A1 delay message -> extraction (model or rules), feasibility, options stored, reply; A2 driver says yes ->
no booking (switch off); A3 the reply reaches the test sink (SENT); A4 a refused number -> FAILED + escalation;
A5 model unavailable -> rules reader; A6 daily model budget spent -> rules reader, no paid call (marks today's budget
as spent: run it last). S1 the protections that remain without WAF, against the live API: unsigned and wrongly signed
webhooks refused, console without or with a forged token refused, malformed input refused, stage throttling set.

The pilot's schedules are woken before the scenarios and put back to sleep after them (`--keep-awake` to skip), so
the outbound worker delivers replies to the test sink during the run and Aurora can pause afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402
from setuhaul.aws.synthetic_seed import FACILITIES, synthetic_phone  # noqa: E402

REGION, STACK = "ap-southeast-2", "SetuHaulPilot"
SCOPE = f"SELECT set_config('app.facility_scope', '{','.join(f for f, _ in FACILITIES)},CENTRAL', true)"
REFUSED_PHONE = "+99900000009999"                      # SETUHAUL_TEST_SINK_FAIL_PHONES in the stack


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class Pilot:
    def __init__(self):
        cf = boto3.client("cloudformation", region_name=REGION)
        self.out = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
        self.db = OwnerSession(boto3.client("rds-data", region_name=REGION), self.out["ClusterArn"],
                               self.out["OwnerSecretArn"])
        sm = boto3.client("secretsmanager", region_name=REGION)
        self.meta_secret = json.loads(sm.get_secret_value(SecretId=self.out["AppSecretArn"])["SecretString"])[
            "META_APP_SECRET"].encode()
        if not self.meta_secret:
            raise SystemExit("META_APP_SECRET is empty in the application secret: set it first (any random string)")
        self.logs = boto3.client("logs", region_name=REGION)
        lam = boto3.client("lambda", region_name=REGION)
        self.log_groups = {n: lam.get_function_configuration(FunctionName=f"setuhaul-pilot-{n}")["LoggingConfig"]["LogGroup"]
                           for n in ("api", "agent", "outbound")}
        self.http = httpx.Client(timeout=30)

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(r) for r in self.db.query(sql, (SCOPE,))]

    def add_driver(self, phone: str | None = None, eta_min: int = 60) -> dict:
        u = uuid.uuid4().hex[:8].upper()
        d = {"driver_id": f"SYN-DRV-{u}", "vehicle_id": f"SYN-VEH-{u}", "shipment_id": f"SYN-SHP-{u}",
             "phone": phone or synthetic_phone(random.randrange(10**6, 10**10))}
        self.db.run(["SET LOCAL search_path = setuhaul", SCOPE,
                     f"INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES ({q(d['driver_id'])},"
                     f"'SYN-CAR1','Synthetic driver',{q(d['phone'])}) ON CONFLICT (phone_e164) DO NOTHING",
                     f"INSERT INTO vehicles VALUES ({q(d['vehicle_id'])},'SYN-CAR1','32FT',{q('SYN-REG-' + u)},20000,false)",
                     "INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,"
                     "destination_facility_id,load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,"
                     f"current_status) SELECT {q(d['shipment_id'])},{q('SYN-ORD-' + u)},'SYN-CAR1',driver_id,"
                     f"{q(d['vehicle_id'])},'FAC-JAI-01',10000,45,now()+make_interval(mins=>{eta_min})-interval '5 hours',"
                     f"now()+make_interval(mins=>{eta_min}),'IN_TRANSIT' FROM drivers WHERE phone_e164 = {q(d['phone'])}"])
        d["driver_id"] = self.rows(f"SELECT driver_id FROM setuhaul.drivers WHERE phone_e164 = {q(d['phone'])}")[0][0]
        return d

    def say(self, phone: str, text: str) -> dict:
        mid = f"wamid.SYN.{uuid.uuid4().hex}"
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": phone.lstrip("+"), "id": mid, "timestamp": str(int(time.time())), "type": "text",
             "text": {"body": text}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(self.meta_secret, body, hashlib.sha256).hexdigest()
        r = self.http.post(self.out["ApiUrl"].rstrip("/") + "/webhooks/whatsapp", content=body,
                           headers={"content-type": "application/json", "x-hub-signature-256": sig})
        return {"message_id": mid, "status": r.status_code, "body": r.text[:200],
                "api_request_id": r.headers.get("x-amzn-requestid") or r.headers.get("apigw-requestid")}

    def wait(self, sql: str, timeout_s: int = 150, every: float = 5) -> list[tuple]:
        end = time.time() + timeout_s
        while time.time() < end:
            got = self.rows(sql)
            if got:
                return got
            time.sleep(every)
        return []

    def log_lines(self, fn: str, needle: str, since_ms: int, limit: int = 5) -> list[str]:
        ev = self.logs.filter_log_events(logGroupName=self.log_groups[fn], startTime=since_ms,
                                         filterPattern=json.dumps(needle), limit=limit).get("events", [])
        return [e["message"].strip()[:400] for e in ev]

    def turn(self, phone: str, text: str) -> dict:
        since = int(time.time() * 1000) - 5000
        sent = self.say(phone, text)
        mid = sent["message_id"]
        driver_row = self.wait(f"SELECT message_id FROM setuhaul.chat_messages WHERE external_message_id = {q(mid)}", 60)
        reply = []
        if driver_row:
            reply = self.wait(f"""SELECT message_id, left(body_masked, 300) FROM setuhaul.chat_messages
                                   WHERE sender_type = 'AGENT' AND reply_to = {driver_row[0][0]}""", 180)
        return {**sent, "stored_driver_message": driver_row[0][0] if driver_row else None,
                "agent_reply": reply[0][1] if reply else None, "agent_reply_id": reply[0][0] if reply else None,
                "agent_log": self.log_lines("agent", mid, since)}


def result(case, scenario, inp, evidence, checks) -> dict:
    return {"case": case, "scenario": scenario, "input": inp, "evidence": evidence,
            "checks": {k: bool(v) for k, v in checks.items()}, "status": "PASS" if all(checks.values()) else "FAIL",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def a1(p: Pilot) -> dict:
    d = p.add_driver(eta_min=60)
    t = p.turn(d["phone"], "Traffic jam near Shahpura, I will reach about 2 hours late")
    usage = p.rows(f"""SELECT status, coalesce(error_category,''), purpose, model FROM setuhaul.model_usage
                        WHERE created_at > now() - interval '5 minutes' ORDER BY created_at DESC LIMIT 3""")
    eta = p.rows(f"SELECT source, derived_eta FROM setuhaul.eta_signals WHERE shipment_id = {q(d['shipment_id'])} "
                 "ORDER BY observed_at DESC LIMIT 1")
    opts = p.rows(f"SELECT count(*) FROM setuhaul.shown_options WHERE shipment_id = {q(d['shipment_id'])}")
    appts = p.rows(f"SELECT count(*) FROM setuhaul.appointments WHERE shipment_id = {q(d['shipment_id'])}")
    ev = {**d, **t, "model_usage_recent": usage, "eta_signal": eta, "shown_options": opts[0][0], "appointments": appts[0][0]}
    return result("A1", "delay message: extraction, policy, feasibility against Aurora, options returned", t["message_id"], ev, {
        "webhook accepted (200)": t["status"] == 200, "driver message stored": t["stored_driver_message"],
        "agent reply stored": t["agent_reply"], "driver ETA recorded": eta, "options computed and stored": opts[0][0] > 0,
        "no booking": appts[0][0] == 0})


def a2(p: Pilot, d: dict | None = None) -> dict:
    d = d or p.add_driver(eta_min=60)
    first = p.turn(d["phone"], "Stuck in traffic, reaching 2 hours late")
    yes = p.turn(d["phone"], "1")
    appts = p.rows(f"SELECT count(*) FROM setuhaul.appointments WHERE shipment_id = {q(d['shipment_id'])}")
    reqs = p.rows(f"SELECT count(*) FROM setuhaul.approval_requests WHERE shipment_id = {q(d['shipment_id'])}")
    return result("A2", "driver picks an option: processed, no booking committed (switch off)", [first["message_id"], yes["message_id"]],
                  {**d, "first": first, "confirm": yes, "appointments": appts[0][0], "approval_requests": reqs[0][0]}, {
                      "options offered": first["agent_reply"], "confirmation answered": yes["agent_reply"],
                      "no appointment": appts[0][0] == 0, "no approval request": reqs[0][0] == 0})


def a3(p: Pilot) -> dict:
    d = p.add_driver(eta_min=60)
    t = p.turn(d["phone"], "Running 90 minutes late because of a puncture")
    n = p.wait(f"""SELECT status, provider_message_id, attempts FROM setuhaul.notifications
                    WHERE chat_message_id = {t['agent_reply_id'] or 0} AND status IN ('SENT','DELIVERED','FAILED')""", 150)
    return result("A3", "the agent's reply goes to the internal test sink (not WhatsApp)", t["message_id"],
                  {**d, **t, "notification": n}, {
                      "reply stored": t["agent_reply_id"], "notification SENT": n and n[0][0] == "SENT",
                      "test-sink id, not a WhatsApp id": n and str(n[0][1]).startswith("testsink.")})


def a4(p: Pilot) -> dict:
    existing = p.rows(f"SELECT driver_id FROM setuhaul.drivers WHERE phone_e164 = {q(REFUSED_PHONE)}")
    d = p.add_driver(phone=REFUSED_PHONE, eta_min=60)
    t = p.turn(d["phone"], "Will be 2 hours late, sorry")
    n = p.wait(f"""SELECT status, last_error, notification_id FROM setuhaul.notifications
                    WHERE chat_message_id = {t['agent_reply_id'] or 0} AND status IN ('SENT','FAILED')""", 150)
    esc = p.rows(f"""SELECT reason_code, mode FROM setuhaul.escalations WHERE reason_code = 'NOTIFICATION_FAILED'
                      AND handoff->>'notification_id' = {q(str(n[0][2]) if n else '')}""")
    return result("A4", "a notification the sink refuses fails safely and a person is told", t["message_id"],
                  {**d, **t, "notification": n, "escalation": esc, "driver_reused": bool(existing)}, {
                      "notification FAILED": n and n[0][0] == "FAILED", "reason recorded": n and n[0][1] == "TEST_SINK_REFUSED",
                      "escalation raised": esc})


def a5(p: Pilot) -> dict:
    d = p.add_driver(eta_min=60)
    t = p.turn(d["phone"], "Traffic, reaching 2 hours late")
    usage = p.rows("""SELECT status, coalesce(error_category,'') FROM setuhaul.model_usage
                       WHERE created_at > now() - interval '5 minutes' ORDER BY created_at DESC LIMIT 3""")
    rules = [l for l in t["agent_log"] if "extractor=rules" in l or "fallback" in l]
    return result("A5", "model unavailable (OpenRouter refuses the key): the rules reader answers", t["message_id"],
                  {**d, **t, "model_usage_recent": usage}, {
                      "reply stored": t["agent_reply"], "model attempt settled as an error": any(u[0] == "ERROR" for u in usage),
                      "rules reader used": rules})


def a6(p: Pilot) -> dict:
    p.db.run(["SET LOCAL search_path = setuhaul",
              "INSERT INTO model_budget_day (day, environment) VALUES ((clock_timestamp() AT TIME ZONE 'UTC')::date, "
              "'pilot-ap-southeast-2') ON CONFLICT (day) DO NOTHING",
              "UPDATE model_budget_day SET spent_usd = greatest(spent_usd, 0.25) "
              "WHERE day = (clock_timestamp() AT TIME ZONE 'UTC')::date"])
    d = p.add_driver(eta_min=60)
    t = p.turn(d["phone"], "Traffic, reaching 2 hours late")
    usage = p.rows("""SELECT status, coalesce(error_category,''), coalesce(cost_usd, 0) FROM setuhaul.model_usage
                       WHERE created_at > now() - interval '3 minutes' ORDER BY created_at DESC LIMIT 3""")
    return result("A6", "daily model budget spent: no paid call, the rules reader answers", t["message_id"],
                  {**d, **t, "model_usage_recent": usage}, {
                      "reply stored": t["agent_reply"],
                      "no attempt settled as OK": not any(u[0] == "OK" for u in usage),
                      "refused by the budget": any(u[0] == "BUDGET_EXHAUSTED" for u in usage) or not usage})


def s1(p: Pilot) -> dict:
    base = p.out["ApiUrl"].rstrip("/")
    body = json.dumps({"entry": []}).encode()
    unsigned = p.http.post(base + "/webhooks/whatsapp", content=body, headers={"content-type": "application/json"})
    wrong = p.http.post(base + "/webhooks/whatsapp", content=body,
                        headers={"content-type": "application/json", "x-hub-signature-256": "sha256=" + "0" * 64})
    signed = p.http.post(base + "/webhooks/whatsapp", content=body, headers={
        "content-type": "application/json",
        "x-hub-signature-256": "sha256=" + hmac.new(p.meta_secret, body, hashlib.sha256).hexdigest()})
    no_token = p.http.get(base + "/console/requests")
    forged = p.http.get(base + "/console/requests", headers={"authorization": "Bearer eyJhbGciOiJub25lIn0.e30."})
    bad_input = p.http.post(base + "/console/requests/x/decision", headers={"authorization": "Bearer x"},
                            content=b"{not json", )
    tools = p.http.post(base + "/tools/eta", json={"turn_id": 0, "args": {}})
    apigw = boto3.client("apigateway", region_name=REGION)
    api_id = base.split("//", 1)[1].split(".", 1)[0]
    stage = apigw.get_stage(restApiId=api_id, stageName="pilot")
    throttle = stage.get("methodSettings", {}).get("*/*", {})
    codes = {"unsigned": unsigned.status_code, "wrong_signature": wrong.status_code, "signed": signed.status_code,
             "console_no_token": no_token.status_code, "console_forged_token": forged.status_code,
             "malformed_input": bad_input.status_code, "tools_route": tools.status_code}
    return result("S1", "protections kept without WAF, against the live API", "probes", {
        "status_codes": codes, "stage_throttling": {k: throttle.get(k) for k in ("throttlingRateLimit", "throttlingBurstLimit")}}, {
        "unsigned webhook refused (401)": codes["unsigned"] == 401, "wrong signature refused (401)": codes["wrong_signature"] == 401,
        "signed webhook accepted (200)": codes["signed"] == 200, "console without a token refused (401)": codes["console_no_token"] == 401,
        "console with a forged token refused (401)": codes["console_forged_token"] == 401,
        "malformed input refused (401/422)": codes["malformed_input"] in (401, 422),
        "/tools is off (404)": codes["tools_route"] == 404,
        "stage throttling 20/40": throttle.get("throttlingRateLimit") == 20 and throttle.get("throttlingBurstLimit") == 40})


SCENARIOS = {"A1": a1, "A2": a2, "A3": a3, "A4": a4, "A5": a5, "A6": a6, "S1": s1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="+", choices=sorted(SCENARIOS))
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "option_a.json"))
    ap.add_argument("--keep-awake", action="store_true", help="leave the schedules on after the run")
    a = ap.parse_args()
    from setuhaul.ops.pilot_mode import from_stack
    mode = from_stack()
    print(json.dumps({"wake": mode.wake()["after"]["mode"]}))
    p = Pilot()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    for s in a.scenarios:
        r = SCENARIOS[s](p)
        print(json.dumps({"case": r["case"], "status": r["status"], "checks": r["checks"]}))
        done.append(r)
        out.write_text(json.dumps(done, indent=2, default=str), encoding="utf-8")
    if not a.keep_awake:
        print(json.dumps({"sleep": mode.sleep()["after"]}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
