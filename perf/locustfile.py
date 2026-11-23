"""Phase G7 load test: 4-5 concurrent users against the ISOLATED local environment (perf/serve_local.py) only.

    locust -f perf/locustfile.py --headless -u 5 -r 1 -t 3m --host http://127.0.0.1:8765 --csv <scratch>/perf
    (PERF_FIXTURES=<the --fixtures file of serve_local>)

Workflows (request names in the Locust report):
  driver   1 delay report        POST /webhooks/whatsapp  "Traffic, reaching by 4:30 pm"  -> agent turn: report + optimizer
           2 selection           POST /webhooks/whatsapp  "1"                              -> read-back
           3 booking request     POST /webhooks/whatsapp  "haan ji, kar do"                -> hold + request (pending)
  planner  4 optimization        GET  /console/shipments/{id}/recommendations
           5 planner approval    GET  /console/requests, POST /console/requests/{id}/decision CONFIRM
           6 dashboard view      GET  /console/observability
Agent turns run asynchronously in the server's workers; their latency, the optimizer time and the queue time are
measured on the server side (perf_db_measurements.json). Refuses any host that is not 127.0.0.1/localhost.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from locust import HttpUser, between, events, task

IST = timezone(timedelta(hours=5, minutes=30))
FX = json.loads(open(os.environ["PERF_FIXTURES"], encoding="utf-8").read())
SECRET = FX["meta_secret"].encode()
_pool = itertools.count()
_lock = threading.Lock()


@events.test_start.add_listener
def _isolation(environment, **_):
    host = urlparse(environment.host or "").hostname
    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"ISOLATION: the Phase G load test runs only against a local server, not {host}")


def clock_in(minutes: int) -> str:
    t = datetime.now(IST) + timedelta(minutes=minutes)
    t -= timedelta(minutes=t.minute % 5)
    return f"{(t.hour % 12) or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


class DriverUser(HttpUser):
    """One synthetic driver per flow: delay report -> pick option 1 -> confirm."""
    weight = 3
    wait_time = between(1.5, 3.0)

    def on_start(self):
        self.next_driver()

    def next_driver(self):
        with _lock:
            self.d = FX["drivers"][next(_pool) % len(FX["drivers"])]
        self.step = 0

    def send(self, text: str, name: str):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": self.d["phone"].lstrip("+"), "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
             "text": {"body": text}}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        with self.client.post("/webhooks/whatsapp", data=body, name=name, catch_response=True,
                              headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"}) as r:
            if r.status_code != 200 or r.json() != {"status": "received"}:
                r.failure(f"{r.status_code} {r.text[:80]}")

    @task
    def flow(self):
        if self.step == 0:
            self.send(f"Traffic, reaching by {clock_in(random.choice((90, 120, 150, 180)))}", "1 delay report (webhook)")
        elif self.step == 1:
            self.send("1", "2 selection (webhook)")
        elif self.step == 2:
            self.send("haan ji, kar do", "3 booking request (webhook)")
        self.step += 1
        if self.step == 3:
            self.next_driver()


class PlannerUser(HttpUser):
    weight = 2
    wait_time = between(1.0, 2.5)

    def on_start(self):
        self.h = {"Authorization": "Bearer " + FX["ops_lead"]}

    @task(3)
    def optimization(self):
        d = random.choice(FX["drivers"])
        with self.client.get(f"/console/shipments/{d['shipment_id']}/recommendations", headers=self.h,
                             name="4 optimization (recommendations)", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"{r.status_code}")
            elif r.json()["solver_status"] not in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "MODEL_INVALID"):
                r.failure("unsafe solver status " + r.json()["solver_status"])

    @task(2)
    def approval(self):
        r = self.client.get("/console/requests", headers=self.h, name="5a planner approval: list")
        if r.status_code != 200:
            return
        reqs = r.json()["requests"]
        if reqs:
            x = random.choice(reqs)
            with self.client.post(f"/console/requests/{x['appointment_id']}/decision", headers=self.h,
                                  json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex, "note": "perf"},
                                  name="5b planner approval: decide", catch_response=True) as d:
                if d.status_code in (200, 409):                    # 409: another user decided it first (expected race)
                    d.success()
                else:
                    d.failure(f"{d.status_code} {d.text[:80]}")

    @task(1)
    def dashboard(self):
        self.client.get("/console/observability?hours=1", headers=self.h, name="6 dashboard view")
