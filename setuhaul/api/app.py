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
from .inbound import (InboundRelay, LocalQueue, SqsFifoQueue, booking_killed, check_booking_preconditions,
                      load_manifest, manifest_from_database, own_code_sha256)
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
                   deployment_id=os.environ.get("SETUHAUL_DEPLOYMENT_ID") or None,
                   deployment_manifest=os.environ.get("SETUHAUL_DEPLOYMENT_MANIFEST") or None,
                   manifest_source=os.environ.get("SETUHAUL_DEPLOYMENT_MANIFEST_SOURCE", "file"),
                   tools_route=(os.environ.get("SETUHAUL_TOOLS_ROUTE", "on").lower() not in ("off", "false", "0")
                                and os.environ.get("SETUHAUL_ENV") not in ("staging", "pilot")))


class ToolCall(BaseModel):
    model_config = {"extra": "forbid"}
    turn_id: int = Field(ge=0)
    args: dict = Field(default_factory=dict)


class Decision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["CONFIRM", "REJECT"]
    idempotency_key: str = Field(min_length=16, max_length=128)
    note: str | None = Field(default=None, max_length=500)


class KillSwitch(BaseModel):
    model_config = {"extra": "forbid"}
    engaged: bool
    reason: str = Field(min_length=5, max_length=300)


class Pause(BaseModel):
    model_config = {"extra": "forbid"}
    paused: bool
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
                         booking_enabled=settings.booking_enabled, optimizer_ranking=settings.optimizer_ranking)
    verifier = CognitoVerifier(settings.cognito_issuer, settings.cognito_client_ids, settings.jwks)
    sessions = SessionTokens(settings.session_secret)
    # Inbound events for the agent orchestrator (B-WF-1). In AWS this is SQS FIFO (inbound.SqsFifoQueue: message group
    # = driver, deduplication id = WhatsApp message id); locally an in-process LocalQueue. The session token travels
    # only on this internal queue and is never returned to the webhook caller.
    inbound = queue if queue is not None else (SqsFifoQueue(settings.queue_url) if settings.queue_url else LocalQueue())
    if settings.booking_enabled and not settings.local_test_harness:
        # S4-05: the switch fails closed. Durable FIFO queue with a dead-letter queue and a visibility timeout that
        # covers the processing lease, plus recorded evidence for every deployment gate, or the service does not start.
        if settings.manifest_source == "database" and settings.environment:
            manifest = manifest_from_database(db, settings.environment, own_code_sha256())
        else:
            manifest = load_manifest(settings.deployment_manifest) if settings.deployment_manifest else None
        app.state.gate_exceptions = check_booking_preconditions(db, inbound, settings.processing_lease_s,
                                                                settings.environment, manifest, settings.deployment_id)
    relay = InboundRelay(svc, inbound, sessions)
    app.state.service, app.state.sessions, app.state.inbound, app.state.relay = svc, sessions, inbound, relay

    @app.exception_handler(ServiceError)
    async def _service_error(_req, e: ServiceError):
        return JSONResponse({"error": e.code, "detail": e.detail}, status_code=e.http)

    @app.get("/health")
    def health():
        """Readiness for the load balancer and the pilot run sheet (Phase G). Public and read-only: it reports whether
        the database answers and how the booking switch is set, and nothing else (no DSN, key, queue URL or data)."""
        try:
            with db.tx("ingest", readonly=True) as c:              # a role the API workload has anyway (E3)
                c.execute("SELECT 1")
            database = "ok"
        except psycopg.Error:
            database = "unreachable"
        body = {"status": "ok" if database == "ok" else "degraded", "database": database,
                "booking_enabled": settings.booking_enabled, "environment": settings.environment, "version": app.version}
        if settings.booking_enabled and database == "ok":
            try:                                                    # 0026: what the database will refuse now
                body["booking_kill_switch"] = "ENGAGED" if booking_killed(db) else "disengaged"
            except Exception:                                       # noqa: BLE001 — reported, never fatal here
                body["booking_kill_switch"] = "unknown"
            body["gate_exceptions"] = sorted(getattr(app.state, "gate_exceptions", None) or {})
        return JSONResponse(body, status_code=200 if database == "ok" else 503)

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
        return svc.decide(p, appointment_id, d.decision, d.note, d.idempotency_key)

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

    @app.post("/console/facilities/{facility_id}/pause")
    def pause(facility_id: str, body: Pause, p: Principal = Depends(staff)):
        return {"automation_paused": svc.pause(p, facility_id, body.paused, body.reason)}

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
                      p: Principal = Depends(staff)):
        """Phase G3, read only: operational, agent, optimizer, reliability and evaluation metrics in this person's
        facility scope. Every number is a persisted row or a measured timing; anything else is marked unavailable."""
        return svc.observability(p, facility, hours)

    @app.get("/console/escalations")
    def escalations(p: Principal = Depends(staff)):
        return {"escalations": svc.list_escalations(p)}

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
