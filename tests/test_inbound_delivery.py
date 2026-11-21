"""Durable inbound delivery (B-WF-1; review findings B2-14 and B2-15; migration 0011).

The acceptance criteria from 08_signoff_and_backlog.md §4, step 4:
  * 4 simultaneous HTTP deliveries of one webhook → 1 stored message, 1 queue event (through the FastAPI route);
  * a crash between the claim and the publish loses nothing (the recovery sweep publishes it);
  * a duplicate after 6 minutes, outside the queue's 5-minute deduplication window, is still rejected (one turn);
plus: a crashed turn is delivered again, a dead worker's lease expires, and the SQS adapter's ids.
"""
import hashlib
import hmac
import json
import threading
import time
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from setuhaul.agent.models import FakeModel
from setuhaul.agent.orchestrator import Orchestrator, TurnLeaseLost
from setuhaul.api.app import Settings, create_app
from setuhaul.api.inbound import (REQUIRED_GATES, BookingGateError, LocalQueue, SqsFifoQueue, database_fingerprint,
                                  dedup_id, deployment_id_for, manifest_fingerprints)
from test_api import (CLIENT, ISSUER, JAI, GGN, META_SECRET, SESSION_SECRET, Driver, add_driver, app, client, dsn,  # noqa: F401
                      keys)
from test_orchestrator import exd


def body_for(phone: str, ext: str, text: str) -> bytes:
    return json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": phone.lstrip("+"), "id": ext, "type": "text", "text": {"body": text}}]}}]}]}).encode()


def post(app_, body: bytes):
    sig = "sha256=" + hmac.new(META_SECRET, body, hashlib.sha256).hexdigest()
    return TestClient(app_).post("/webhooks/whatsapp", content=body,
                                 headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def receipt(dsn_, ext: str):
    with psycopg.connect(dsn_) as c:
        return c.execute("""SELECT r.message_id, r.enqueued, r.published_at IS NOT NULL, r.processed_at IS NOT NULL,
                                   r.deliveries, r.generation
                              FROM setuhaul.inbound_receipts r JOIN setuhaul.chat_messages m USING (message_id)
                             WHERE m.external_message_id=%s""", (ext,)).fetchone()


def events_for(app_, ext: str) -> list[dict]:
    return [e for e in app_.state.inbound if e.get("external_message_id") == ext]


def agent_replies(dsn_, thread: str) -> int:
    with psycopg.connect(dsn_) as c:
        return c.execute("SELECT count(*) FROM setuhaul.chat_messages WHERE thread_id=%s AND sender_type='AGENT'",
                         (thread,)).fetchone()[0]


def orchestrator(app_, **kw):
    model = FakeModel(extractions=[exd(delay_kind="DELAY_DURATION", declared_delay_min=45)] * 4)
    return Orchestrator(app_.state.service, app_.state.sessions, model, **kw)


# ---------------------------------------------------------------- B2-15
def test_four_simultaneous_http_deliveries_store_one_message_and_queue_one_event(app, dsn):
    _, phone, _ = add_driver(dsn)
    ext = f"wamid.{uuid.uuid4().hex}"
    body, barrier, codes = body_for(phone, ext, "late by about an hour, tyre puncture"), threading.Barrier(4), []

    def deliver():
        barrier.wait()
        codes.append(post(app, body).status_code)
    ths = [threading.Thread(target=deliver) for _ in range(4)]
    [t.start() for t in ths]
    [t.join(60) for t in ths]
    assert codes == [200] * 4
    with psycopg.connect(dsn) as c:
        n = c.execute("SELECT count(*) FROM setuhaul.chat_messages WHERE external_message_id=%s", (ext,)).fetchone()[0]
    assert n == 1
    assert len(events_for(app, ext)) == 1
    _, enqueued, published, processed, _, _ = receipt(dsn, ext)
    assert (enqueued, published, processed) == (True, True, False)          # queued once, waiting for its turn


# ---------------------------------------------------------------- B2-14
def test_a_crash_between_claim_and_publish_loses_nothing(app, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    ext = f"wamid.{uuid.uuid4().hex}"
    q = app.state.inbound

    def crash(*a, **kw):
        raise RuntimeError("process died before the queue accepted the message")
    monkeypatch.setattr(q, "send", crash)
    assert post(app, body_for(phone, ext, "stuck at the toll, late by 40 min")).status_code == 200
    monkeypatch.undo()
    assert events_for(app, ext) == []                                       # nothing reached the queue…
    mid, enqueued, published, _, _, _ = receipt(dsn, ext)
    assert (enqueued, published) == (True, False)                           # …but the receipt says so
    assert post(app, body_for(phone, ext, "stuck at the toll, late by 40 min")).status_code == 200
    assert events_for(app, ext) == []                                       # a provider retry does not double it
    assert app.state.relay.recover(unpublished_after_s=0) >= 1              # the sweep publishes it
    evs = events_for(app, ext)
    assert len(evs) == 1 and evs[0]["message_id"] == mid
    assert receipt(dsn, ext)[2] is True
    app.state.relay.recover(unpublished_after_s=0)
    assert len(events_for(app, ext)) == 1                                   # published once only
    r = orchestrator(app).handle(evs[0])                                    # and it is answered
    assert r is not None and receipt(dsn, ext)[3] is True


def test_duplicate_after_six_minutes_is_still_rejected(app, client, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    ev = d.say("late by 45 min")
    orch = orchestrator(app)
    assert orch.handle(ev) is not None
    replies = agent_replies(dsn, d.thread)
    assert app.state.relay.publish(ev) is False                             # inside 5 min: the queue drops it
    q = app.state.inbound
    later = q.clock() + 6 * 60
    monkeypatch.setattr(q, "clock", lambda: later)
    assert app.state.relay.publish(ev) is True                              # outside the window the queue takes it…
    assert orch.handle(ev) is None                                          # …and the database refuses the turn
    assert agent_replies(dsn, d.thread) == replies                          # no second answer to the driver
    assert receipt(dsn, ev["external_message_id"])[4] == 1                  # one delivery was processed


def test_a_crashed_turn_is_delivered_again(app, client, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    ev = d.say("late by 45 min")
    orch = orchestrator(app)
    real = orch._turn
    monkeypatch.setattr(orch, "_turn", lambda *a: (_ for _ in ()).throw(RuntimeError("worker crashed mid-turn")))
    with pytest.raises(RuntimeError):
        orch.handle(ev)
    assert receipt(dsn, ev["external_message_id"])[3] is False              # not processed: the lease was released
    monkeypatch.setattr(orch, "_turn", real)
    assert orch.handle(ev) is not None                                      # the redelivery is answered
    assert orch.handle(ev) is None                                          # and only once


def test_a_dead_workers_lease_expires_and_recovery_redelivers(app, client, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    ev = d.say("late by 45 min")
    ext = ev["external_message_id"]
    svc, p = app.state.service, app.state.sessions.verify(ev["session_token"])
    assert svc.claim_turn(p, ev["message_id"], "worker-that-dies", lease_s=1)
    orch = orchestrator(app)
    assert orch.handle(ev) is None                                          # a live lease: someone is on it
    q = app.state.inbound
    later = q.clock() + 600
    monkeypatch.setattr(q, "clock", lambda: later)                          # past the queue's deduplication window
    app.state.relay.recover(unpublished_after_s=0, stalled_after_s=0)
    assert len(events_for(app, ext)) == 1                                   # not while the lease is live
    time.sleep(1.2)                                                         # the worker never comes back
    app.state.relay.recover(unpublished_after_s=0, stalled_after_s=0)
    evs = events_for(app, ext)
    assert len(evs) == 2 and evs[-1]["redelivered"] is True
    assert orch.handle(evs[-1]) is not None                                 # answered by a live worker
    assert receipt(dsn, ext)[3:5] == (True, 2)                              # processed; two claims in all


def test_a_turn_cannot_be_claimed_with_another_threads_session(app, client, dsn):
    _, phone_a, _ = add_driver(dsn)
    _, phone_b, _ = add_driver(dsn)
    a, b = Driver(client, phone_a), Driver(client, phone_b)
    ev_a, ev_b = a.say("late by 45 min"), b.say("late by 45 min")
    forged = dict(ev_a, session_token=ev_b["session_token"])                # b's session, a's message
    assert orchestrator(app).handle(forged) is None
    assert receipt(dsn, ev_a["external_message_id"])[3] is False


# ---------------------------------------------------------------- queue adapters
def test_local_queue_deduplicates_for_five_minutes_only():
    now = [0.0]
    q = LocalQueue(clock=lambda: now[0])
    assert q.send({"n": 1}, "wamid.X#0", "DRV-1") is True
    assert q.send({"n": 2}, "wamid.X#0", "DRV-1") is False
    assert q.send({"n": 3}, "wamid.X#1", "DRV-1") is True                  # a new generation is a new message
    now[0] = 301
    assert q.send({"n": 4}, "wamid.X#0", "DRV-1") is True
    assert [e["n"] for e in q] == [1, 3, 4] and q.receive() == {"n": 1}


def test_sqs_fifo_queue_uses_driver_group_and_a_valid_deduplication_id():
    class Stub:
        def __init__(self):
            self.calls = []

        def send_message(self, **kw):
            self.calls.append(kw)
    stub = Stub()
    ev = {"external_message_id": "wamid." + "A" * 200 + "==", "generation": 2, "driver_id": "DRV-9", "message_id": 7}
    assert SqsFifoQueue("https://sqs.ap-south-1.amazonaws.com/000000000000/inbound.fifo", stub).send(
        ev, dedup_id(ev), ev["driver_id"])
    kw = stub.calls[0]
    assert kw["MessageGroupId"] == "DRV-9"
    assert len(kw["MessageDeduplicationId"]) <= 128 and kw["MessageDeduplicationId"].isalnum()
    assert json.loads(kw["MessageBody"])["message_id"] == 7


# ================================================================ review R1.9 (migration 0012)
def bump_generation(dsn_, message_id: int) -> None:
    """What ingest does when a provider retry recovers a FAILED attestation (same SQL, same lock)."""
    with psycopg.connect(dsn_, autocommit=True) as c:
        with c.transaction():
            tid = c.execute("SELECT thread_id FROM setuhaul.inbound_receipts WHERE message_id=%s", (message_id,)).fetchone()[0]
            c.execute("SELECT setuhaul.thread_lock(%s)", (tid,))
            c.execute("""UPDATE setuhaul.inbound_receipts SET generation=generation+1, enqueued=true, enqueued_at=now(),
                                published_at=NULL, processed_at=NULL, processing_owner=NULL, processing_until=NULL
                          WHERE message_id=%s""", (message_id,))


def replies_to(dsn_, message_id: int) -> int:
    with psycopg.connect(dsn_) as c:
        return c.execute("SELECT count(*) FROM setuhaul.chat_messages WHERE sender_type='AGENT' AND reply_to=%s",
                         (message_id,)).fetchone()[0]


# ---------------------------------------------------------------- S4-01
def test_s4_01_a_message_that_was_not_stored_is_not_acknowledged(app, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    ext = f"wamid.{uuid.uuid4().hex}"
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.broken", "type": "text"},                                     # malformed: still skipped
        {"from": phone.lstrip("+"), "id": ext, "type": "text", "text": {"body": "late by 30 min"}}]}}]}]}).encode()
    svc = app.state.service
    monkeypatch.setattr(svc, "ingest", lambda *a: (_ for _ in ()).throw(psycopg.OperationalError("database down")))
    r = post(app, body)
    assert r.status_code == 503 and "session_token" not in r.text                  # the provider retries
    assert receipt(dsn, ext) is None
    monkeypatch.undo()
    assert post(app, body).status_code == 200                                       # the retry stores it once
    assert receipt(dsn, ext) is not None and len(events_for(app, ext)) == 1


# ---------------------------------------------------------------- S4-03
def test_s4_03_an_event_for_another_generation_is_refused(app, client, dsn):
    _, phone, _ = add_driver(dsn)
    ev = Driver(client, phone).say("late by 45 min")
    p = app.state.sessions.verify(ev["session_token"])
    assert not app.state.service.claim_turn(p, ev["message_id"], "w", 120, generation=1)
    assert orchestrator(app).handle(dict(ev, generation=3)) is None
    assert orchestrator(app).handle(ev) is not None                                  # the current generation runs


def _worker_in_flight(app, ev, gate: threading.Event, done: threading.Event, out: dict, **kw):
    """A worker that claims the turn, then waits at `gate` before its first write (the reply)."""
    orch = orchestrator(app, **kw)

    def slow_turn(p, event):
        gate.wait(10)
        app.state.service.record_reply(p, "stale reply", reply_to=int(event["message_id"]),
                                       generation=int(event.get("generation", 0)))
    orch._turn = slow_turn

    def run():
        try:
            out["result"] = orch.handle(ev)
        except Exception as e:                                                       # noqa: BLE001
            out["error"] = e
        done.set()
    th = threading.Thread(target=run)
    th.start()
    return th


def test_s4_02_a_worker_superseded_by_a_new_generation_commits_nothing(app, client, dsn):
    _, phone, _ = add_driver(dsn)
    ev = Driver(client, phone).say("late by 45 min")
    gate, done, out = threading.Event(), threading.Event(), {}
    th = _worker_in_flight(app, ev, gate, done, out)
    time.sleep(0.5)                                                                  # it holds the lease now
    bump_generation(dsn, ev["message_id"])                                           # a retry recovered the attestation
    gate.set()
    th.join(15)
    assert "TURN_FENCED" in str(out.get("error")), out                              # its write is refused…
    assert replies_to(dsn, ev["message_id"]) == 0                                    # …nothing reached the driver
    assert receipt(dsn, ev["external_message_id"])[3] is False                      # and it did not mark it processed
    assert orchestrator(app).handle(dict(ev, generation=1)) is not None             # the new generation gets its turn


def test_s4_02_the_heartbeat_keeps_a_long_turn_and_nobody_else_gets_it(app, client, dsn):
    _, phone, _ = add_driver(dsn)
    ev = Driver(client, phone).say("late by 45 min")
    gate, done, out = threading.Event(), threading.Event(), {}
    th = _worker_in_flight(app, ev, gate, done, out, lease_s=2)                     # renewed every 2/3 s
    time.sleep(5)                                                                    # well past two leases
    assert orchestrator(app).handle(ev) is None                                      # still held: refused
    gate.set()
    th.join(15)
    assert "error" not in out, out
    assert receipt(dsn, ev["external_message_id"])[3] is True and replies_to(dsn, ev["message_id"]) == 1


def test_s4_02_a_worker_that_outlives_its_lease_is_fenced(app, client, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    ev = Driver(client, phone).say("late by 45 min")
    monkeypatch.setattr(app.state.service, "renew_turn", lambda *a: False)         # the heartbeat cannot renew
    gate, done, out = threading.Event(), threading.Event(), {}
    th = _worker_in_flight(app, ev, gate, done, out, lease_s=2)
    time.sleep(2.6)                                                                  # the lease has expired
    monkeypatch.undo()
    assert orchestrator(app).handle(ev) is not None                                  # a live worker takes over
    gate.set()
    th.join(15)
    assert "TURN_FENCED" in str(out.get("error")), out                              # the old one commits nothing
    assert replies_to(dsn, ev["message_id"]) == 1                                    # only the live worker's reply


# ---------------------------------------------------------------- S4-08
def test_s4_08_a_crash_after_the_reply_before_finishing_sends_one_reply(app, client, dsn, monkeypatch):
    _, phone, _ = add_driver(dsn)
    ev = Driver(client, phone).say("late by 45 min")
    svc = app.state.service
    real = svc.finish_turn
    monkeypatch.setattr(svc, "finish_turn", lambda *a: (_ for _ in ()).throw(RuntimeError("crashed before finishing")))
    with pytest.raises(RuntimeError):
        orchestrator(app).handle(ev)
    assert replies_to(dsn, ev["message_id"]) == 1
    monkeypatch.setattr(svc, "finish_turn", real)
    assert orchestrator(app).handle(ev) is not None                                  # redelivered and re-run…
    assert replies_to(dsn, ev["message_id"]) == 1                                    # …still one reply
    assert receipt(dsn, ev["external_message_id"])[3] is True


# ---------------------------------------------------------------- S4-04
def backfill_as_0011(dsn_, *message_ids):
    """What migration 0011 did to receipts written under the old in-memory path."""
    with psycopg.connect(dsn_, autocommit=True) as c:
        c.execute("""UPDATE setuhaul.inbound_receipts SET published_at=created_at, processed_at=created_at
                      WHERE message_id = ANY(%s)""", (list(message_ids),))


def test_s4_04_cutover_lists_every_legacy_receipt_and_reopens_them(app, client, dsn):
    _, phone_a, _ = add_driver(dsn)
    _, phone_b, _ = add_driver(dsn)
    lost = Driver(client, phone_a).say("late by 45 min")                             # never answered
    answered = Driver(client, phone_b).say("late by 45 min")
    svc = app.state.service
    svc.record_reply(app.state.sessions.verify(answered["session_token"]), "old-path reply")
    backfill_as_0011(dsn, lost["message_id"], answered["message_id"])
    rows = {r["message_id"]: r for r in svc.legacy_uncertain_receipts()}
    assert rows[lost["message_id"]]["later_agent_messages"] == 0
    assert rows[answered["message_id"]]["later_agent_messages"] == 1                 # listed, with the evidence
    assert svc.reopen_receipts([lost["message_id"]]) == 1
    app.state.relay.recover()                                                        # the next sweep publishes it
    ev = events_for(app, lost["external_message_id"])[-1]
    assert ev["generation"] == 1 and orchestrator(app).handle(ev) is not None
    assert lost["message_id"] not in {r["message_id"] for r in svc.legacy_uncertain_receipts()}


def test_s4_r2_02_a_reply_to_a_later_message_does_not_hide_a_lost_one(app, client, dsn):
    """10:00 message A is lost in the old queue; 10:02 message B; 10:03 the agent answers B. A must still be listed."""
    _, phone, _ = add_driver(dsn)
    d = Driver(client, phone)
    a = d.say("late by 45 min")
    b = d.say("reaching at 3 pm now")
    app.state.service.record_reply(app.state.sessions.verify(b["session_token"]), "answer to B")
    backfill_as_0011(dsn, a["message_id"], b["message_id"])
    rows = {r["message_id"]: r for r in app.state.service.legacy_uncertain_receipts()}
    assert a["message_id"] in rows and rows[a["message_id"]]["later_agent_messages"] == 1
    assert rows[a["message_id"]]["next_driver_message_at"] is not None              # B came after A


def test_s4_r2_03_reopen_refuses_a_receipt_the_migration_did_not_backfill(app, client, dsn):
    _, phone, _ = add_driver(dsn)
    fresh = Driver(client, phone).say("late by 45 min")                              # new path, not processed yet
    assert app.state.service.reopen_receipts([fresh["message_id"]]) == 0             # a mistyped id changes nothing
    assert receipt(dsn, fresh["external_message_id"])[5] == 0                        # generation unchanged


# ---------------------------------------------------------------- R2.0 S4-R2-01
def test_s4_r2_01_a_worker_cannot_finish_after_its_lease_expired(app, client, dsn, monkeypatch):
    """The lease expires, nobody else claims the message, then the old worker tries to finish: refused."""
    _, phone, _ = add_driver(dsn)
    ev = Driver(client, phone).say("late by 45 min")
    svc = app.state.service
    monkeypatch.setattr(svc, "renew_turn", lambda *a: False)                       # heartbeat cannot renew
    orch = orchestrator(app, lease_s=3)              # long enough for the turn's writes even on a loaded machine
    real = orch._turn

    def slow(p, event):
        r = real(p, event)                                                           # writes while the lease is live
        time.sleep(3.3)                                                              # …then outlives it
        return r
    orch._turn = slow
    with pytest.raises(TurnLeaseLost):
        orch.handle(ev)
    assert receipt(dsn, ev["external_message_id"])[3] is False                      # not marked processed
    monkeypatch.undo()
    assert orchestrator(app).handle(ev) is not None                                  # redelivered, answered
    assert receipt(dsn, ev["external_message_id"])[3] is True
    assert replies_to(dsn, ev["message_id"]) == 1                                    # still one reply (S4-08)


# ---------------------------------------------------------------- S4-05 / R2.0 S4-R2-04, -05, -06 / R2.1
URL = "https://sqs.ap-south-1.amazonaws.com/000000000000/inbound.fifo"
DLQ_ARN = "arn:aws:sqs:ap-south-1:000000000000:inbound-dlq.fifo"
DLQ_URL = "https://sqs.ap-south-1.amazonaws.com/000000000000/inbound-dlq.fifo"


class FakeSqs:
    def __init__(self, dlq=None, **attrs):
        self.attrs = {"QueueArn": "arn:aws:sqs:ap-south-1:000000000000:inbound.fifo", "FifoQueue": "true",
                      "VisibilityTimeout": "300", "MessageRetentionPeriod": "345600",
                      "RedrivePolicy": json.dumps({"deadLetterTargetArn": DLQ_ARN, "maxReceiveCount": "5"})}
        self.attrs.update({k: v for k, v in attrs.items() if v is not None})
        for k in [k for k, v in attrs.items() if v is None]:
            self.attrs.pop(k)
        self.dlq = {"QueueArn": DLQ_ARN, "FifoQueue": "true", "MessageRetentionPeriod": "1209600", **(dlq or {})}

    def get_queue_url(self, QueueName, QueueOwnerAWSAccountId):
        assert QueueOwnerAWSAccountId == "000000000000"
        return {"QueueUrl": DLQ_URL.rsplit("/", 1)[0] + "/" + QueueName}

    def get_queue_attributes(self, QueueUrl, **kw):
        if QueueUrl == DLQ_URL:
            return {"Attributes": dict(self.dlq)}
        if QueueUrl == URL:
            return {"Attributes": dict(self.attrs)}
        raise KeyError(QueueUrl)

    def send_message(self, **kw):
        pass


def manifest(env="test-env", release=None, **gate_changes):
    """What the pipeline builds with each deployment and ships beside the artifact (R2.1 S4-R3-02)."""
    m = {"environment": env, "release": release or uuid.uuid4().hex[:8], "gates": {
        "RECOVERY_WORKER": {"artifact_sha256": "1" * 64, "schedule": "rate(1 minute)", "config_version": "3",
                            "queue_visibility_timeout_s": 300},
        "BOOKING_WORKFLOW": {"state_machine_arn": "arn:aws:states:ap-south-1:000000000000:stateMachine:booking",
                             "state_machine_version": "7", "definition_sha256": "2" * 64},
        "WORKLOAD_IDENTITY": {"iam_policies_sha256": "3" * 64,
                              "role_mapping": {"agent-runtime": "setuhaul_agent", "pipeline": "setuhaul_deploy_evidence"}},
        "OUTBOUND_DELIVERY": {"artifact_sha256": "4" * 64, "provider_config_sha256": "5" * 64,
                              "delivery_policy_version": "2"}}}
    for gate, change in gate_changes.items():
        m["gates"][gate].update(change)
    return m


def booking_app(dsn_, queue=None, env="test-env", man=None, dep=None):
    path = None
    if man is not None:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(man, f)
        path = f.name
    return create_app(Settings(dsn=dsn_, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks={"keys": []},
                               booking_enabled=True, environment=env, deployment_id=dep, deployment_manifest=path),
                      queue=queue)


def write_evidence(dsn_, env, dep, gate, fp="0" * 64, expires="7 days", verified_ago="0 min"):
    """As the deployment pipeline would, through the dedicated writer role."""
    with psycopg.connect(dsn_, autocommit=True) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE setuhaul_deploy_evidence")
            c.execute("""INSERT INTO setuhaul.deployment_evidence (environment, deployment_id, gate, verified_at, expires_at,
                                artifact_uri, evidence_sha256, config_fingerprint)
                         VALUES (%s,%s,%s, now() - %s::interval, now() - %s::interval + %s::interval,
                                 's3://evidence/run-1/report.json', %s, %s)""",
                      (env, dep, gate, verified_ago, verified_ago, expires, "a" * 64, fp))


def all_gates(dsn_, man, queue_fp, db_fp, **override):
    """Evidence for every gate of this manifest's deployment, with the fingerprints the pipeline computes."""
    fps = {"AWS_QUEUE": queue_fp, "AURORA": db_fp, **manifest_fingerprints(man)}
    for g in REQUIRED_GATES:
        write_evidence(dsn_, man["environment"], deployment_id_for(man), g, fp=override.get(g, fps[g]))


def app_db(dsn_):
    from setuhaul.api.db import Db
    return Db(dsn_)


def test_s4_05_the_booking_switch_fails_closed(dsn):
    man = manifest()
    with pytest.raises(BookingGateError, match="durable FIFO queue"):
        booking_app(dsn, man=man)                                                    # LocalQueue
    with pytest.raises(BookingGateError, match="SETUHAUL_DEPLOYMENT_MANIFEST"):
        booking_app(dsn, SqsFifoQueue(URL, FakeSqs()))                               # no manifest
    for bad, msg in (({"FifoQueue": "false"}, "not a FIFO queue"), ({"VisibilityTimeout": "120"}, "visibility timeout"),
                     ({"RedrivePolicy": None}, "dead-letter")):
        with pytest.raises(BookingGateError, match=msg):
            booking_app(dsn, SqsFifoQueue(URL, FakeSqs(**bad)), man=man)
    with pytest.raises(BookingGateError, match="WORKLOAD_IDENTITY: no evidence") as e:
        booking_app(dsn, SqsFifoQueue(URL, FakeSqs()), man=man)
    assert all(f"{g}: no evidence" in str(e.value) for g in REQUIRED_GATES) and len(REQUIRED_GATES) == 6
    q = SqsFifoQueue(URL, FakeSqs())
    all_gates(dsn, man, q.fingerprint(), database_fingerprint(app_db(dsn)))
    assert booking_app(dsn, q, man=man) is not None                                  # all six current: it starts
    assert booking_app(dsn, q, man=man, dep=deployment_id_for(man)) is not None      # an explicit id must agree
    with pytest.raises(BookingGateError, match="does not match the manifest"):
        booking_app(dsn, q, man=man, dep="dep-typed-by-hand")
    off = create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                              meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks={"keys": []}))
    assert off is not None                                                           # switch off: nothing needed


def test_s4_r2_04_evidence_must_be_for_this_deployment_fresh_and_for_this_configuration(dsn):
    q = SqsFifoQueue(URL, FakeSqs())
    qfp, dbfp = q.fingerprint(), database_fingerprint(app_db(dsn))
    old = manifest()
    all_gates(dsn, old, qfp, dbfp)
    with pytest.raises(BookingGateError, match="no evidence for this deployment"):
        booking_app(dsn, q, man=manifest())                                          # a new release: old proof void
    with pytest.raises(BookingGateError, match="no evidence for this deployment"):
        booking_app(dsn, q, env="other-env", man={**old, "environment": "other-env"})  # another environment
    with pytest.raises(BookingGateError, match="manifest is for environment"):
        booking_app(dsn, q, env="other-env", man=old)                                # a manifest built for test-env
    replaced = SqsFifoQueue(URL, FakeSqs(VisibilityTimeout="600"))                   # reconfigured after verification
    with pytest.raises(BookingGateError, match="AWS_QUEUE: evidence is for a different configuration"):
        booking_app(dsn, replaced, man=old)
    new_dlq = SqsFifoQueue(URL, FakeSqs(dlq={"MessageRetentionPeriod": "1209599"}))  # R2.1: the DLQ changed
    with pytest.raises(BookingGateError, match="AWS_QUEUE: evidence is for a different configuration"):
        booking_app(dsn, new_dlq, man=old)
    stale = manifest()
    all_gates(dsn, stale, qfp, dbfp)
    write_evidence(dsn, "test-env", deployment_id_for(stale), "AURORA", fp=dbfp, verified_ago="20 days", expires="10 days")
    with pytest.raises(BookingGateError, match="AURORA: evidence expired"):          # newest row: expired
        booking_app(dsn, q, man=stale)
    wrong_db = manifest()
    all_gates(dsn, wrong_db, qfp, dbfp, AURORA="b" * 64)
    with pytest.raises(BookingGateError, match="AURORA: evidence is for a different configuration"):
        booking_app(dsn, q, man=wrong_db)


def test_s4_r2_05_only_the_evidence_writer_can_add_and_nobody_can_change(dsn):
    dep = f"dep-{uuid.uuid4().hex[:8]}"
    write_evidence(dsn, "test-env", dep, "AURORA")
    with psycopg.connect(dsn, autocommit=True) as c:
        for role in ("setuhaul_agent", "setuhaul_console", "setuhaul_ingest", "setuhaul_feasibility", "setuhaul_system",
                     "setuhaul_rpc"):
            with pytest.raises(psycopg.Error) as e:
                with c.transaction():
                    c.execute(f"SET LOCAL ROLE {role}")
                    c.execute("""INSERT INTO setuhaul.deployment_evidence (environment, deployment_id, gate, expires_at,
                                        artifact_uri, evidence_sha256, config_fingerprint)
                                 VALUES ('test-env', %s, 'AWS_QUEUE', now() + interval '1 day', 's3://x/y', %s, %s)""",
                              (dep, "a" * 64, "0" * 64))
            assert e.value.sqlstate == "42501", role
        for sql in ("UPDATE setuhaul.deployment_evidence SET expires_at = now() + interval '20 days'",
                    "DELETE FROM setuhaul.deployment_evidence", "TRUNCATE setuhaul.deployment_evidence"):
            with pytest.raises(psycopg.Error, match="APPEND_ONLY"):                  # the owner too, by ordinary DML
                c.execute(sql)
        who = c.execute("SELECT verifier FROM setuhaul.deployment_evidence WHERE deployment_id=%s", (dep,)).fetchone()[0]
    assert who == "postgres"                                                         # the session user, not a claim
    with pytest.raises(psycopg.Error):
        write_evidence(dsn, "test-env", dep, "AURORA", expires="40 days")            # at most 30 days


def test_s4_r3_01_future_dated_evidence_is_refused(dsn):
    """R2.1: the reviewer's example. Verified "1 Jan 2027", expiring "31 Jan 2027", written in November 2026: 0013's
    check (expiry within 30 days of verification) accepted it. The insert trigger checks against the database clock."""
    dep = f"dep-{uuid.uuid4().hex[:8]}"
    for ahead in ("43 days", "1 hour", "6 minutes"):
        with pytest.raises(psycopg.Error, match="EVIDENCE_FUTURE_DATED"):
            write_evidence(dsn, "test-env", dep, "AURORA", verified_ago=f"-{ahead}", expires="29 days")
    with pytest.raises(psycopg.Error, match="EVIDENCE_TOO_LONG"):                    # no future verification needed
        write_evidence(dsn, "test-env", dep, "AURORA", expires="31 days")
    write_evidence(dsn, "test-env", dep, "AURORA", verified_ago="-2 minutes", expires="29 days")   # clock skew: ok
    write_evidence(dsn, "test-env", dep, "AURORA", expires="30 days")                # the ceiling itself: ok
    with psycopg.connect(dsn) as c:
        n = c.execute("SELECT count(*) FROM setuhaul.deployment_evidence WHERE deployment_id=%s", (dep,)).fetchone()[0]
    assert n == 2


def test_s4_r3_02_operational_evidence_expires_after_seven_days(dsn):
    dep = f"dep-{uuid.uuid4().hex[:8]}"
    for gate in ("RECOVERY_WORKER", "OUTBOUND_DELIVERY"):
        with pytest.raises(psycopg.Error, match="EVIDENCE_TOO_LONG"):
            write_evidence(dsn, "test-env", dep, gate, expires="8 days")
        write_evidence(dsn, "test-env", dep, gate, expires="7 days")
    for gate in ("AWS_QUEUE", "AURORA", "BOOKING_WORKFLOW", "WORKLOAD_IDENTITY"):
        write_evidence(dsn, "test-env", dep, gate, expires="30 days")


def test_s4_r3_02_every_gate_is_bound_to_the_deployment_manifest(dsn):
    """R2.1: a change to the worker artifact, the state machine, an IAM policy or the sender's configuration is a new
    manifest, so a new deployment id; evidence for the old deployment no longer counts. And a gate's evidence must
    carry its own manifest section's fingerprint."""
    q = SqsFifoQueue(URL, FakeSqs())
    qfp, dbfp = q.fingerprint(), database_fingerprint(app_db(dsn))
    base = manifest(release="r1")
    all_gates(dsn, base, qfp, dbfp)
    assert booking_app(dsn, q, man=base) is not None
    for gate, change in (("RECOVERY_WORKER", {"artifact_sha256": "6" * 64}),
                         ("BOOKING_WORKFLOW", {"state_machine_version": "8"}),
                         ("WORKLOAD_IDENTITY", {"iam_policies_sha256": "7" * 64}),
                         ("OUTBOUND_DELIVERY", {"provider_config_sha256": "8" * 64})):
        drifted = manifest(release="r1", **{gate: change})
        assert deployment_id_for(drifted) != deployment_id_for(base)
        with pytest.raises(BookingGateError, match=f"{gate}: no evidence for this deployment"):
            booking_app(dsn, q, man=drifted)
    other = manifest()
    v8 = manifest(BOOKING_WORKFLOW={"state_machine_version": "8"})
    all_gates(dsn, other, qfp, dbfp, BOOKING_WORKFLOW=manifest_fingerprints(v8)["BOOKING_WORKFLOW"])
    with pytest.raises(BookingGateError, match="BOOKING_WORKFLOW: evidence is for a different configuration"):
        booking_app(dsn, q, man=other)                                               # evidence for another definition
    incomplete = manifest(BOOKING_WORKFLOW={"definition_sha256": "not-a-hash"}, WORKLOAD_IDENTITY={"role_mapping": {}})
    with pytest.raises(BookingGateError, match="definition_sha256 is not a SHA-256.*role_mapping is missing"):
        booking_app(dsn, q, man=incomplete)


def test_r2_1_the_dead_letter_queue_itself_is_verified():
    for kw, msg in (({"RedrivePolicy": json.dumps({"deadLetterTargetArn": DLQ_ARN, "maxReceiveCount": "50"})},
                     "maxReceiveCount 50"),
                    ({"RedrivePolicy": json.dumps({"deadLetterTargetArn": "arn:dlq", "maxReceiveCount": "5"})},
                     "not an SQS queue ARN"),
                    ({"dlq": {"FifoQueue": "false"}}, "dead-letter queue is not FIFO"),
                    ({"dlq": {"MessageRetentionPeriod": "86400"}}, "keeps messages for less time"),
                    ({"RedrivePolicy": json.dumps({"deadLetterTargetArn": DLQ_ARN.replace("000000000000", "111111111111"),
                                                   "maxReceiveCount": "5"})}, "not readable")):
        with pytest.raises(BookingGateError, match=msg):
            SqsFifoQueue(URL, FakeSqs(**kw)).verify(120)
    SqsFifoQueue(URL, FakeSqs()).verify(120)


def test_r2_1_a_disabled_evidence_trigger_blocks_the_switch(dsn):
    """R2.1 §3: the triggers stop ordinary DML; an owner could disable them. The service refuses to start then."""
    q = SqsFifoQueue(URL, FakeSqs())
    man = manifest()
    all_gates(dsn, man, q.fingerprint(), database_fingerprint(app_db(dsn)))
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("ALTER TABLE setuhaul.deployment_evidence DISABLE TRIGGER deployment_evidence_append_only")
        try:
            with pytest.raises(BookingGateError, match="triggers are missing or disabled"):
                booking_app(dsn, q, man=man)
        finally:
            c.execute("ALTER TABLE setuhaul.deployment_evidence ENABLE TRIGGER deployment_evidence_append_only")
    assert booking_app(dsn, q, man=man) is not None
