"""Lambda entry points for the Sydney pilot (build items E1 and E2).

Each function is a thin shell over code the test suite already covers: the FastAPI app, the orchestrator, the inbound
relay's recovery sweep, the outbound relay and the approval outbox relay. Configuration comes from the environment
the infrastructure sets; secrets come from AWS Secrets Manager at cold start and are never logged.

| Handler | Trigger | Database role(s) |
|---|---|---|
| `api_handler` | API Gateway HTTP API | ingest, console (and the read paths the API uses) |
| `agent_handler` | SQS FIFO inbound queue (batch size 1) | agent, feasibility, ingest |
| `recovery_handler` | EventBridge, every minute | system |
| `outbound_handler` | EventBridge, every minute | system |
| `approval_relay_handler` | EventBridge, every minute | system |
| `notify_approvers_handler`, `record_task_token_handler`, `expire_approval_handler` | Step Functions tasks | system |

The booking switch is read from `SETUHAUL_BOOKING_ENABLED` by `Settings.from_env` and defaults to off. With it off
the app starts without the deployment gates; with it on, `create_app` refuses to start unless every gate has current
evidence (DEPLOYMENT_GATES.md). Nothing here can turn it on.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

log = logging.getLogger("setuhaul.lambda")
logging.getLogger().setLevel(os.environ.get("SETUHAUL_LOG_LEVEL", "INFO"))

# Keys an application secret may set (SETUHAUL_APP_SECRET_ID: one JSON secret per environment).
SECRET_KEYS = ("SESSION_SECRET", "META_APP_SECRET", "WHATSAPP_TOKEN")


def load_app_secrets(env: dict, client=None) -> dict:
    """Copy of `env` with the application secrets filled in from Secrets Manager. Plain-text secrets in the Lambda
    environment are refused in staging and pilot, as the OpenRouter key is (T52)."""
    out = dict(env)
    secret_id = env.get("SETUHAUL_APP_SECRET_ID")
    if env.get("SETUHAUL_ENV") in ("staging", "pilot"):
        present = [k for k in SECRET_KEYS if env.get(k)]
        if present:
            raise RuntimeError(f"CONFIG: {present} must come from Secrets Manager (SETUHAUL_APP_SECRET_ID), "
                               "not the environment")
        if not secret_id:
            raise RuntimeError("CONFIG: SETUHAUL_APP_SECRET_ID is required in staging and pilot")
    if secret_id:
        if client is None:
            import boto3
            client = boto3.client("secretsmanager", region_name=env.get("SETUHAUL_AWS_REGION", "ap-southeast-2"))
        try:
            values = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
        except Exception as e:                                   # never echo the secret or its id's contents
            raise RuntimeError(f"CONFIG: application secret unreadable ({type(e).__name__})") from None
        out.update({k: str(values[k]) for k in SECRET_KEYS if k in values})
    return out


@lru_cache(maxsize=1)
def runtime():
    """Built once per Lambda container: the app (and through it the service, sessions, queue and relay)."""
    from ..api.app import Settings, create_app
    env = load_app_secrets(dict(os.environ))
    os.environ.update({k: env[k] for k in SECRET_KEYS if k in env})   # Settings.from_env reads the process env
    return create_app(Settings.from_env())


# ---------------------------------------------------------------- E1: the HTTP API
@lru_cache(maxsize=1)
def _asgi():
    from mangum import Mangum
    return Mangum(runtime(), lifespan="off")


def api_handler(event, context):
    return _asgi()(event, context)


# ---------------------------------------------------------------- E2: workers
@lru_cache(maxsize=1)
def _orchestrator():
    from ..agent.models import model_for
    from ..agent.orchestrator import Orchestrator
    app = runtime()
    lease = int(os.environ.get("SETUHAUL_PROCESSING_LEASE_S", "120"))
    model = model_for(dict(os.environ), db=app.state.service.db)     # fails closed on any configuration problem
    return Orchestrator(app.state.service, app.state.sessions, model, lease_s=lease)


def agent_handler(event, context, sqs=None):
    """One SQS FIFO record per invocation (batch size 1 keeps one driver's messages in order). While the turn runs,
    the orchestrator's heartbeat extends the message's visibility, so the queue does not hand it to another worker.
    A failed record is reported back (ReportBatchItemFailures) and redelivered; the turn fence makes that safe."""
    orch = _orchestrator()
    queue_url = os.environ["SETUHAUL_QUEUE_URL"]
    if sqs is None:
        import boto3
        sqs = boto3.client("sqs")
    failures = []
    for rec in event.get("Records", []):
        receipt = rec["receiptHandle"]

        def extend(_ev, receipt=receipt):
            sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt,
                                          VisibilityTimeout=2 * orch.lease_s)
        orch.on_heartbeat = extend
        try:
            res = orch.handle(json.loads(rec["body"]))
            log.info("agent turn message=%s path=%s extractor=%s", rec.get("messageId"),
                     None if res is None else "->".join(res.path), getattr(res, "extractor", None))
        except Exception:                                          # logged with its type; redelivered by SQS
            log.exception("agent turn failed for SQS message %s", rec.get("messageId"))
            failures.append({"itemIdentifier": rec["messageId"]})
        finally:
            orch.on_heartbeat = None
    return {"batchItemFailures": failures}


def recovery_handler(event, context):
    n = runtime().state.relay.recover()
    log.info("recovery re-published %s inbound messages", n)
    return {"republished": n}


def outbound_handler(event, context):
    from ..notify.outbound import OutboundRelay, WhatsAppCloudProvider
    app = runtime()
    provider = WhatsAppCloudProvider(os.environ["SETUHAUL_WHATSAPP_PHONE_NUMBER_ID"], os.environ["WHATSAPP_TOKEN"])
    result = OutboundRelay(app.state.service.db, provider).run_once(limit=int(os.environ.get("SETUHAUL_OUTBOUND_BATCH", "50")))
    log.info("outbound %s", {k: v for k, v in result.items() if isinstance(v, (int, str))})
    return {k: v for k, v in result.items() if isinstance(v, (int, str, bool))}


@lru_cache(maxsize=1)
def _workflow():
    from .stepfunctions import StepFunctionsApprovalWorkflow
    return StepFunctionsApprovalWorkflow(runtime().state.service.db, os.environ["SETUHAUL_APPROVAL_STATE_MACHINE_ARN"])


def approval_relay_handler(event, context):
    from ..workflow.approvals import ApprovalOutboxRelay
    wf = _workflow()
    started = ApprovalOutboxRelay(wf.db, wf).run_once()
    resent = wf.reconcile()
    log.info("approval relay consumed=%s reconciled=%s", started, resent)
    return {"consumed": started, "reconciled": resent}


# ---------------------------------------------------------------- Step Functions tasks
def notify_approvers_handler(event, context):
    """Planners see open requests in the console (Console → Requests); this task records that the reminder point was
    reached. A push channel for planners is not part of the pilot."""
    log.info("approval %s waiting for a planner", event["approval_id"])
    return {"approval_id": event["approval_id"]}


def record_task_token_handler(event, context):
    open_ = _workflow().record_token(event["approval_id"], event["task_token"])
    return {"approval_id": event["approval_id"], "open": open_}


def expire_approval_handler(event, context):
    return {"approval_id": event["approval_id"], "outcome": _workflow().expire(event["approval_id"])}
