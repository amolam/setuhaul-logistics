"""Lambda entry points for the Sydney pilot (build items E1 and E2).

Each function is a thin shell over code the test suite already covers: the FastAPI app, the orchestrator, the inbound
relay's recovery sweep, the outbound relay and the approval outbox relay. Configuration comes from the environment
the infrastructure sets; secrets come from AWS Secrets Manager at cold start and are never logged.

| Handler | Trigger | Database role(s) |
|---|---|---|
| `api_handler` | API Gateway REST API | ingest, console (`/tools` is off in staging and pilot) |
| `agent_handler` | SQS FIFO inbound queue (batch size 1) | agent, feasibility, ingest |
| `recovery_handler` | EventBridge, every minute | system, ingest |
| `outbound_handler` | EventBridge, every minute | system; the only function that reads the WhatsApp token |
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

# Keys the application secret may set (SETUHAUL_APP_SECRET_ID: one JSON secret per environment, read by every function).
SECRET_KEYS = ("SESSION_SECRET", "META_APP_SECRET")
# The WhatsApp sending token lives in its own secret (SETUHAUL_WHATSAPP_SECRET_ID), readable by the outbound function
# only (review IAM-01). It is refused anywhere else.
WHATSAPP_KEY = "WHATSAPP_TOKEN"


def load_app_secrets(env: dict, client=None) -> dict:
    """Copy of `env` with the application secrets filled in from Secrets Manager. Plain-text secrets in the Lambda
    environment are refused in staging and pilot, as the OpenRouter key is (T52)."""
    out = dict(env)
    secret_id = env.get("SETUHAUL_APP_SECRET_ID")
    if env.get("SETUHAUL_ENV") in ("staging", "pilot"):
        present = [k for k in (*SECRET_KEYS, WHATSAPP_KEY) if env.get(k)]
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
        if values.get(WHATSAPP_KEY):
            raise RuntimeError("CONFIG: WHATSAPP_TOKEN must be in the WhatsApp secret, not the application secret")
        out.update({k: str(values[k]) for k in SECRET_KEYS if k in values})
    return out


def load_whatsapp_token(env: dict, client=None) -> str:
    """The WhatsApp sending token, from its own secret. Only the outbound function has the secret id and the IAM grant.
    Outside staging and pilot a plain WHATSAPP_TOKEN in the environment is accepted (local runs and tests)."""
    secret_id = env.get("SETUHAUL_WHATSAPP_SECRET_ID")
    if not secret_id:
        if env.get("SETUHAUL_ENV") in ("staging", "pilot") or not env.get(WHATSAPP_KEY):
            raise RuntimeError("CONFIG: SETUHAUL_WHATSAPP_SECRET_ID is required to send WhatsApp messages")
        return env[WHATSAPP_KEY]
    if client is None:
        import boto3
        client = boto3.client("secretsmanager", region_name=env.get("SETUHAUL_AWS_REGION", "ap-southeast-2"))
    try:
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    except Exception as e:
        raise RuntimeError(f"CONFIG: WhatsApp secret unreadable ({type(e).__name__})") from None
    try:                                                          # a plain string, or {"WHATSAPP_TOKEN": "..."}
        raw = json.loads(raw).get(WHATSAPP_KEY, "") if raw.lstrip().startswith("{") else raw
    except (ValueError, AttributeError):
        raise RuntimeError("CONFIG: WhatsApp secret is not readable JSON") from None
    if not raw:
        raise RuntimeError("CONFIG: WhatsApp secret is empty")
    return str(raw)


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
    from ..agent.models import DeferredModel, model_for
    from ..agent.orchestrator import Orchestrator
    from ..observability import tracing
    from ..observability.langsmith import tracer_from_env
    app = runtime()
    lease = int(os.environ.get("SETUHAUL_PROCESSING_LEASE_S", "120"))
    env, db = dict(os.environ), app.state.service.db
    # G.1 (G3-C2): a missing, empty or unreadable OpenRouter secret, or a budget-configuration problem, no longer stops
    # every turn: the agent answers with the rules reader, records the category, and re-tries building the client at
    # most every 5 minutes. A refused environment, data class, provider or model still fails closed here.
    model = DeferredModel(lambda: model_for(env, db=db),
                          retry_after_s=float(os.environ.get("SETUHAUL_MODEL_RETRY_S", "300"))).prime()
    if model.error is not None:
        log.error("ALERT model client not started (%s %s): the rules reader answers until it can be built",
                  model.error.category, model.error.detail)
    elif model.client is None:                                      # no model configured (provider none): as before
        model = None                                                # G.1 fix: never wrap "no model" in a client
    tracing.install(tracer_from_env(env))                           # off unless SETUHAUL_LANGSMITH_TRACING=on
    return Orchestrator(app.state.service, app.state.sessions, model, lease_s=lease)


def _flush_traces() -> None:
    """After the batch has been processed and its outcome decided: post queued traces, bounded. Never raises."""
    try:
        from ..observability import tracing
        t = tracing.tracer()
        if t.enabled:
            stats = t.flush(float(os.environ.get("SETUHAUL_TRACE_FLUSH_S", "0.5")))
            log.info("trace export %s", {k: stats[k] for k in ("exported", "dropped", "export_errors", "pending")})
    except Exception as e:                                          # noqa: BLE001 — tracing is non-critical
        log.warning("trace flush failed (%s)", type(e).__name__)


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
            log.info("agent turn message=%s path=%s extractor=%s fallback=%s correlation_id=%s turn_ref=%s "
                     "ingest_correlation_id=%s",
                     rec.get("messageId"), None if res is None else "->".join(res.path),
                     getattr(res, "extractor", None), getattr(res, "fallback_reason", None),
                     getattr(res, "correlation_id", None), getattr(res, "turn_ref", None),
                     getattr(res, "ingest_correlation_id", None))
        except Exception:                                          # logged with its type; redelivered by SQS
            log.exception("agent turn failed for SQS message %s", rec.get("messageId"))
            failures.append({"itemIdentifier": rec["messageId"]})
        finally:
            orch.on_heartbeat = None
    _flush_traces()                                                # after every outcome above is final
    return {"batchItemFailures": failures}


def recovery_handler(event, context):
    n = runtime().state.relay.recover()
    log.info("recovery re-published %s inbound messages", n)
    return {"republished": n}


@lru_cache(maxsize=1)
def _whatsapp_token() -> str:
    return load_whatsapp_token(dict(os.environ))


def notification_provider(env: dict | None = None):
    """The outbound provider, fail closed. `SETUHAUL_NOTIFICATION_MODE` is `test_sink` (default: the internal sink,
    no network call) or `whatsapp`, which also needs `SETUHAUL_WHATSAPP_ENABLED=true`; anything else is refused.
    The synthetic pilot runs with the sink: no Meta call, no real phone, no WhatsApp token read."""
    from ..notify.outbound import TestSinkProvider, WhatsAppCloudProvider
    env = dict(os.environ) if env is None else env
    mode = env.get("SETUHAUL_NOTIFICATION_MODE", "test_sink")
    if mode == "test_sink":
        fail = [p for p in env.get("SETUHAUL_TEST_SINK_FAIL_PHONES", "").split(",") if p]
        return TestSinkProvider(fail_phones=fail)
    if mode == "whatsapp":
        if env.get("SETUHAUL_WHATSAPP_ENABLED", "false").lower() != "true":
            raise RuntimeError("CONFIG: SETUHAUL_NOTIFICATION_MODE=whatsapp needs SETUHAUL_WHATSAPP_ENABLED=true")
        return WhatsAppCloudProvider(env["SETUHAUL_WHATSAPP_PHONE_NUMBER_ID"], _whatsapp_token())
    raise RuntimeError(f"CONFIG: unknown SETUHAUL_NOTIFICATION_MODE {mode!r}")


def outbound_handler(event, context):
    """Sends due notifications through `notification_provider()` (the test sink in the synthetic pilot). Outside a
    driver's 24-hour window a WhatsApp notice goes as a Meta-approved template (SETUHAUL_WA_APPROVED_TEMPLATES); with
    none approved it fails with an escalation, never silently (E6-01)."""
    from ..notify.outbound import ApprovedTemplates, OutboundRelay
    app = runtime()
    provider = notification_provider()
    relay = OutboundRelay(app.state.service.db, provider,
                          approved_templates=ApprovedTemplates.parse(os.environ.get("SETUHAUL_WA_APPROVED_TEMPLATES")),
                          window_margin_min=int(os.environ.get("SETUHAUL_WA_WINDOW_MARGIN_MIN", "30")))
    result = relay.run_once(limit=int(os.environ.get("SETUHAUL_OUTBOUND_BATCH", "50")))
    log.info("outbound %s", {k: v for k, v in result.items() if isinstance(v, (int, str))})
    return {k: v for k, v in result.items() if isinstance(v, (int, str, bool))}


@lru_cache(maxsize=1)
def _workflow():
    """Only the relay starts executions, so only the relay has the state machine ARN. The three state-machine task
    functions cannot be given it (the machine references them: a CloudFormation cycle) and do not need it: storing a
    task token, answering one and expiring a request use the token and the database only. Step 6 drill, 23 Nov 2026:
    requiring it here made the token and expiry tasks fail with KeyError, so no execution could wait or expire."""
    from .stepfunctions import StepFunctionsApprovalWorkflow
    return StepFunctionsApprovalWorkflow(runtime().state.service.db, os.environ.get("SETUHAUL_APPROVAL_STATE_MACHINE_ARN", ""))


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
