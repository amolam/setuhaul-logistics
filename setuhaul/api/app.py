"""HTTP surface. Three audiences, three authentication mechanisms, no shared endpoints.

    /webhooks/whatsapp   Meta → us            X-Hub-Signature-256 (HMAC of the raw body)
    /tools/{tool}        agent orchestrator   X-Setuhaul-Session (driver session token); off in staging and pilot
    /console/...         planners, ops        Authorization: Bearer <Cognito access token> [+ API Gateway JWT authorizer]

Run locally:  uvicorn setuhaul.api.app:app  (settings from environment, see Settings.from_env)
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .db import CORRELATION_ID, Db
from .inbound import (InboundRelay, LocalQueue, SqsFifoQueue, booking_authorization,
                      check_booking_preconditions, deployment_id_for, load_manifest, manifest_from_database, own_code_sha256)
from .security import AuthError, CognitoVerifier, Principal, SessionTokens, meta_signature_ok
from .service import BookingService, ServiceError


@dataclass
class Settings:
    dsn: str
    cognito_issuer: str
    cognito_client_ids: set[str]
    session_secret: bytes
    meta_app_secret: bytes
    facilities: tuple[str, ...]
    jwks: dict | None = None                    # static JWKS for tests / air-gapped; None = fetch from Cognito
    booking_enabled: bool = False               # production booking switch; stays off until step 4 safeguards
    queue_url: str | None = None                # SQS FIFO queue for inbound events; None = in-process LocalQueue
    processing_lease_s: int = 120               # orchestrator turn lease; the queue visibility must be >= 2x this
    environment: str | None = None              # e.g. "prod-ap-south-1"; deployment evidence is bound to it
    deployment_id: str | None = None            # optional cross-check; the real id is derived from the manifest
    deployment_manifest: str | None = None      # R2.1: path to the immutable deployment manifest shipped with the build
    # 0026: "database" = the manifest the pipeline recorded, checked against this function's own code hash (AWS)
    manifest_source: str = "file"
    # Tests and the replay only: lets the booking path run on LocalQueue without deployment gates. from_env never
    # sets it, so a deployed service with the switch on always goes through check_booking_preconditions (S4-05).
    local_test_harness: bool = False
    # /tools/{tool} serves an orchestrator that runs outside AWS. In AWS the agent Lambda calls the service directly,
    # so the route is off in staging and pilot whatever the environment says (review IAM-02): the API function then
    # needs no agent or feasibility database login.
    tools_route: bool = True
    # The hosted staff console, served by this API under /app/ (same origin, so no CORS): its Cognito hosted domain
    # and the public client id. Unset: no console page.
    cognito_domain: str | None = None
    console_client_id: str | None = None
    # Phase G1: rank driver options with the slot optimizer (booking policy: the owner switches it on per environment)
    optimizer_ranking: bool = False
    # Roadmap Phase 2: how long a shown option / dispatcher offer stays acceptable (SETUHAUL_OPTION_TTL_S, default 300)
    option_ttl_s: int | None = None
    # Roadmap Phase 5: Telegram webhook secret (setWebhook secret_token). Unset: the /webhooks/telegram route does not
    # exist. In AWS it comes from Secrets Manager (SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID; notify.telegram.
    # webhook_secret_from_env); SETUHAUL_TELEGRAM_WEBHOOK_ENABLED=false removes the route (inbound kill switch).
    telegram_webhook_secret: str | None = None
    # Where webhook replies go (link results, button answers, cleared buttons): None = the in-process test sink. With
    # SETUHAUL_TELEGRAM_DELIVERY=test_bot this is the allowlisted test bot, and `telegram_allowlist` makes the webhook
    # ignore every account outside it (setuhaul.notify.telegram.telegram_from_env; fail closed).
    telegram_replies: object | None = None
    telegram_allowlist: frozenset | None = None
    telegram_mode: str | None = None                  # SETUHAUL_TELEGRAM_DELIVERY as configured (shown by /health)
    local_runner: bool = False                        # the local test-bot runner only (see /health); never from env

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(dsn=os.environ["SETUHAUL_DSN"], cognito_issuer=os.environ["COGNITO_ISSUER"],
                   cognito_client_ids=set(os.environ["COGNITO_CLIENT_IDS"].split(",")),
                   session_secret=os.environ["SESSION_SECRET"].encode(), meta_app_secret=os.environ["META_APP_SECRET"].encode(),
                   facilities=tuple(os.environ.get("FACILITIES", "FAC-JAI-01,FAC-GGN-01").split(",")),
                   booking_enabled=os.environ.get("SETUHAUL_BOOKING_ENABLED", "false").lower() == "true",
                   queue_url=os.environ.get("SETUHAUL_QUEUE_URL") or None,
                   processing_lease_s=int(os.environ.get("SETUHAUL_PROCESSING_LEASE_S", "120")),
                   environment=os.environ.get("SETUHAUL_ENVIRONMENT") or None,
                   cognito_domain=os.environ.get("COGNITO_DOMAIN") or None,
                   console_client_id=os.environ.get("COGNITO_CONSOLE_CLIENT_ID") or None,
                   optimizer_ranking=os.environ.get("SETUHAUL_OPTIMIZER_RANKING", "off").lower() in ("on", "true", "1"),
                   option_ttl_s=int(os.environ["SETUHAUL_OPTION_TTL_S"]) if os.environ.get("SETUHAUL_OPTION_TTL_S") else None,
                   **_telegram_from_env(),
                   deployment_id=os.environ.get("SETUHAUL_DEPLOYMENT_ID") or None,
                   deployment_manifest=os.environ.get("SETUHAUL_DEPLOYMENT_MANIFEST") or None,
                   manifest_source=os.environ.get("SETUHAUL_DEPLOYMENT_MANIFEST_SOURCE", "file"),
                   tools_route=(os.environ.get("SETUHAUL_TOOLS_ROUTE", "on").lower() not in ("off", "false", "0")
                                and os.environ.get("SETUHAUL_ENV") not in ("staging", "pilot")))


def _telegram_from_env() -> dict:
    """The webhook secret and the transport behind webhook replies. Only test_bot / live put a real transport there;
    anything else keeps the test sink. test_bot (local/dev only) still raises on any missing piece (the app does not
    start). LIVE fails closed on TELEGRAM ONLY: an unreadable token or webhook secret, or a refused environment, logs an
    ALERT and leaves the webhook route absent (Telegram keeps the updates and redelivers once fixed), so the console,
    booking and every other route keep working. Nothing secret is logged."""
    from ..notify.telegram import telegram_from_env, webhook_secret_from_env
    env = dict(os.environ)
    mode = env.get("SETUHAUL_TELEGRAM_DELIVERY", "test_sink")
    out: dict = {"telegram_mode": mode}
    if mode == "live":
        try:
            transport, allow = telegram_from_env(env)
            out.update(telegram_replies=transport, telegram_allowlist=allow)
        except RuntimeError as e:
            log.error("ALERT Telegram live unavailable (%s): the webhook route is not registered", str(e)[:160])
            return {"telegram_mode": "live_unavailable"}
    elif mode == "test_bot":
        transport, allow = telegram_from_env(env)                # raises on any missing piece: the app does not start
        out.update(telegram_replies=transport, telegram_allowlist=allow)
    try:
        out["telegram_webhook_secret"] = webhook_secret_from_env(env)
    except RuntimeError as e:
        if mode == "test_bot":
            raise
        log.error("ALERT Telegram webhook secret unavailable (%s): the webhook route is not registered", str(e)[:160])
        out["telegram_webhook_secret"] = None
    return out


def _features_public(settings) -> dict:
    """The six independent controls for /health. Never a secret, token, chat id, key or budget figure."""
    try:
        from ..ops.features import resolve
        f = resolve(dict(os.environ))
        return {"booking_switch": f.booking_enabled, "telegram_delivery_mode": settings.telegram_mode or f.telegram_delivery_mode,
                "telegram_live_authorized": f.telegram_live_authorized,
                "telegram_outbound_enabled": f.telegram_outbound_enabled,
                "telegram_webhook_enabled": bool(settings.telegram_webhook_secret),     # the route is registered
                "telegram_driver_allowlist": bool((os.environ.get("SETUHAUL_TELEGRAM_AUTHORIZED_DRIVERS") or "").strip()),
                "llm_judge_enabled": f.llm_judge_enabled, "llm_judge_advisory_only": True,
                "langsmith_tracing_enabled": f.langsmith_tracing_enabled, "production": f.production}
    except Exception:                                               # noqa: BLE001 — diagnostics never break /health
        return {"unavailable": True}


class ToolCall(BaseModel):
    model_config = {"extra": "forbid"}
    turn_id: int = Field(ge=0)
    args: dict = Field(default_factory=dict)


class Decision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["CONFIRM", "REJECT"]
    idempotency_key: str = Field(min_length=16, max_length=128)
    note: str | None = Field(default=None, max_length=500)


class Revoke(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str = Field(min_length=5, max_length=300)


class KillSwitch(BaseModel):
    model_config = {"extra": "forbid"}
    engaged: bool
    reason: str = Field(min_length=5, max_length=300)


class Pause(BaseModel):
    model_config = {"extra": "forbid"}
    paused: bool
    reason: str = Field(min_length=3, max_length=300)


class ApprovalModeIn(BaseModel):
    """0047: MANUAL (a planner approves every booking) or AUTO (eligible driver-selected slots confirm at once)."""
    model_config = {"extra": "forbid"}
    mode: Literal["MANUAL", "AUTO"]
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=300)


class Block(BaseModel):
    model_config = {"extra": "forbid"}
    event_id: str = Field(min_length=3, max_length=64)
    start: AwareDatetime
    end: AwareDatetime
    type: Literal["MAINTENANCE", "BREAKDOWN", "MANUAL_BLOCK"]
    reason: str = Field(min_length=3, max_length=300)

    @model_validator(mode="after")
    def _ordered(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Withdraw(BaseModel):
    model_config = {"extra": "forbid"}
    reason_code: Literal["DRIVER_REQUESTED", "OPERATIONS_DECISION", "DATA_CORRECTION"]
    note: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=128)


class HoldIn(BaseModel):
    model_config = {"extra": "forbid"}
    facility_id: str = Field(min_length=3, max_length=40)
    dock_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=3, max_length=300)


class HoldClear(BaseModel):
    model_config = {"extra": "forbid"}
    note: str = Field(min_length=3, max_length=300)


class Takeover(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str = Field(min_length=3, max_length=300)


class Handback(BaseModel):
    model_config = {"extra": "forbid"}
    summary: str = Field(min_length=10, max_length=1000)


ScheduleRule = Literal["FACILITY_SCORE", "FCFS", "EARLIEST_ETA"]


class ScheduleStore(BaseModel):
    model_config = {"extra": "forbid"}
    rule: ScheduleRule = "FACILITY_SCORE"
    ttl_s: int = Field(default=600, ge=60, le=1800)


class OfferIn(BaseModel):
    model_config = {"extra": "forbid"}
    shipment_id: str = Field(min_length=3, max_length=80)
    driver_id: str = Field(min_length=3, max_length=80)      # the assigned driver, or another one (reassignment)
    vehicle_id: str = Field(min_length=3, max_length=80)     # confirmed; a vehicle change is refused in this scope
    dock_code: str = Field(min_length=1, max_length=20)
    start: AwareDatetime
    idempotency_key: str = Field(min_length=16, max_length=128)
    # 0048 (issue 5): who the dispatcher chose. ASSIGNED = "This driver", OTHER = "Another driver" (never the assigned
    # one). Optional for older clients; the console always sends it.
    target: Literal["ASSIGNED", "OTHER"] | None = None


class LifecycleStep(BaseModel):
    """0048: gate arrival, at dock, completed (operations / ops_lead)."""
    model_config = {"extra": "forbid"}
    note: str | None = Field(default=None, max_length=300)


class NoShowRebook(BaseModel):
    model_config = {"extra": "forbid"}
    note: str = Field(min_length=3, max_length=300)


class RescheduleIn(BaseModel):
    """0048 (issue 6): a dispatcher asks a planner for a new time for a confirmed booking."""
    model_config = {"extra": "forbid"}
    dock_code: str = Field(min_length=1, max_length=20)
    start: AwareDatetime
    note: str = Field(min_length=3, max_length=300)
    idempotency_key: str = Field(min_length=16, max_length=128)


class ReassignIn(BaseModel):
    model_config = {"extra": "forbid"}
    shipment_id: str = Field(min_length=3, max_length=80)
    driver_id: str = Field(min_length=3, max_length=80)
    idempotency_key: str = Field(min_length=16, max_length=128)


class CancelIn(BaseModel):
    model_config = {"extra": "forbid"}
    reason_code: Literal["DRIVER_UNAVAILABLE", "VEHICLE_BREAKDOWN", "SHIPMENT_CANCELLED", "CUSTOMER_REQUEST",
                         "OPERATIONS_DECISION"]
    note: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=128)


class CancelDecision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=500)


class EscalationNote(BaseModel):
    model_config = {"extra": "forbid"}
    note: str | None = Field(default=None, max_length=300)


class EscalationResolve(BaseModel):
    model_config = {"extra": "forbid"}
    resolution: str = Field(min_length=5, max_length=500)


class Move(BaseModel):
    model_config = {"extra": "forbid"}
    proposal_id: uuid.UUID
    idempotency_key: str = Field(min_length=16, max_length=128)
    note: str | None = Field(default=None, max_length=500)


log = logging.getLogger("setuhaul.api")
# G.1 (G5): the shipment id format the console validates against before it asks (mirrored in console_static/app.js;
# tests/test_phase_g1.py keeps the two equal). Pilot ids look like SYN-SHP-2172ACB8; seeded ones SYN-SHP1001. Ids are
# case-sensitive and other sources use lower-case hex (SHP-99399e2f-0), so both cases are accepted.
SHIPMENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def create_app(settings: Settings, clock: Callable[[], datetime] | None = None, queue=None) -> FastAPI:
    app = FastAPI(title="SetuHaul exception service", version="0.2.0", docs_url=None, redoc_url=None)
    from .console_site import mount_console
    mount_console(app, settings.cognito_domain, settings.console_client_id)

    @app.middleware("http")
    async def _no_store_for_staff_data(request: Request, call_next):
        # Phase G3: one correlation id per request (a well-formed caller id is kept, else a new uuid); echoed back
        # and carried into the structured logs and optimizer runs. It identifies a request, never a person.
        cid = request.headers.get("x-correlation-id", "")
        try:
            cid = str(uuid.UUID(cid))
        except ValueError:
            cid = str(uuid.uuid4())
        token = CORRELATION_ID.set(cid)
        try:
            response = await call_next(request)
        finally:
            CORRELATION_ID.reset(token)
        response.headers["X-Correlation-Id"] = cid
        if request.url.path.startswith("/console/"):      # staff data: never kept by a browser or proxy cache
            response.headers["Cache-Control"] = "no-store"
        return response
    from ..aws.data_api import make_db                     # SETUHAUL_DSN=rds-data: the Data API backend (E3)
    db = make_db(settings.dsn)
    svc = BookingService(db, clock or (lambda: datetime.now(timezone.utc)), settings.facilities,
                         booking_enabled=settings.booking_enabled, optimizer_ranking=settings.optimizer_ranking,
                         option_ttl_s=settings.option_ttl_s)
    verifier = CognitoVerifier(settings.cognito_issuer, settings.cognito_client_ids, settings.jwks)
    sessions = SessionTokens(settings.session_secret)
    # Inbound events for the agent orchestrator (B-WF-1). In AWS this is SQS FIFO (inbound.SqsFifoQueue: message group
    # = driver, deduplication id = WhatsApp message id); locally an in-process LocalQueue. The session token travels
    # only on this internal queue and is never returned to the webhook caller.
    inbound = queue if queue is not None else (SqsFifoQueue(settings.queue_url) if settings.queue_url else LocalQueue())
    # D4 (Recovery Release A): what this container passed at start-up. /health combines it with the database's verdict
    # now; lambdas.runtime() replaces it when a cold start fails its gates and the container runs booking-OFF instead.
    app.state.booking_gate = {"switch": settings.booking_enabled, "ok": settings.booking_enabled, "reason": None}
    if settings.booking_enabled and not settings.local_test_harness:
        # S4-05: the switch fails closed. Durable FIFO queue with a dead-letter queue and a visibility timeout that
        # covers the processing lease, plus recorded evidence for every deployment gate, or the service does not start.
        own_code = None
        if settings.manifest_source == "database" and settings.environment:
            own_code = own_code_sha256()
            manifest = manifest_from_database(db, settings.environment, own_code)
        else:
            manifest = load_manifest(settings.deployment_manifest) if settings.deployment_manifest else None
        app.state.provisional = {}
        app.state.gate_exceptions = check_booking_preconditions(db, inbound, settings.processing_lease_s,
                                                                settings.environment, manifest, settings.deployment_id,
                                                                provisional_out=app.state.provisional)
        # 0030: declare the accepted deployment in every transaction; the database re-checks it at each booking
        # commitment (newest manifest, same code, current evidence), so a warm container cannot outlive its evidence
        db.deployment = {"environment": settings.environment, "deployment_id": deployment_id_for(manifest),
                         "code_sha256": own_code or manifest["gates"]["RECOVERY_WORKER"]["artifact_sha256"]}
        # one line per container start (api and agent), so the rollout can verify what each function declares
        log.info("booking evidence guard: this process declares environment=%s deployment=%s code=%s",
                 db.deployment["environment"], db.deployment["deployment_id"], db.deployment["code_sha256"][:12])
    relay = InboundRelay(svc, inbound, sessions)
    app.state.service, app.state.sessions, app.state.inbound, app.state.relay = svc, sessions, inbound, relay

    @app.exception_handler(ServiceError)
    async def _service_error(_req, e: ServiceError):
        # 0048 (issue 2): a plain sentence for the console next to the code; the code stays for tools and tests
        from .lifecycle import refusal_text
        return JSONResponse({"error": e.code, "detail": e.detail, "message": refusal_text(e.code)}, status_code=e.http)

    @app.get("/health")
    def health():
        """Readiness for the load balancer and the pilot run sheet (Phase G). Public and read-only: whether the database
        answers and whether booking is enabled, and nothing else (no DSN, key, queue URL or data).

        D4 (Recovery Release A): booking_enabled is the DATABASE's answer now, not the start-up snapshot: the switch is
        on, this container passed its gates, the database authorizes this deployment, its code and its release identity
        (booking_authorization_status, 0033) and the kill switch is off. booking_state says which: off | authorized |
        refused | unknown. Anything that cannot be verified is unknown and booking_enabled false (fail closed), so a
        warm container and a cold one report the same thing."""
        try:
            with db.tx("ingest", readonly=True) as c:              # a role the API workload has anyway (E3)
                c.execute("SELECT 1")
            database = "ok"
        except psycopg.Error:
            database = "unreachable"
        gate = getattr(app.state, "booking_gate", None) or {"switch": False, "ok": False, "reason": None}
        body = {"status": "ok" if database == "ok" else "degraded", "database": database, "booking_enabled": False,
                "booking_state": "off", "environment": settings.environment, "version": app.version,
                # Telegram delivery mode only (test_sink | test_bot | live | off): no token, chat or allowlist
                "telegram": settings.telegram_mode or ("test_bot" if settings.telegram_allowlist is not None
                                                       else "live" if settings.telegram_replies is not None
                                                       else "test_sink"),
                # Full Activation: the independent controls as this function resolves them (setuhaul.ops.features):
                # modes and booleans only; booking_enabled above stays the database's answer
                "features": _features_public(settings)}
        if not gate["switch"]:
            return JSONResponse(body, status_code=200 if database == "ok" else 503)
        body["booking_switch"] = "on"
        if database != "ok":
            body["booking_state"] = "unknown"
            return JSONResponse(body, status_code=503)
        if not gate["ok"]:
            body.update(booking_state="refused", booking_refusal=f"start-up gates not satisfied: {gate['reason']}"[:400])
        else:
            body["gate_exceptions"] = sorted(getattr(app.state, "gate_exceptions", None) or {})
        declared = getattr(db, "deployment", None) or {}
        try:
            st = booking_authorization(db, declared.get("environment") or settings.environment,
                                       declared.get("deployment_id"), declared.get("code_sha256"))
        except Exception:                                           # noqa: BLE001 — unverifiable: fail closed
            body.update(booking_state="unknown", booking_kill_switch="unknown")
            return JSONResponse(body, status_code=200)
        body["booking_kill_switch"] = "ENGAGED" if st.get("kill_switch") else "disengaged"
        if settings.local_runner:
            # the owner-run local test-bot runner (scripts/telegram_test_bot.py): bookings run on a local synthetic
            # database WITHOUT the release gates (no deployment exists to authorize). booking_enabled stays false;
            # the console labels it a local harness. Never set by Settings.from_env (no deployment can turn it on).
            body["booking_state"] = "local_test_harness"
            return JSONResponse(body, status_code=200)
        if gate["ok"]:
            if st.get("authorized") is True:
                body.update(booking_enabled=True, booking_state="authorized")
            else:
                body.update(booking_state="refused",
                            booking_refusal=(st.get("verdict") or "the booking kill switch is engaged")[:400])
        if st.get("provisional_open") and st.get("provisional_until"):     # 0032: an open Option P window, never an exception
            body["booking_provisional_until"] = st["provisional_until"]
        return JSONResponse(body, status_code=200)

    def staff(authorization: str | None = Header(default=None)) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            return verifier.verify(authorization.removeprefix("Bearer "))
        except AuthError as e:
            raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from e

    def driver(x_setuhaul_session: str | None = Header(default=None)) -> Principal:
        if not x_setuhaul_session:
            raise HTTPException(401, "missing session")
        try:
            return sessions.verify(x_setuhaul_session)
        except AuthError as e:
            raise HTTPException(401, str(e)) from e

    # ---------------------------------------------------------------- inbound WhatsApp
    @app.post("/webhooks/whatsapp")
    async def whatsapp(request: Request, x_hub_signature_256: str | None = Header(default=None)):
        raw = await request.body()
        if not meta_signature_ok(raw, x_hub_signature_256, settings.meta_app_secret):
            raise HTTPException(401, "bad signature")
        try:
            body = json.loads(raw)
        except ValueError:
            log.warning("signed webhook with an unreadable body")
            return {"status": "received"}
        not_stored = 0
        for entry in body.get("entry", []) if isinstance(body, dict) else []:
            for ch in entry.get("changes", []):
                for st in ch.get("value", {}).get("statuses", []):  # B-WF-3 delivery receipts for our messages
                    try:
                        status = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}.get(
                            str(st.get("status")))
                        if status:
                            err = (st.get("errors") or [{}])[0].get("code")
                            await run_in_threadpool(svc.record_delivery_status, st.get("id"),
                                                    st.get("biz_opaque_callback_data"), status,
                                                    str(err) if err else None)
                    except Exception:
                        # R3-P2-02: a receipt that was not stored could have settled an ambiguous send. Answer 5xx so
                        # the provider redelivers the batch; receipts and messages are idempotent on the retry.
                        log.exception("could not record a delivery status; the provider will retry")
                        not_stored += 1
                for m in ch.get("value", {}).get("messages", []):
                    try:                                             # a malformed message never fails the batch
                        if m.get("type", "text") != "text":
                            continue                                 # media / location handled in a later step
                        phone, ext, text = "+" + str(m["from"]).lstrip("+"), str(m["id"]), str(m["text"]["body"])
                    except (KeyError, TypeError, AttributeError):
                        log.warning("skipped one malformed inbound message")
                        continue
                    try:
                        # database work off the event loop: simultaneous deliveries really run in parallel
                        event = await run_in_threadpool(svc.ingest, phone, ext, text, sessions)
                    except Exception:
                        # S4-01: a valid message that was NOT stored must not be acknowledged: answer 5xx so the
                        # provider retries. Messages of the batch that were stored are deduplicated on the retry.
                        log.exception("could not store an inbound message; the provider will retry")
                        not_stored += 1
                        continue
                    if "session_token" in event:
                        try:                                         # stored and claimed: the receipt is durable
                            await run_in_threadpool(relay.publish, event)
                        except Exception:
                            log.exception("stored but not published; the recovery sweep will publish it")
        if not_stored:
            return JSONResponse({"status": "retry"}, status_code=503)
        # Constant answer: reveals neither tokens nor whether a number is known.
        return {"status": "received"}

    # ---------------------------------------------------------------- inbound Telegram (roadmap Phase 5, 0035)
    if settings.telegram_webhook_secret:
        from ..notify.telegram import REMOVE_KEYBOARD, TelegramGateway, secret_ok
        # test sink (default): replies are logged and kept, never sent. test_bot: the allowlisted test bot.
        app.state.telegram = TelegramGateway(settings.telegram_replies)
        tg_allow = settings.telegram_allowlist
        if len(settings.telegram_webhook_secret) < 32:
            raise ValueError("SETUHAUL_TELEGRAM_WEBHOOK_SECRET must be at least 32 characters")

        @app.post("/webhooks/telegram")
        async def telegram(request: Request,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)):
            if not secret_ok(x_telegram_bot_api_secret_token, settings.telegram_webhook_secret):
                raise HTTPException(401, "bad secret")
            raw = await request.body()
            if len(raw) > 64 * 1024:
                raise HTTPException(413, "too large")
            try:
                update = json.loads(raw)
            except ValueError:
                return {"ok": True}
            if tg_allow is not None:
                # test_bot: an account outside the test allowlist is not processed at all (no link, no reply)
                frm = ((update.get("message") or (update.get("callback_query") or {})) or {}).get("from") \
                    if isinstance(update, dict) else None
                if not (isinstance(frm, dict) and str(frm.get("id")) in tg_allow):
                    return {"ok": True}
            from ..observability import tracing
            tr = tracing.tracer()
            kind = next((k for k in ("callback_query", "message") if isinstance(update, dict) and k in update), "other")
            try:
                # Full Activation: one telegram_inbound trace per verified update (driver id set by the service once
                # identified; never the chat id, the user id, the text or the secret). No-op when tracing is off.
                # its run id IS the request's correlation id, which the queue event carries to the agent as the
                # turn's ingest_correlation_id: the agent_turn trace names this inbound trace (Demo release)
                msg = (update.get("message") or (update.get("callback_query") or {}).get("message") or {}) \
                    if isinstance(update, dict) else {}
                frm = ((update.get("message") or update.get("callback_query") or {}).get("from") or {}) \
                    if isinstance(update, dict) else {}
                full = tr.full_content
                with tr.turn(CORRELATION_ID.get() or str(uuid.uuid4()), "telegram_inbound",
                             # issue 8: in full content mode (synthetic pilot only) the message text, scrubbed
                             inputs={"text": msg.get("text")} if full and isinstance(msg.get("text"), str) else None,
                             metadata={"channel": "TELEGRAM", "component": "telegram_inbound", "update_kind": kind,
                                       "webhook_received_at": _utcnow_iso()}):
                    # the account's own identifiers are masked wherever they appear in exported text (every mode)
                    # 27 Nov 2026 (owner, DEMO-04): the account's NAME only; ids and the username are exported
                    tracing.safe(lambda: tr.add_pii_terms(frm.get("first_name"), frm.get("last_name"),
                                                          " ".join(x for x in (frm.get("first_name"), frm.get("last_name"))
                                                                   if isinstance(x, str) and x)))
                    out = await run_in_threadpool(svc.handle_telegram_update, update, sessions)
            except Exception:
                # not stored: answer 5xx so Telegram redelivers; the stored part is deduplicated on the retry
                log.exception("could not handle a Telegram update; Telegram will retry")
                return JSONResponse({"ok": False}, status_code=503)
            finally:
                if tr.enabled:
                    await run_in_threadpool(lambda: tracing.safe(tr.flush, 0.5))
            if out.get("event"):
                try:
                    await run_in_threadpool(relay.publish, out["event"])
                except Exception:
                    log.exception("stored but not published; the recovery sweep will publish it")
            cq_msg = (update.get("callback_query") or {}).get("message") or {}
            chat = (update.get("message") or cq_msg or {}).get("chat", {})
            cq_id = (update.get("callback_query") or {}).get("id")
            if isinstance(cq_id, str):                    # stop the tapped button's spinner first
                app.state.telegram.answer_callback(cq_id)
            if out.get("clear_buttons") and isinstance(chat.get("id"), int) and isinstance(cq_msg.get("message_id"), int):
                app.state.telegram.clear_buttons(str(chat["id"]), cq_msg["message_id"])
            if isinstance(out.get("clear_message_id"), int) and isinstance(chat.get("id"), int):
                app.state.telegram.clear_buttons(str(chat["id"]), out["clear_message_id"])   # typed yes / no (0038)
            if out.get("reply") and isinstance(chat.get("id"), int):
                app.state.telegram.reply(str(chat["id"]), out["reply"],
                                         REMOVE_KEYBOARD if out.get("remove_keyboard") else None)
            return {"ok": True}                         # constant: reveals nothing about links or drivers

    # ---------------------------------------------------------------- agent tools
    if settings.tools_route:
        @app.post("/tools/{tool}")
        def tool(tool: str, call: ToolCall, p: Principal = Depends(driver)):
            return svc.call_tool(p, tool, call.turn_id, call.args)

    # ---------------------------------------------------------------- console
    @app.get("/console/requests")
    def requests_(facility_id: str | None = None, p: Principal = Depends(staff)):
        return {"requests": svc.list_requests(p, facility_id)}

    @app.post("/console/requests/{appointment_id}/decision")
    def decide(appointment_id: str, d: Decision, p: Principal = Depends(staff)):
        out = svc.decide(p, appointment_id, d.decision, d.note, d.idempotency_key)
        from ..notify.kick import kick_outbound
        kick_outbound("decision")                   # the driver's notice goes out now, not on the next tick
        return out

    @app.post("/console/requests/{appointment_id}/withdraw")
    def withdraw(appointment_id: str, w: Withdraw, p: Principal = Depends(staff)):
        return svc.withdraw(p, appointment_id, w.reason_code, w.note, w.idempotency_key)

    @app.get("/console/policy/outcomes")
    def policy_outcomes(facility: str | None = None, p: Principal = Depends(staff)):
        return svc.policy_outcomes(p, facility)

    @app.post("/console/holds")
    def set_hold(h: HoldIn, p: Principal = Depends(staff)):
        return {"hold_id": svc.set_hold(p, h.facility_id, h.dock_id, h.reason)}

    @app.post("/console/holds/{hold_id}/clear")
    def clear_hold(hold_id: int, h: HoldClear, p: Principal = Depends(staff)):
        return {"cleared": svc.clear_hold(p, hold_id, h.note)}

    @app.get("/console/booking-kill-switch")
    def kill_switch_state(p: Principal = Depends(staff)):
        return svc.kill_switch(p)

    @app.post("/console/booking-kill-switch")
    def kill_switch_set(body: KillSwitch, p: Principal = Depends(staff)):
        """0026: an ops_lead engages (or disengages) the database's booking kill switch. Audited; no deployment."""
        return svc.set_kill_switch(p, body.engaged, body.reason)

    @app.post("/console/provisional-authorization/revoke")
    def provisional_revoke(body: Revoke, p: Principal = Depends(staff)):
        """0032: an ops_lead revokes the open provisional booking authorization (Option P). Audited; no deployment."""
        return svc.revoke_provisional(p, settings.environment or "", body.reason)

    @app.post("/console/facilities/{facility_id}/pause")
    def pause(facility_id: str, body: Pause, p: Principal = Depends(staff)):
        return {"automation_paused": svc.pause(p, facility_id, body.paused, body.reason)}

    @app.get("/console/facilities/{facility_id}/approval-mode")
    def approval_mode(facility_id: str, p: Principal = Depends(staff)):
        """0047: Planner settings: the facility's booking approval mode (MANUAL by default)."""
        return svc.approval_mode(p, facility_id)

    @app.put("/console/facilities/{facility_id}/approval-mode")
    def set_approval_mode(facility_id: str, body: ApprovalModeIn, p: Principal = Depends(staff)):
        """0047: planner or ops_lead; refused with 409 VERSION_CONFLICT when the setting changed meanwhile."""
        return svc.set_approval_mode(p, facility_id, body.mode, body.expected_version, body.reason)

    @app.post("/console/docks/{dock_id}/blocks")
    def block(dock_id: str, b: Block, p: Principal = Depends(staff)):
        return {"occupancy_id": svc.block_dock(p, dock_id, b.event_id, b.start, b.end, b.type, b.reason)}

    @app.get("/console/facilities/{facility_id}/schedule-proposal")
    def schedule_proposal(facility_id: str = Path(min_length=1, max_length=40), rule: ScheduleRule = "FACILITY_SCORE",
                          p: Principal = Depends(staff)):
        """Read only: the facility scheduling tool's PROPOSAL as of now (not stored, not a booking)."""
        return svc.schedule_proposal(p, facility_id, rule)

    @app.post("/console/facilities/{facility_id}/schedule-proposals")
    def store_schedule_proposal(body: ScheduleStore, facility_id: str = Path(min_length=1, max_length=40),
                                p: Principal = Depends(staff)):
        """Compute and store a proposal (evidence with an expiry) so its moves can be applied one at a time."""
        return svc.store_schedule_proposal(p, facility_id, body.rule, body.ttl_s)

    @app.post("/console/appointments/{appointment_id}/move")
    def move_appointment(body: Move, appointment_id: str = Path(min_length=1, max_length=80),
                         p: Principal = Depends(staff)):
        """Apply one move of a stored proposal. Booking switch, role, facility and every booking rule are re-checked."""
        return svc.move_appointment(p, appointment_id, body.proposal_id, body.idempotency_key, body.note)

    @app.get("/console/shipments/{shipment_id}/recommendations")
    def recommendations(shipment_id: str = Path(min_length=3, max_length=80, pattern=SHIPMENT_ID_PATTERN),
                        earliest: AwareDatetime | None = None,
                        latest: AwareDatetime | None = None, days: int = Query(1, ge=1, le=3),
                        p: Principal = Depends(staff)):
        """Phase G1, read only: the optimizer's ranked dock/slot options for one truck, with every hard constraint
        evaluated and the rejected alternatives. A recommendation, never a booking."""
        if earliest and latest and latest <= earliest:
            raise HTTPException(422, "latest must be after earliest")
        return svc.recommend(p, shipment_id, earliest, latest, days)

    @app.get("/console/observability")
    def observability(facility: str | None = Query(None, max_length=40), hours: int = Query(24, ge=1, le=744),
                      driver: str | None = Query(None, max_length=48), p: Principal = Depends(staff)):
        """Phase G3, read only: operational, agent, optimizer, reliability and evaluation metrics in this person's
        facility scope. Every number is a persisted row or a measured timing; anything else is marked unavailable."""
        return svc.observability(p, facility, hours, driver)

    @app.get("/console/escalations")
    def escalations(p: Principal = Depends(staff)):
        return {"escalations": svc.list_escalations(p)}

    # ---------------------------------------------------------------- dispatcher (roadmap Phase 1/2, 0035)
    @app.get("/console/dispatch/shipments")
    def dispatch_shipments(facility_id: str | None = Query(None, max_length=40), p: Principal = Depends(staff)):
        return {"shipments": svc.list_dispatch_shipments(p, facility_id)}

    @app.get("/console/dispatch/shipments/{shipment_id}/options")
    def dispatch_options(shipment_id: str = Path(min_length=3, max_length=80, pattern=SHIPMENT_ID_PATTERN),
                         p: Principal = Depends(staff)):
        return svc.dispatch_options(p, shipment_id)

    @app.post("/console/dispatch/offers")
    def dispatch_offer(body: OfferIn, p: Principal = Depends(staff)):
        return svc.create_offer(p, body.shipment_id, body.driver_id, body.vehicle_id, body.dock_code, body.start,
                                body.idempotency_key, body.target)

    @app.get("/console/dispatch/offers")
    def dispatch_offers(facility_id: str | None = Query(None, max_length=40), limit: int = Query(50, ge=1, le=200),
                        p: Principal = Depends(staff)):
        return {"offers": svc.list_offers(p, facility_id, limit)}

    @app.post("/console/drivers/{driver_id}/telegram-link-code")
    def telegram_link_code(driver_id: str = Path(min_length=3, max_length=80), p: Principal = Depends(staff)):
        return svc.issue_link_code(p, driver_id)

    @app.post("/console/drivers/{driver_id}/telegram-link/revoke")
    def telegram_link_revoke(body: Revoke, driver_id: str = Path(min_length=3, max_length=80),
                             p: Principal = Depends(staff)):
        return svc.revoke_link(p, driver_id, body.reason)

    # ---------------------------------------------------------------- 0036: confirmed reassignment and cancellation
    @app.post("/console/dispatch/reassignments")
    def dispatch_reassign(body: ReassignIn, p: Principal = Depends(staff)):
        return svc.reassign_confirmed(p, body.shipment_id, body.driver_id, body.idempotency_key)

    @app.post("/console/appointments/{appointment_id}/cancellation-requests")
    def cancellation_request(body: CancelIn, appointment_id: str = Path(min_length=1, max_length=80),
                             p: Principal = Depends(staff)):
        return svc.request_cancellation(p, appointment_id, body.reason_code, body.note, body.idempotency_key)

    @app.get("/console/cancellation-requests")
    def cancellation_requests(facility_id: str | None = Query(None, max_length=40), p: Principal = Depends(staff)):
        return {"requests": svc.list_cancellations(p, facility_id)}

    @app.post("/console/cancellation-requests/{request_id}/decision")
    def cancellation_decision(body: CancelDecision, request_id: uuid.UUID, p: Principal = Depends(staff)):
        return svc.decide_cancellation(p, str(request_id), body.decision, body.note)

    # ---------------------------------------------------------------- planner schedule and operations health (read)
    @app.get("/console/facilities/{facility_id}/schedule")
    def facility_schedule(facility_id: str = Path(min_length=1, max_length=40), hours: int = Query(12, ge=1, le=48),
                          p: Principal = Depends(staff)):
        return svc.facility_schedule(p, facility_id, hours)

    @app.get("/console/ops/notifications")
    def ops_notifications(state: Literal["PENDING", "SENDING", "SENT", "DELIVERED", "RETRY", "FAILED", "UNKNOWN"] | None = None,
                          limit: int = Query(100, ge=1, le=200), p: Principal = Depends(staff)):
        return svc.ops_notifications(p, state, limit)

    @app.get("/console/ops/audit")
    def ops_audit(shipment_id: str | None = Query(None, max_length=80), limit: int = Query(100, ge=1, le=200),
                  p: Principal = Depends(staff)):
        return svc.ops_audit(p, shipment_id, limit)

    @app.get("/console/ops/health")
    def ops_health(p: Principal = Depends(staff)):
        return svc.ops_health(p)

    @app.post("/console/escalations/{escalation_id}/acknowledge")
    def escalation_ack(body: EscalationNote, escalation_id: uuid.UUID, p: Principal = Depends(staff)):
        return svc.acknowledge_escalation(p, str(escalation_id), body.note)

    @app.post("/console/escalations/{escalation_id}/resolve")
    def escalation_resolve(body: EscalationResolve, escalation_id: uuid.UUID, p: Principal = Depends(staff)):
        return svc.resolve_escalation(p, str(escalation_id), body.resolution)

    # ---------------------------------------------------------------- 0048: the appointment lifecycle (workstream A)
    @app.post("/console/appointments/{appointment_id}/arrival")
    def appointment_arrival(body: LifecycleStep, appointment_id: str = Path(min_length=1, max_length=80),
                            p: Principal = Depends(staff)):
        """D1: operations or ops_lead record the truck at the gate (only from CONFIRMED, until start + grace)."""
        return svc.record_arrival(p, appointment_id, body.note)

    @app.post("/console/appointments/{appointment_id}/dock")
    def appointment_dock(body: LifecycleStep, appointment_id: str = Path(min_length=1, max_length=80),
                         p: Principal = Depends(staff)):
        return svc.record_dock(p, appointment_id, body.note)

    @app.post("/console/appointments/{appointment_id}/completion")
    def appointment_completion(body: LifecycleStep, appointment_id: str = Path(min_length=1, max_length=80),
                               p: Principal = Depends(staff)):
        return svc.record_completion(p, appointment_id, body.note)

    @app.post("/console/appointments/{appointment_id}/no-show/rebook")
    def no_show_rebook(body: NoShowRebook, appointment_id: str = Path(min_length=1, max_length=80),
                       p: Principal = Depends(staff)):
        """D3: the dispatcher returns a no-show shipment to Needs booking (a planner approves the new booking)."""
        return svc.resolve_no_show(p, appointment_id, body.note)

    @app.get("/console/facilities/{facility_id}/arrivals")
    def arrivals(facility_id: str = Path(min_length=1, max_length=40), hours: int = Query(24, ge=1, le=48),
                 p: Principal = Depends(staff)):
        return svc.arrivals(p, facility_id, hours)

    @app.get("/console/holds")
    def holds(facility_id: str | None = Query(None, max_length=40), include_cleared: bool = False,
              p: Principal = Depends(staff)):
        """Issue 1: the open booking holds (ops_lead Controls tab)."""
        return {"holds": svc.list_holds(p, facility_id, include_cleared)}

    @app.get("/console/controls")
    def controls(p: Principal = Depends(staff)):
        """Issue 1: kill switch, automation pause and holds, with who changed each last (ops_lead only)."""
        return svc.controls(p)

    # ---------------------------------------------------------------- 0048: dispatch (workstream B)
    @app.get("/console/dispatch/shipments/{shipment_id}/free-drivers")
    def free_drivers(start: AwareDatetime, end: AwareDatetime,
                     shipment_id: str = Path(min_length=3, max_length=80, pattern=SHIPMENT_ID_PATTERN),
                     p: Principal = Depends(staff)):
        if end <= start:
            raise HTTPException(422, "end must be after start")
        return svc.free_drivers(p, shipment_id, start, end)

    @app.get("/console/appointments/{appointment_id}/reschedule-options")
    def reschedule_options(appointment_id: str = Path(min_length=1, max_length=80), p: Principal = Depends(staff)):
        return svc.reschedule_options(p, appointment_id)

    @app.post("/console/appointments/{appointment_id}/reschedule-requests")
    def reschedule_request(body: RescheduleIn, appointment_id: str = Path(min_length=1, max_length=80),
                           p: Principal = Depends(staff)):
        """Issue 6 / Q2: always a planner decision; the booking stays confirmed until it is approved."""
        return svc.request_reschedule(p, appointment_id, body.dock_code, body.start, body.idempotency_key, body.note)

    @app.get("/console/me")
    def me(p: Principal = Depends(staff)):
        """Read only: the roles, facilities and permitted actions the server derives from the verified token."""
        return svc.whoami(p)

    @app.get("/console/threads")
    def threads(limit: int = Query(25, ge=1, le=50), before: datetime | None = None, p: Principal = Depends(staff)):
        """Read only: conversations in this person's facilities, newest activity first (paged by `before`)."""
        return svc.list_threads(p, limit, before)

    @app.get("/console/threads/{thread_id}")
    def thread(thread_id: str = Path(min_length=1, max_length=80), p: Principal = Depends(staff)):
        """Read only: one conversation as stored (masked messages, facts, ETA, options, notifications)."""
        return svc.thread_detail(p, thread_id)

    @app.post("/console/threads/{thread_id}/takeover")
    def takeover(thread_id: str, body: Takeover, p: Principal = Depends(staff)):
        svc.takeover(p, thread_id, body.reason)
        return {"human_active": True}

    @app.post("/console/threads/{thread_id}/handback")
    def handback(thread_id: str, body: Handback, p: Principal = Depends(staff)):
        svc.handback(p, thread_id, body.summary)
        return {"human_active": False}

    return app
