"""Phase G: an ISOLATED local environment for the evaluation (G2) and the load test (G7).

Local PostgreSQL (default: the throwaway test cluster on port 55432) + the real FastAPI app in-process, with locally
signed Cognito-style tokens, a local signed-webhook secret, the in-process inbound queue and NO network edges: no AWS,
no Meta, no paid model (the model is scripted or absent). It refuses any DSN that is not on localhost, so it can never
point at the pilot (Aurora via the RDS Data API) or anything else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import jwt
import psycopg
from cryptography.hazmat.primitives.asymmetric import rsa

from setuhaul.adapters.phase_g_dataset import build
from setuhaul.api.app import Settings, create_app
from setuhaul.db.migrate import migrate

IST = timezone(timedelta(hours=5, minutes=30))
BASE_DSN = os.environ.get("SETUHAUL_TEST_DSN", "host=localhost port=55432 user=postgres")
ISSUER = "https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_PHASEG"
CLIENT = "phase-g-local-console"
META_SECRET = b"phase-g-local-meta-secret-not-a-real-one-0123"
SESSION_SECRET = b"phase-g-local-session-secret-not-real-0123456789"
JAI, GGN = "FAC-JAI-01", "FAC-GGN-01"


def assert_local(dsn: str) -> None:
    parts = dict(p.split("=", 1) for p in dsn.split() if "=" in p)
    if parts.get("host") not in ("localhost", "127.0.0.1", "::1") or dsn.startswith("rds-data"):
        raise SystemExit(f"ISOLATION: Phase G evaluation and load tests run only against a local database, not {parts.get('host')}")


def fresh_database(name: str, target_day: date | None = None) -> tuple[str, dict]:
    assert_local(BASE_DSN)
    if not name.startswith("setuhaul_phase_g"):
        raise ValueError("Phase G databases are named setuhaul_phase_g*")
    with psycopg.connect(f"{BASE_DSN} dbname=postgres", autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name}")
    dsn = f"{BASE_DSN} dbname={name}"
    migrate(dsn)
    report = build(dsn, target_day)
    return dsn, report


@dataclass
class Keys:
    private: object
    jwks: dict


def make_keys() -> Keys:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
    jwk.update(kid="phase-g-kid", alg="RS256", use="sig")
    return Keys(k, {"keys": [jwk]})


def staff_token(keys: Keys, groups=("planner",), facilities=(JAI,), ttl_s: int = 3600) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": f"syn-staff-{uuid.uuid4().hex[:6]}", "token_use": "access", "client_id": CLIENT,
              "iat": now, "exp": now + ttl_s, "cognito:groups": list(groups), "facility_scope": ",".join(facilities)}
    return jwt.encode(claims, keys.private, algorithm="RS256", headers={"kid": "phase-g-kid"})


def make_app(dsn: str, keys: Keys, booking: bool = True, optimizer_ranking: bool = True):
    assert_local(dsn)
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys.jwks,
                               booking_enabled=booking, local_test_harness=booking,
                               optimizer_ranking=optimizer_ranking, environment="local-phase-g"))


def signed_message(phone: str, text: str, msg_id: str | None = None) -> tuple[bytes, dict]:
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": phone.lstrip("+"), "id": msg_id or f"wamid.{uuid.uuid4().hex}", "type": "text",
         "text": {"body": text}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(META_SECRET, body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def add_synthetic_driver(dsn: str, facility: str = JAI, eta_min: int = 120, unload: int = 45, priority: str = "NORMAL",
                         reefer: bool = False, kg: int = 10000, n_shipments: int = 1) -> tuple[str, str, list[str]]:
    """A synthetic driver (SYN- ids, +999 phone) with fresh shipments, as the owner's seed would add them."""
    u = uuid.uuid4().hex[:8].upper()
    drv, phone = f"SYN-DRV-{u}", f"+999{uuid.uuid4().int % 10**11:011d}"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        car = c.execute("SELECT carrier_id FROM carriers ORDER BY carrier_id LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,%s,'Synthetic Driver',%s)",
                  (drv, car, phone))
        sids = []
        for i in range(n_shipments):
            sid, veh = f"SYN-SHP-{u}-{i}", f"SYN-VEH-{u}-{i}"
            c.execute("INSERT INTO vehicles VALUES (%s,%s,'32FT',%s,45000,%s)", (veh, car, f"SYN-REG-{u}-{i}", reefer))
            c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,destination_facility_id,
                         load_weight_kg,temperature_control_required,priority_code,expected_unload_min,planned_departure_ts,
                         original_eta_ts,current_status)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()+make_interval(mins=>%s)-interval '5 hours',
                                 now()+make_interval(mins=>%s),'IN_TRANSIT')""",
                      (sid, f"SYN-ORD-{u}-{i}", car, drv, veh, facility, kg, reefer, priority, unload,
                       eta_min + i * 600, eta_min + i * 600))
            sids.append(sid)
    return drv, phone, sids


def now_ist() -> datetime:
    return datetime.now(IST)
