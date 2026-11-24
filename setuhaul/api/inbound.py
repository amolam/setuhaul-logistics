"""Durable inbound delivery (B-WF-1, review findings B2-14 / B2-15; migration 0011).

    webhook ──ingest()──▶ chat_messages + inbound_receipts   (one transaction: the receipt row is the outbox)
            ──InboundRelay.publish()──▶ queue                 (deduplication id = WhatsApp id #generation, group = driver)
            ──mark_published()                                (only after the queue accepted it)
    worker  ──Orchestrator.handle()── claim_turn() lease ─ turn ─ finish_turn()
    sweep   ──InboundRelay.recover()── re-publishes stored-but-unpublished and stalled messages

Delivery is at least once; the processing lease in the database makes it one turn per message. The queue's own
deduplication (SQS FIFO: 5 minutes) is only the first filter: a duplicate that arrives later is still refused,
because the receipt says the message was processed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Protocol

log = logging.getLogger("setuhaul.inbound")


class Queue(Protocol):
    def send(self, body: dict, dedup_id: str, group_id: str) -> bool: ...


class LocalQueue:
    """In-process stand-in for an SQS FIFO queue: messages in order, deduplication id remembered for 5 minutes.
    Iterating it yields every accepted event in order (what tests and the replay read); receive() consumes."""

    DEDUP_WINDOW_S = 300
    durable = False                                           # in-memory: gone with the process

    def __init__(self, clock: Callable[[], float] = time.monotonic, maxlen: int = 10_000):
        self.clock = clock
        self._lock = threading.Lock()
        self._sent: deque = deque(maxlen=maxlen)
        self._pending: deque = deque(maxlen=maxlen)
        self._seen: dict[str, float] = {}

    def send(self, body: dict, dedup_id: str, group_id: str) -> bool:
        with self._lock:
            now = self.clock()
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.DEDUP_WINDOW_S}
            if dedup_id in self._seen:
                return False                                  # what SQS does inside its deduplication window
            self._seen[dedup_id] = now
            self._sent.append(body)
            self._pending.append(body)
            return True

    def receive(self) -> dict | None:
        with self._lock:
            return self._pending.popleft() if self._pending else None

    def __iter__(self):
        with self._lock:
            return iter(list(self._sent))

    def __reversed__(self):
        with self._lock:
            return reversed(list(self._sent))

    def __len__(self) -> int:
        return len(self._sent)


class SqsFifoQueue:
    """SQS FIFO (AWS). Message group = driver, so one driver's messages are handled in order and different drivers in
    parallel. The deduplication id is hashed: SQS allows 128 characters from a fixed set, WhatsApp ids are longer."""

    durable = True

    def __init__(self, queue_url: str, client=None):
        if client is None:
            import boto3                                       # only in AWS; tests pass a stub
            client = boto3.client("sqs")
        self.url, self.sqs = queue_url, client

    def send(self, body: dict, dedup_id: str, group_id: str) -> bool:
        self.sqs.send_message(QueueUrl=self.url, MessageBody=json.dumps(body, default=str), MessageGroupId=group_id,
                              MessageDeduplicationId=hashlib.sha256(dedup_id.encode()).hexdigest())
        return True


    def _attrs(self, url: str) -> dict:
        return self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]

    def _dead_letter(self, a: dict) -> tuple[dict, dict | None, list[str]]:
        """R2.1 §4: the redrive policy itself and the dead-letter queue it points at, not only that a policy exists."""
        try:
            policy = json.loads(a["RedrivePolicy"])
        except (KeyError, TypeError, ValueError):
            return {}, None, ["no dead-letter queue (RedrivePolicy)"]
        problems, arn = [], str(policy.get("deadLetterTargetArn", ""))
        try:
            receives = int(policy.get("maxReceiveCount", 0))
        except (TypeError, ValueError):
            receives = 0
        if not 1 <= receives <= 10:
            problems.append(f"maxReceiveCount {policy.get('maxReceiveCount')} is outside 1-10")
        parts = arn.split(":")
        if len(parts) != 6 or parts[2] != "sqs":
            return policy, None, problems + [f"dead-letter target {arn!r} is not an SQS queue ARN"]
        try:
            url = self.sqs.get_queue_url(QueueName=parts[5], QueueOwnerAWSAccountId=parts[4])["QueueUrl"]
            dlq = self._attrs(url)
        except Exception as e:                                 # noqa: BLE001 — reported, the service refuses to start
            return policy, None, problems + [f"dead-letter queue not readable ({type(e).__name__})"]
        if dlq.get("FifoQueue") != "true":
            problems.append("dead-letter queue is not FIFO")
        if int(dlq.get("MessageRetentionPeriod", 0)) < int(a.get("MessageRetentionPeriod", 0)):
            problems.append("dead-letter queue keeps messages for less time than the queue")
        return policy, dlq, problems

    def verify(self, lease_s: int) -> None:
        """S4-05: read the queue's own attributes; refuse anything that cannot carry the processing contract."""
        a = self._attrs(self.url)
        problems = []
        if a.get("FifoQueue") != "true":
            problems.append("not a FIFO queue")
        if int(a.get("VisibilityTimeout", 0)) < 2 * lease_s:
            problems.append(f"visibility timeout {a.get('VisibilityTimeout')} s is below 2 x the {lease_s} s lease")
        problems += self._dead_letter(a)[2]
        if problems:
            raise BookingGateError("queue " + self.url.rsplit("/", 1)[-1] + ": " + "; ".join(problems))

    def fingerprint(self) -> str:
        """R2.0: what the AWS_QUEUE evidence must have verified. Replacing or reconfiguring the queue, or its
        dead-letter queue (R2.1), changes it."""
        a = self._attrs(self.url)
        _, dlq, _ = self._dead_letter(a)
        return fingerprint({"url": self.url, **{k: a.get(k) for k in FINGERPRINT_ATTRS},
                            "dead_letter": {k: (dlq or {}).get(k) for k in DLQ_FINGERPRINT_ATTRS}})


class BookingGateError(RuntimeError):
    """The booking switch is on but a precondition is not met: the service refuses to start."""


# R2.0 S4-R2-06: six gates; what each covers (and its monitoring) is in docs/operations/DEPLOYMENT_GATES.md.
REQUIRED_GATES = ("AWS_QUEUE", "AURORA", "RECOVERY_WORKER", "BOOKING_WORKFLOW", "WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY")
FINGERPRINT_ATTRS = ("QueueArn", "FifoQueue", "VisibilityTimeout", "RedrivePolicy", "MessageRetentionPeriod")
DLQ_FINGERPRINT_ATTRS = ("QueueArn", "FifoQueue", "MessageRetentionPeriod")

# R2.1 S4-R3-02: the four gates the service cannot inspect itself are bound to an immutable deployment manifest that
# ships with the artifact. Each gate's evidence must carry the fingerprint of its manifest section, and the deployment
# id is derived from the whole manifest, so any change to a worker artifact, the state machine, an IAM policy or the
# sender's configuration is a new deployment id: evidence for the old one no longer counts. See docs/operations/DEPLOYMENT_GATES.md.
MANIFEST_GATES = {
    "RECOVERY_WORKER": ("artifact_sha256", "schedule", "config_version", "queue_visibility_timeout_s"),
    "BOOKING_WORKFLOW": ("state_machine_arn", "state_machine_version", "definition_sha256"),
    "WORKLOAD_IDENTITY": ("iam_policies_sha256", "role_mapping"),
    "OUTBOUND_DELIVERY": ("artifact_sha256", "provider_config_sha256", "delivery_policy_version"),
}


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# Owner-approved pilot exceptions (0026; OWNER-APPROVAL-20261123-OPTION-D). The database lists the same three pairs.
EXCEPTION_CODES = {"AURORA": "T33-P", "WORKLOAD_IDENTITY": "NO-LIVE-DRIFT-DETECTION",
                   "OUTBOUND_DELIVERY": "META-DEFERRED-TEST-SINK-ONLY"}
GATE_READ_ROLES = ("system", "ingest", "agent")        # 0026: each may read the gate tables; none may write them


def gate_role(db) -> str:
    """The database login this workload reads the gate tables with: the api has ingest, the agent has agent."""
    have = getattr(db, "secret_arns", None)
    if have is None:                                    # psycopg (local, tests): every role is available
        return "system"
    for r in GATE_READ_ROLES:
        if r in have:
            return r
    raise BookingGateError("this workload has no database login that may read the deployment gates")


def gate_snapshot(db, environment: str | None = None, deployment_id: str | None = None) -> dict:
    """0026: everything the start-up check reads, through one read-only definer function (no table grants)."""
    with db.tx(gate_role(db), readonly=True) as c:
        raw = c.execute("SELECT booking_gate_snapshot(%s, %s)", (environment, deployment_id)).fetchone()[0]
    return json.loads(raw) if isinstance(raw, str) else raw


def snapshot_fingerprint(snap: dict) -> str:
    return fingerprint({"database": snap["database"], "server_version_num": snap["server_version_num"],
                        "migrations": [list(m) for m in snap["migrations"]]})


def database_fingerprint(db) -> str:
    """What the AURORA evidence must have verified: this database, its server version and the exact migrations."""
    return snapshot_fingerprint(gate_snapshot(db))


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def manifest_problems(manifest: dict, environment: str) -> list[str]:
    problems = []
    if manifest.get("environment") != environment:
        problems.append(f"manifest is for environment {manifest.get('environment')!r}, not {environment!r}")
    gates = manifest.get("gates") if isinstance(manifest.get("gates"), dict) else {}
    for gate, keys in MANIFEST_GATES.items():
        section = gates.get(gate)
        if not isinstance(section, dict):
            problems.append(f"manifest has no {gate} section")
            continue
        for k in keys:
            v = section.get(k)
            if v in (None, "", {}, []):
                problems.append(f"manifest {gate}.{k} is missing")
            elif k.endswith("sha256") and not (isinstance(v, str) and len(v) == 64
                                               and all(ch in "0123456789abcdef" for ch in v)):
                problems.append(f"manifest {gate}.{k} is not a SHA-256")
    return problems


def deployment_id_for(manifest: dict) -> str:
    """Content-derived: the same manifest always gives the same id; any change gives a new one."""
    return "m-" + fingerprint(manifest)[:40]


def manifest_fingerprints(manifest: dict) -> dict[str, str]:
    return {g: fingerprint({"gate": g, **manifest["gates"][g]}) for g in MANIFEST_GATES}


GUARD_TRIGGERS = {("deployment_evidence", "deployment_evidence_append_only"),
                  ("deployment_evidence", "deployment_evidence_fresh"),
                  ("deployment_manifests", "deployment_manifests_append_only"),
                  ("deployment_exceptions", "deployment_exceptions_append_only"),
                  ("deployment_exceptions", "deployment_exceptions_clock"),
                  ("dock_occupancy", "aa_booking_kill_switch"),
                  ("appointments", "aa_booking_kill_switch"),
                  # 0030: the per-commitment evidence guard; 0031 (M1): no evidence is recorded while it is off
                  ("dock_occupancy", "ab_booking_evidence_guard"),
                  ("appointments", "ab_booking_evidence_guard"),
                  ("deployment_manifests", "aa_evidence_requires_guard"),
                  ("deployment_evidence", "aa_evidence_requires_guard"),
                  ("deployment_exceptions", "aa_evidence_requires_guard")}


def evidence_triggers_enabled(db) -> bool:
    """R2.1 §3: the append-only and freshness triggers stop ordinary DML only while they are enabled. An owner can
    disable them; the service refuses to start if any is missing or disabled. 0026 adds the manifest and exception
    tables' guards and the kill switch's triggers: a disabled kill-switch trigger would make the switch a dead letter."""
    return guards_enabled(gate_snapshot(db))


def guards_enabled(snap: dict) -> bool:
    return GUARD_TRIGGERS <= {(r[0], r[1]) for r in snap["guards"]}


def booking_killed(db) -> bool:
    """The kill switch (0026). Read for /health; the database itself refuses commitments while it is engaged."""
    return bool(gate_snapshot(db)["kill_switch"])


def own_code_sha256(lambda_client=None, function_name: str | None = None) -> str:
    """The SHA-256 (hex) of this Lambda function's deployed code, as AWS reports it (CodeSha256 is base64)."""
    import base64
    import os
    if lambda_client is None:
        import boto3
        lambda_client = boto3.client("lambda")
    cfg = lambda_client.get_function_configuration(FunctionName=function_name or os.environ["AWS_LAMBDA_FUNCTION_NAME"])
    return base64.b64decode(cfg["CodeSha256"]).hex()


def manifest_from_database(db, environment: str, code_sha256: str) -> dict:
    """0026: the newest manifest the pipeline recorded for this environment, accepted only if it is for the code that
    is running here and its id is the one its content gives. A deployment whose manifest was not recorded therefore
    finds the previous one, which names other code, and the switch stays closed."""
    row = gate_snapshot(db, environment)["manifest"]
    if not row:
        raise BookingGateError(f"no deployment manifest recorded for {environment}")
    dep, art, manifest = row["deployment_id"], row["artifact_sha256"], row["manifest"]
    if deployment_id_for(manifest) != dep:
        raise BookingGateError("the recorded manifest does not match its deployment id")
    if art != code_sha256:
        raise BookingGateError("the newest recorded manifest is for other code than this function's "
                               f"(manifest {art[:12]}..., running {code_sha256[:12]}...)")
    return manifest


def check_booking_preconditions(db, queue, lease_s: int, environment: str | None, manifest: dict | None,
                                deployment_id: str | None = None, provisional_out: dict | None = None) -> dict[str, str]:
    """S4-05 / R2.0 S4-R2-04 / R2.1: with the booking switch on, start only if the queue is durable and correctly
    configured, and every gate has a current evidence row for THIS environment and deployment, within its validity
    window. The deployment id comes from the deployment manifest. The queue and database fingerprints are computed
    live; the other four come from the manifest's sections."""
    if not getattr(queue, "durable", False):
        raise BookingGateError("SETUHAUL_BOOKING_ENABLED needs a durable FIFO queue (SETUHAUL_QUEUE_URL); "
                               "the in-process queue loses messages on a restart")
    if not environment or manifest is None:
        raise BookingGateError("SETUHAUL_ENVIRONMENT and SETUHAUL_DEPLOYMENT_MANIFEST are required with the booking "
                               "switch on")
    if problems := manifest_problems(manifest, environment):
        raise BookingGateError("deployment manifest: " + "; ".join(problems))
    derived = deployment_id_for(manifest)
    if deployment_id and deployment_id != derived:
        raise BookingGateError(f"SETUHAUL_DEPLOYMENT_ID {deployment_id} does not match the manifest ({derived})")
    deployment_id = derived
    queue.verify(lease_s)
    snap = gate_snapshot(db, environment, deployment_id)          # one read-only call (0026)
    if not guards_enabled(snap):
        raise BookingGateError("deployment_evidence triggers are missing or disabled")
    live = {"AWS_QUEUE": queue.fingerprint(), "AURORA": snapshot_fingerprint(snap), **manifest_fingerprints(manifest)}
    rows, exc = [tuple(r) for r in snap["evidence"]], [tuple(r) for r in snap["exceptions"]]
    server_version = int(snap["server_version_num"])
    latest = {g: (fresh, fp) for g, fresh, fp in rows}
    excepted = {g: (code, fresh, fp, ref) for g, code, fresh, fp, ref in exc}
    problems, by_exception = [], {}
    for g in REQUIRED_GATES:
        if g in latest and latest[g][0] and latest[g][1] == live[g]:
            continue                                                       # current evidence: the gate passed
        e = excepted.get(g)
        why = exception_problem(g, e, live[g], environment, server_version) if e else "none"
        if why is None:
            by_exception[g] = f"{e[0]} ({e[3]})"
            continue
        if g == "BOOKING_WORKFLOW" and (window := provisional_window(db, environment, deployment_id, manifest)):
            # 0032 (Option P): an OPEN provisional authorization stands in for BOOKING_WORKFLOW only, until its expiry;
            # the database re-checks it on every commitment. Not an owner-approved exception: reported separately.
            if provisional_out is not None:
                provisional_out.update(window)
            continue
        if g not in latest:
            problems.append(f"{g}: no evidence for this deployment"
                            + (f" and exception {e[0]} does not hold ({why})" if e else ""))
        elif not latest[g][0]:
            problems.append(f"{g}: evidence expired or outside its validity window")
        else:
            problems.append(f"{g}: evidence is for a different configuration")
    if problems:
        raise BookingGateError("deployment gates not satisfied: " + "; ".join(problems))
    if by_exception:
        log.warning("booking switch ON with owner-approved pilot exceptions: %s", by_exception)
    if provisional_out:
        log.warning("booking switch ON under provisional authorization %s (BOOKING_WORKFLOW only) until %s",
                    provisional_out.get("authorization_id"), provisional_out.get("expires_at"))
    return by_exception


def provisional_window(db, environment: str, deployment_id: str, manifest: dict) -> dict | None:
    """0032: the open provisional authorization of this deployment, or None. Open = ISSUED and inside its window by the
    database clock, for this deployment's code, with deployment identity (M2) enforced. Any read failure (for example
    a database without 0032) is None: fail closed."""
    try:
        with db.tx(gate_role(db), readonly=True) as c:
            raw = c.execute("SELECT booking_provisional_snapshot(%s, %s)", (environment, deployment_id)).fetchone()[0]
    except Exception:                                               # noqa: BLE001 — fail closed
        return None
    snap = json.loads(raw) if isinstance(raw, str) else raw
    art = ((manifest.get("gates") or {}).get("RECOVERY_WORKER") or {}).get("artifact_sha256")
    if not snap or not snap.get("open") or not snap.get("identity_enforced") or snap.get("code_sha256") != art:
        return None
    return {"authorization_id": snap["authorization_id"], "expires_at": snap["expires_at"],
            "approval_ref": snap["approval_ref"], "release_sha": snap["release_sha"]}


def exception_problem(gate: str, e: tuple, live_fp: str, environment: str, server_version: int) -> str | None:
    """Why an owner-approved exception does not stand in for this gate here, or None. The database already limits the
    gate/code pairs, the environment and the duration; these rules hold even if a row got past it."""
    code, fresh, fp, _ref = e
    if EXCEPTION_CODES.get(gate) != code:
        return "not an exception the owner accepted for this gate"
    if "prod" in environment or not environment.startswith("pilot"):
        return "exceptions apply only to the pilot, never to production"
    if not fresh:
        return "expired"
    if fp != live_fp:
        return "recorded for a different configuration"
    if gate == "AURORA" and not 170009 <= server_version < 180000:
        return f"T33-P covers PostgreSQL 17.9 on the pilot only; this server is {server_version}"
    return None


def dedup_id(event: dict) -> str:
    return f'{event["external_message_id"]}#{event.get("generation", 0)}'


class InboundRelay:
    """Publishes claimed receipts to the queue, then marks them published. Never the other way round: a crash after
    sending and before marking only means one more delivery, which the processing lease absorbs."""

    def __init__(self, service, queue: Queue, sessions):
        self.svc, self.queue, self.sessions = service, queue, sessions

    def publish(self, event: dict) -> bool:
        sent = self.queue.send(event, dedup_id(event), event["driver_id"])
        self.svc.mark_published(event["driver_id"], event["message_id"])   # dropped as a duplicate = already queued
        return sent

    def recover(self, unpublished_after_s: int = 30, stalled_after_s: int = 360) -> int:
        n = 0
        for ev in self.svc.recover_inbound(self.sessions, unpublished_after_s, stalled_after_s):
            try:
                n += self.publish(ev)
            except Exception:                                  # noqa: BLE001 — the next sweep tries again
                log.exception("could not re-publish inbound message %s", ev["message_id"])
        return n
