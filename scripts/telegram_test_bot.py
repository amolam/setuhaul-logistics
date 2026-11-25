"""Local runner for the dedicated Telegram TEST bot (owner-run; never part of the automated tests).

What it does, in one process, with no public endpoint:
  * long-polls the Bot API (getUpdates) for the test bot and hands every update to the REAL webhook route
    (/webhooks/telegram) of an in-process API, with the webhook secret header, so secret checking, parsing,
    deduplication, linking, callbacks, replies, answered-callback spinners and button clearing run exactly as they
    would behind a webhook;
  * runs the driver's turns with the existing Orchestrator in its no-model mode (rules extractor + deterministic
    templates): a typed delay is read, recorded with record_driver_report, and answered. No model is called;
  * runs the existing OutboundRelay once per loop, so offers, outcomes, notices and the one-time location keyboard go
    out through the same outbox / relay as in AWS, to the test bot ONLY (AllowlistedTelegram);
  * serves the staff console on 127.0.0.1 with local staff tokens (no Cognito), for the dispatcher, planner and
    ops_lead steps of the journeys.

Fail closed. It refuses to start unless ALL hold (setuhaul.notify.telegram.telegram_from_env):
  SETUHAUL_TELEGRAM_DELIVERY=test_bot, SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED=true, SETUHAUL_ENV=local|dev,
  SETUHAUL_TELEGRAM_TEST_CHAT_IDS=<1-10 numeric ids>, a token (SETUHAUL_TELEGRAM_TEST_BOT_TOKEN locally, or
  SETUHAUL_TELEGRAM_BOT_SECRET_ID), and SETUHAUL_TEST_BOT_DSN naming a database whose name starts with
  `setuhaul_tgbot` (never a pilot or shared database). The bot must have NO webhook set (getUpdates and a webhook
  exclude each other); the runner stops if one is set rather than deleting it.

Secrets: the owner sets them in their own shell. Nothing here prints, logs or stores the token, the webhook secret or
chat ids. The webhook secret of the in-process route is random per run and never leaves the process.

  python scripts/telegram_test_bot.py --setup     # (re)create, migrate and seed the dedicated database: synthetic only
  python scripts/telegram_test_bot.py             # run; console at http://127.0.0.1:8781/app-runner/<role>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_NAME_RE = re.compile(r"\bdbname=(setuhaul_tgbot[a-z0-9_]*)(?:\s|$)")
IST = timezone(timedelta(hours=5, minutes=30))
ROLES = ("dispatcher", "planner", "ops_lead", "operations")


def config():
    from setuhaul.notify.telegram import telegram_from_env
    env = dict(os.environ)
    if env.get("SETUHAUL_TELEGRAM_DELIVERY") != "test_bot":
        sys.exit("refused: set SETUHAUL_TELEGRAM_DELIVERY=test_bot")
    dsn = env.get("SETUHAUL_TEST_BOT_DSN", "")
    if not DB_NAME_RE.search(dsn):
        sys.exit("refused: SETUHAUL_TEST_BOT_DSN must name a database starting with setuhaul_tgbot")
    try:
        transport, allow = telegram_from_env(env)
    except RuntimeError as e:
        sys.exit(f"refused: {e}")
    return dsn, transport, allow


def setup(dsn: str, drivers: int = 4) -> None:
    """A fresh database: two synthetic facilities, docks, 4 days of hourly slots, and synthetic shipments/drivers."""
    import psycopg

    from setuhaul.db.migrate import migrate
    name = DB_NAME_RE.search(dsn).group(1)
    with psycopg.connect(DB_NAME_RE.sub("dbname=postgres ", dsn), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name}")
    migrate(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO carriers VALUES ('CAR1','Synthetic Carrier',true)")
        c.execute("""INSERT INTO facilities (facility_id,facility_name,open_time,close_time,latitude,longitude) VALUES
                     ('FAC-JAI-01','Jaipur (synthetic)','00:00','23:59',26.9124,75.7873),
                     ('FAC-GGN-01','Gurugram (synthetic)','00:00','23:59',NULL,NULL)""")
        shifts = '[{"name":"DAY","start":"00:00","end":"12:00"},{"name":"NIGHT","start":"12:00","end":"00:00"}]'
        c.execute("""INSERT INTO facility_config (facility_id,cfg_version,gate_to_dock_min,changeover_min,no_show_grace_min,
                     warehouse_confirm_ttl_min,shifts,confirm_lead_min) VALUES
                     ('FAC-JAI-01',1,10,10,30,'{"DAY":15,"NIGHT":15}',%s,30),
                     ('FAC-GGN-01',1,15,10,20,'{"DAY":15,"NIGHT":15}',%s,30)""", (shifts, shifts))
        c.execute("""INSERT INTO docks (dock_id,facility_id,dock_code,dock_type,supports_refrigerated,max_vehicle_weight_kg) VALUES
                     ('DOCK-JAI-D1','FAC-JAI-01','D1','STANDARD',false,30000),('DOCK-JAI-D2','FAC-JAI-01','D2','STANDARD',false,30000),
                     ('DOCK-GGN-D1','FAC-GGN-01','D1','STANDARD',false,30000)""")
        base = datetime.now(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for dock in ("DOCK-JAI-D1", "DOCK-JAI-D2", "DOCK-GGN-D1"):
            for h in range(96):
                s = base + timedelta(hours=h)
                c.execute("INSERT INTO appointment_slots VALUES (%s,%s,tstzrange(%s,%s,'[)'),'OPEN',NULL)",
                          (f"S-{dock}-{h}", dock, s, s + timedelta(hours=1)))
        for i in range(1, drivers + 1):
            drv, sid, veh = f"DRV-TEST-{i}", f"SHP-TEST-{i}", f"VEH-TEST-{i}"
            c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1',%s,%s)",
                      (drv, f"Test driver {i}", f"+9100000000{i:02d}"))           # synthetic, unroutable numbers
            c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-TEST-{i}"))
            eta = datetime.now(IST).replace(second=0, microsecond=0) + timedelta(hours=3 + i)
            c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,
                         destination_facility_id,load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,
                         current_status) VALUES (%s,%s,'CAR1',%s,%s,'FAC-JAI-01',10000,45,%s,%s,'IN_TRANSIT')""",
                      (sid, f"ORD-TEST-{i}", drv, veh, eta - timedelta(hours=5), eta))
    print(f"database {name} ready: synthetic facilities, docks, slots and {drivers} test drivers / shipments")


def make_app(dsn: str, transport, allow, secret: str):
    """The real API app with an in-process JWKS for local staff tokens (no Cognito), the test bot behind replies."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from setuhaul.api.app import Settings, create_app
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="tgbot", alg="RS256", use="sig")
    iss = "https://cognito-idp.local/tgbot-runner"
    app = create_app(Settings(dsn=dsn, cognito_issuer=iss, cognito_client_ids={"tgbot"},
                              session_secret=secrets.token_bytes(40), meta_app_secret=secrets.token_bytes(40),
                              facilities=("FAC-JAI-01", "FAC-GGN-01"), jwks={"keys": [jwk]}, booking_enabled=True,
                              local_test_harness=True, cognito_domain="tgbot.invalid", console_client_id="tgbot",
                              environment="local test-bot runner", telegram_mode="test_bot", local_runner=True,
                              telegram_webhook_secret=secret, telegram_replies=transport, telegram_allowlist=allow))

    @app.get("/runner/token/{role}")
    def token(role: str):
        """Local staff token for the console (127.0.0.1 only). Each call is a different person (different `sub`), so
        the requester-is-not-the-decider rules hold between a dispatcher and a planner."""
        if role not in ROLES:
            return {"error": "unknown role"}
        now = int(time.time())
        return {"token": jwt.encode({"iss": iss, "sub": f"runner-{role}-{secrets.token_hex(3)}",
                                     "username": f"runner {role}", "token_use": "access", "client_id": "tgbot",
                                     "iat": now, "exp": now + 4 * 3600, "cognito:groups": [role],
                                     "facility_scope": "FAC-JAI-01"}, key, algorithm="RS256", headers={"kid": "tgbot"})}

    from fastapi.responses import HTMLResponse

    from setuhaul.api.console_site import STATIC
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/app-runner/{role}")
    def console_as(role: str):
        """The unchanged staff console, signed in with a local staff token for `role` (runner only, 127.0.0.1)."""
        if role not in ROLES:
            return HTMLResponse("unknown role", status_code=404)
        signin = ('<script>addEventListener("load", () => setTimeout(async () => {'
                  f'const t = (await (await fetch("/runner/token/{role}")).json()).token;'
                  'TOKEN = t; EXPIRES = Date.now() + 4 * 3600e3; showApp(); }, 800));</script>')
        return HTMLResponse(index.replace("<head>", '<head><base href="/app/">', 1).replace("</body>", signin + "</body>", 1))
    return app


def run(dsn: str, transport, allow, port: int) -> None:
    import httpx
    import uvicorn
    from fastapi.testclient import TestClient

    from setuhaul.agent.orchestrator import Orchestrator
    from setuhaul.api.db import Db
    from setuhaul.notify.outbound import LocalProvider, OutboundRelay
    secret = secrets.token_urlsafe(48)
    app = make_app(dsn, transport, allow, secret)
    bot = transport.provider                                   # TelegramBotProvider: the token lives only in its URLs
    info = bot.http.get(f"{bot._base}/getWebhookInfo")
    if info.status_code != 200 or not info.json().get("ok"):
        sys.exit("refused: the Bot API did not accept the token (getWebhookInfo)")
    if info.json()["result"].get("url"):
        sys.exit("refused: this bot has a webhook set; long polling needs none. Remove it yourself if intended.")
    threading.Thread(target=uvicorn.run, args=(app,), daemon=True,
                     kwargs={"host": "127.0.0.1", "port": port, "log_level": "warning"}).start()
    hook = TestClient(app)
    orch = Orchestrator(app.state.service, app.state.sessions, model=None)   # rules + templates; no model call
    relay = OutboundRelay(Db(dsn), LocalProvider(), telegram=transport)     # LocalProvider: never WhatsApp / SMS
    print(f"console: http://127.0.0.1:{port}/app-runner/<dispatcher|planner|ops_lead>; polling the test bot. Ctrl+C stops.")
    offset, poll = None, httpx.Client(timeout=35)
    while True:
        try:
            r = poll.get(f"{bot._base}/getUpdates", params={"timeout": 20, **({"offset": offset} if offset else {})})
            if r.status_code == 429:
                time.sleep(int((r.json().get("parameters") or {}).get("retry_after", 5)))
            for u in (r.json().get("result") or []) if r.status_code == 200 else []:
                offset = u["update_id"] + 1
                res = hook.post("/webhooks/telegram", content=json.dumps(u).encode(),
                                headers={"X-Telegram-Bot-Api-Secret-Token": secret, "Content-Type": "application/json"})
                print(f"update {u['update_id']}: webhook {res.status_code}")
            while (ev := app.state.inbound.receive()) is not None:          # the driver's turns, in order
                out = orch.handle(ev)
                print(f"turn on message {ev['message_id']}: {'done' if out else 'duplicate'}")
            sent = relay.run_once()
            if sent.get("claimed"):
                print("relay:", sent)
        except KeyboardInterrupt:
            return
        except Exception as e:                                  # noqa: BLE001 — keep polling; never print secrets
            print("loop error:", type(e).__name__)
            time.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--port", type=int, default=8781)
    a = ap.parse_args()
    dsn, transport, allow = config()
    if a.setup:
        setup(dsn)
    else:
        run(dsn, transport, allow, a.port)
