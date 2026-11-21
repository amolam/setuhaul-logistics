"""E1, E2, E4: Lambda entry points and the Step Functions approval workflow, run locally.

AWS is never called: Step Functions and SQS are stubs, the database is reached through the Data API backend and the
local emulator, and the model is the rules reader. Each workload runs with database logins for **only** the roles in
`WORKLOAD_ROLES`; a handler that needed another role would fail with PermissionError, so these tests are also the
evidence for the least-privilege table in docs/release/04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md §4.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from data_api_emulator import ALL_ROLES, EmulatedRdsData
from setuhaul.aws import lambdas
from setuhaul.aws.data_api import DataApiDb
from setuhaul.aws.stepfunctions import NotDueYet, StepFunctionsApprovalWorkflow
from setuhaul.workflow.approvals import ApprovalOutboxRelay
from test_api import CLIENT, ISSUER, META_SECRET, SESSION_SECRET, add_driver
from test_approvals import approval, pending
from test_db_requests import decide, short_deadline

ARN = "arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul"
# The database roles each Lambda is given a login for (one Secrets Manager secret per role).
WORKLOAD_ROLES = {
    "api": ("ingest", "console", "agent", "feasibility"),  # agent + feasibility only for the /tools route
    "agent": ("agent", "feasibility", "ingest"),          # no system role: the turn never needs it
    "recovery": ("system", "ingest"),
    "outbound": ("system",),
    "approval": ("system",),
}


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class StubSfn:
    def __init__(self):
        self.started, self.answered = {}, []

    def start_execution(self, stateMachineArn, name, input):
        if name in self.started:
            raise AwsError("ExecutionAlreadyExists")
        self.started[name] = json.loads(input)

    def send_task_success(self, taskToken, output):
        if taskToken in self.answered:
            raise AwsError("TaskTimedOut")                       # a finished task
        self.answered.append(taskToken)


class StubSqs:
    def __init__(self):
        self.extended = []

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.extended.append((ReceiptHandle, VisibilityTimeout))


def data_api(dsn, workload, em=None):
    em = em or EmulatedRdsData(dsn)
    return DataApiDb(ARN, "setuhaul", {r: ALL_ROLES[r] for r in WORKLOAD_ROLES[workload]}, em)


# ================================================================ E4: Step Functions approval workflow
@pytest.fixture
def wf(dsn):
    return StepFunctionsApprovalWorkflow(data_api(dsn, "approval"), "arn:aws:states:ap-southeast-2:0:stateMachine:approval:1",
                                         StubSfn())


def test_start_is_named_after_the_approval_and_idempotent(conn, wf):
    _, r = pending(conn)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert wf.start(r["approval_id"], deadline) is True
    assert wf.start(r["approval_id"], deadline) is False              # ExecutionAlreadyExists: the same execution
    inp = wf.sfn.started[str(r["approval_id"])]
    assert inp["approval_id"] == str(r["approval_id"]) and 590 <= inp["timeout_s"] <= 600


def test_the_outbox_relay_starts_one_execution_however_often_it_runs(conn, wf):
    _, r = pending(conn)
    relay = ApprovalOutboxRelay(wf.db, wf)
    relay.run_once()
    relay.run_once()
    assert list(wf.sfn.started).count(str(r["approval_id"])) == 1


def test_a_decision_answers_the_stored_task_token_once(conn, wf):
    _, r = pending(conn)
    assert wf.record_token(r["approval_id"], "tok-A") is True
    assert approval(conn, r["appointment_id"])["task_token"] == "tok-A"
    decide(conn, r["appointment_id"], "CONFIRM")
    assert wf.callback(r["approval_id"]) is True and wf.sfn.answered == ["tok-A"]
    assert wf.callback(r["approval_id"]) is False                     # finished task: treated as done


def test_a_token_that_arrives_after_the_decision_is_answered_at_once(conn, wf):
    _, r = pending(conn)
    decide(conn, r["appointment_id"], "REJECT")
    assert wf.record_token(r["approval_id"], "tok-late") is False      # not stored: the request is closed
    assert wf.sfn.answered == ["tok-late"]


def test_no_token_yet_means_no_callback(conn, wf):
    _, r = pending(conn)
    assert wf.callback(r["approval_id"]) is False and wf.sfn.answered == []


def test_the_reconciler_resends_a_lost_callback(conn, wf):
    _, r = pending(conn)
    wf.record_token(r["approval_id"], "tok-lost")
    decide(conn, r["appointment_id"], "CONFIRM")                       # the callback never happened
    assert wf.reconcile() >= 1 and "tok-lost" in wf.sfn.answered


def test_expiry_runs_in_postgres_and_retries_when_not_yet_due(conn, wf):
    _, r = pending(conn)
    with pytest.raises(NotDueYet):
        wf.expire(r["approval_id"])                                    # Step Functions Retry tries again
    _, r2 = short_deadline(conn, seconds=2)
    time.sleep(2.3)
    assert wf.expire(r2["approval_id"]) == "EXPIRED"
    assert approval(conn, r2["appointment_id"])["status"] == "EXPIRED"


# ================================================================ E1 / E2: Lambda entry points
@pytest.fixture
def lambda_env(monkeypatch, dsn):
    """The environment the infrastructure gives every Lambda, with the database on the Data API backend."""
    for k, v in {"SETUHAUL_ENV": "ci", "SETUHAUL_DSN": "rds-data", "SETUHAUL_DB_CLUSTER_ARN": ARN,
                 "COGNITO_ISSUER": ISSUER, "COGNITO_CLIENT_IDS": CLIENT, "SESSION_SECRET": SESSION_SECRET.decode(),
                 "META_APP_SECRET": META_SECRET.decode(), "SETUHAUL_BOOKING_ENABLED": "false",
                 "SETUHAUL_QUEUE_URL": "", "FACILITIES": "FAC-JAI-01,FAC-GGN-01"}.items():
        monkeypatch.setenv(k, v)
    em = EmulatedRdsData(dsn)

    def use(workload):
        monkeypatch.setenv("SETUHAUL_DB_SECRET_ARNS", json.dumps({r: ALL_ROLES[r] for r in WORKLOAD_ROLES[workload]}))
        monkeypatch.setattr(DataApiDb, "from_env", classmethod(lambda cls, env=None, client=None: cls(
            ARN, "setuhaul", json.loads(__import__("os").environ["SETUHAUL_DB_SECRET_ARNS"]), em)))
        _clear()
    yield use
    _clear()


def _clear():
    for f in (lambdas.runtime, lambdas._asgi, lambdas._orchestrator, lambdas._workflow):
        getattr(f, "cache_clear", lambda: None)()                     # a test may have replaced one


def http_event(method, path, body=b"", headers=None):
    return {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "",
            "headers": {"content-type": "application/json", **(headers or {})},
            "requestContext": {"http": {"method": method, "path": path, "sourceIp": "203.0.113.1", "protocol": "HTTP/1.1",
                                        "userAgent": "test"}, "stage": "$default", "requestId": "r1", "accountId": "0",
                               "apiId": "api", "domainName": "example.test", "timeEpoch": 0},
            "isBase64Encoded": False, "body": body.decode() if body else None}


def test_the_api_lambda_serves_health_with_the_booking_switch_off(lambda_env):
    lambda_env("api")
    r = lambdas.api_handler(http_event("GET", "/health"), None)
    assert r["statusCode"] == 200 and json.loads(r["body"])["booking_enabled"] is False


def test_a_driver_message_through_the_api_lambda_and_the_agent_lambda(lambda_env, dsn, monkeypatch):
    """Webhook (api workload) → queue → agent turn (agent workload), each with only its own database logins."""
    lambda_env("api")
    _, phone, _ = add_driver(dsn)
    mid = f"wamid.{uuid.uuid4().hex}"
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": phone.lstrip("+"), "id": mid, "type": "text", "text": {"body": "Traffic, reaching 2 hours late"}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(META_SECRET, body, hashlib.sha256).hexdigest()
    r = lambdas.api_handler(http_event("POST", "/webhooks/whatsapp", body, {"x-hub-signature-256": sig}), None)
    assert r["statusCode"] == 200 and json.loads(r["body"]) == {"status": "received"}
    queued = lambdas.runtime().state.inbound.receive()
    assert queued["external_message_id"] == mid

    lambda_env("agent")
    monkeypatch.setattr("setuhaul.agent.models.model_for", lambda env, db=None, **kw: None)   # rules reader: no model call
    sqs = StubSqs()
    out = lambdas.agent_handler({"Records": [{"messageId": "m1", "receiptHandle": "rh-1", "body": json.dumps(queued)}]},
                                None, sqs=sqs)
    assert out == {"batchItemFailures": []}
    with __import__("psycopg").connect(dsn, autocommit=True) as c:
        n = c.execute("""SELECT count(*) FROM setuhaul.chat_messages WHERE sender_type='AGENT' AND reply_to=%s""",
                      (queued["message_id"],)).fetchone()[0]
    assert n == 1                                                       # one stored reply for the driver


def test_a_failed_turn_is_reported_for_redelivery_not_swallowed(lambda_env, monkeypatch):
    lambda_env("agent")
    monkeypatch.setattr("setuhaul.agent.models.model_for", lambda env, db=None, **kw: None)
    bad = {"Records": [{"messageId": "m-bad", "receiptHandle": "rh", "body": json.dumps({"message_id": -1})}]}
    assert lambdas.agent_handler(bad, None, sqs=StubSqs()) == {"batchItemFailures": [{"itemIdentifier": "m-bad"}]}


def test_the_recovery_and_approval_lambdas_run_with_their_own_roles(lambda_env, monkeypatch, conn):
    lambda_env("recovery")
    assert "republished" in lambdas.recovery_handler({}, None)
    lambda_env("approval")
    monkeypatch.setenv("SETUHAUL_APPROVAL_STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:0:stateMachine:approval:1")
    stub = StubSfn()
    lambdas._workflow.cache_clear()
    monkeypatch.setattr(lambdas, "_workflow", lambda: StepFunctionsApprovalWorkflow(
        lambdas.runtime().state.service.db, "arn:aws:states:ap-southeast-2:0:stateMachine:approval:1", stub))
    _, r = pending(conn)
    out = lambdas.approval_relay_handler({}, None)
    assert out["consumed"] >= 1 and str(r["approval_id"]) in stub.started
    assert lambdas.record_task_token_handler({"approval_id": str(r["approval_id"]), "task_token": "t1"}, None)["open"] is True
    assert lambdas.notify_approvers_handler({"approval_id": str(r["approval_id"])}, None)["approval_id"] == str(r["approval_id"])


def test_the_outbound_lambda_sends_stored_replies_with_only_the_system_role(lambda_env, monkeypatch):
    lambda_env("outbound")
    sent = []

    from setuhaul.notify.outbound import SendResult

    class Provider:
        supports_idempotency = True

        def __init__(self, phone_number_id, token):
            assert token == "wa-token"

        def send(self, to, text, idempotency_key):
            sent.append((to, text, idempotency_key))
            return SendResult("ACCEPTED", f"wamid.out.{len(sent)}")
    monkeypatch.setenv("SETUHAUL_WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_TOKEN", "wa-token")
    monkeypatch.setattr("setuhaul.notify.outbound.WhatsAppCloudProvider", Provider)
    out = lambdas.outbound_handler({}, None)
    assert isinstance(out, dict)


# ================================================================ secrets at cold start
class Secrets:
    def __init__(self, value=None, fail=False):
        self.value, self.fail = value, fail

    def get_secret_value(self, SecretId):
        if self.fail:
            raise AwsError("AccessDeniedException")
        return {"SecretString": json.dumps(self.value)}


def test_pilot_secrets_come_from_secrets_manager_only():
    with pytest.raises(RuntimeError, match="must come from Secrets Manager"):
        lambdas.load_app_secrets({"SETUHAUL_ENV": "pilot", "SESSION_SECRET": "x", "SETUHAUL_APP_SECRET_ID": "id"})
    with pytest.raises(RuntimeError, match="SETUHAUL_APP_SECRET_ID is required"):
        lambdas.load_app_secrets({"SETUHAUL_ENV": "pilot"})
    env = lambdas.load_app_secrets({"SETUHAUL_ENV": "pilot", "SETUHAUL_APP_SECRET_ID": "id"},
                                   Secrets({"SESSION_SECRET": "s", "META_APP_SECRET": "m", "WHATSAPP_TOKEN": "w", "other": "x"}))
    assert (env["SESSION_SECRET"], env["META_APP_SECRET"], env["WHATSAPP_TOKEN"]) == ("s", "m", "w") and "other" not in env


def test_an_unreadable_secret_fails_without_revealing_anything():
    with pytest.raises(RuntimeError) as e:
        lambdas.load_app_secrets({"SETUHAUL_ENV": "pilot", "SETUHAUL_APP_SECRET_ID": "arn:secret-name"}, Secrets(fail=True))
    assert "arn:secret-name" not in str(e.value) and "AwsError" in str(e.value)
