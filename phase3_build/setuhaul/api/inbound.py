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


# R2.0 S4-R2-06: six gates; what each covers (and its monitoring) is in DEPLOYMENT_GATES.md.
REQUIRED_GATES = ("AWS_QUEUE", "AURORA", "RECOVERY_WORKER", "BOOKING_WORKFLOW", "WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY")
FINGERPRINT_ATTRS = ("QueueArn", "FifoQueue", "VisibilityTimeout", "RedrivePolicy", "MessageRetentionPeriod")
DLQ_FINGERPRINT_ATTRS = ("QueueArn", "FifoQueue", "MessageRetentionPeriod")

# R2.1 S4-R3-02: the four gates the service cannot inspect itself are bound to an immutable deployment manifest that
# ships with the artifact. Each gate's evidence must carry the fingerprint of its manifest section, and the deployment
# id is derived from the whole manifest, so any change to a worker artifact, the state machine, an IAM policy or the
# sender's configuration is a new deployment id: evidence for the old one no longer counts. See DEPLOYMENT_GATES.md.
MANIFEST_GATES = {
    "RECOVERY_WORKER": ("artifact_sha256", "schedule", "config_version", "queue_visibility_timeout_s"),
    "BOOKING_WORKFLOW": ("state_machine_arn", "state_machine_version", "definition_sha256"),
    "WORKLOAD_IDENTITY": ("iam_policies_sha256", "role_mapping"),
    "OUTBOUND_DELIVERY": ("artifact_sha256", "provider_config_sha256", "delivery_policy_version"),
}


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def database_fingerprint(db) -> str:
    """What the AURORA evidence must have verified: this database, its server version and the exact migrations."""
    with db.tx("system", readonly=True) as c:
        name, ver = c.execute("SELECT current_database(), current_setting('server_version_num')").fetchone()
        migs = c.execute("SELECT version, sha256 FROM setuhaul_meta.schema_migrations ORDER BY version").fetchall()
    return fingerprint({"database": name, "server_version_num": ver, "migrations": migs})


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


def evidence_triggers_enabled(db) -> bool:
    """R2.1 §3: the append-only and freshness triggers stop ordinary DML only while they are enabled. An owner can
    disable them; the service refuses to start if either is missing or disabled."""
    with db.tx("system", readonly=True) as c:
        n = c.execute("""SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid
                           JOIN pg_namespace s ON s.oid = r.relnamespace
                          WHERE s.nspname = 'setuhaul' AND r.relname = 'deployment_evidence' AND t.tgenabled IN ('O','A')
                            AND t.tgname IN ('deployment_evidence_append_only', 'deployment_evidence_fresh')""").fetchone()[0]
    return n == 2


def check_booking_preconditions(db, queue, lease_s: int, environment: str | None, manifest: dict | None,
                                deployment_id: str | None = None) -> None:
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
    if not evidence_triggers_enabled(db):
        raise BookingGateError("deployment_evidence triggers are missing or disabled")
    live = {"AWS_QUEUE": queue.fingerprint(), "AURORA": database_fingerprint(db), **manifest_fingerprints(manifest)}
    with db.tx("system", readonly=True) as c:
        # the same freshness rules as the insert trigger (0014), so rows written before it get no more
        rows = c.execute("""SELECT DISTINCT ON (gate) gate,
                                   expires_at > now() AND verified_at <= now() + interval '5 minutes'
                                     AND expires_at <= now() + setuhaul.evidence_max_validity(gate)
                                     AND expires_at <= verified_at + setuhaul.evidence_max_validity(gate),
                                   config_fingerprint
                              FROM deployment_evidence WHERE environment=%s AND deployment_id=%s
                             ORDER BY gate, evidence_id DESC""", (environment, deployment_id)).fetchall()
    latest = {g: (fresh, fp) for g, fresh, fp in rows}
    problems = []
    for g in REQUIRED_GATES:
        if g not in latest:
            problems.append(f"{g}: no evidence for this deployment")
        elif not latest[g][0]:
            problems.append(f"{g}: evidence expired or outside its validity window")
        elif latest[g][1] != live[g]:
            problems.append(f"{g}: evidence is for a different configuration")
    if problems:
        raise BookingGateError("deployment gates not satisfied: " + "; ".join(problems))


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
