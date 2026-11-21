"""HTTP surface. Three audiences, three authentication mechanisms, no shared endpoints.

    /webhooks/whatsapp   Meta → us            X-Hub-Signature-256 (HMAC of the raw body)
    /tools/{tool}        agent orchestrator   X-Setuhaul-Session (driver session token) [+ IAM SigV4 at API Gateway]
    /console/...         planners, ops        Authorization: Bearer <Cognito access token> [+ API Gateway JWT authorizer]

Run locally:  uvicorn setuhaul.api.app:app  (settings from environment, see Settings.from_env)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .db import Db
from .inbound import InboundRelay, LocalQueue, SqsFifoQueue, check_booking_preconditions, load_manifest
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
    # Tests and the replay only: lets the booking path run on LocalQueue without deployment gates. from_env never
    # sets it, so a deployed service with the switch on always goes through check_booking_preconditions (S4-05).
    local_test_harness: bool = False

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
                   deployment_id=os.environ.get("SETUHAUL_DEPLOYMENT_ID") or None,
                   deployment_manifest=os.environ.get("SETUHAUL_DEPLOYMENT_MANIFEST") or None)


class ToolCall(BaseModel):
    model_config = {"extra": "forbid"}
    turn_id: int = Field(ge=0)
    args: dict = Field(default_factory=dict)


class Decision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["CONFIRM", "REJECT"]
    idempotency_key: str = Field(min_length=16, max_length=128)
    note: str | None = Field(default=None, max_length=500)


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


log = logging.getLogger("setuhaul.api")


def create_app(settings: Settings, clock: Callable[[], datetime] | None = None, queue=None) -> FastAPI:
    app = FastAPI(title="SetuHaul exception service", version="0.2.0", docs_url=None, redoc_url=None)
    db = Db(settings.dsn)
    svc = BookingService(db, clock or (lambda: datetime.now(timezone.utc)), settings.facilities,
                         booking_enabled=settings.booking_enabled)
    verifier = CognitoVerifier(settings.cognito_issuer, settings.cognito_client_ids, settings.jwks)
    sessions = SessionTokens(settings.session_secret)
    # Inbound events for the agent orchestrator (B-WF-1). In AWS this is SQS FIFO (inbound.SqsFifoQueue: message group
    # = driver, deduplication id = WhatsApp message id); locally an in-process LocalQueue. The session token travels
    # only on this internal queue and is never returned to the webhook caller.
    inbound = queue if queue is not None else (SqsFifoQueue(settings.queue_url) if settings.queue_url else LocalQueue())
    if settings.booking_enabled and not settings.local_test_harness:
        # S4-05: the switch fails closed. Durable FIFO queue with a dead-letter queue and a visibility timeout that
        # covers the processing lease, plus recorded evidence for every deployment gate, or the service does not start.
        manifest = load_manifest(settings.deployment_manifest) if settings.deployment_manifest else None
        check_booking_preconditions(db, inbound, settings.processing_lease_s, settings.environment, manifest,
                                    settings.deployment_id)
    relay = InboundRelay(svc, inbound, sessions)
    app.state.service, app.state.sessions, app.state.inbound, app.state.relay = svc, sessions, inbound, relay

    @app.exception_handler(ServiceError)
    async def _service_error(_req, e: ServiceError):
        return JSONResponse({"error": e.code, "detail": e.detail}, status_code=e.http)

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

    @app.post("/console/facilities/{facility_id}/pause")
    def pause(facility_id: str, body: Pause, p: Principal = Depends(staff)):
        return {"automation_paused": svc.pause(p, facility_id, body.paused, body.reason)}

    @app.post("/console/docks/{dock_id}/blocks")
    def block(dock_id: str, b: Block, p: Principal = Depends(staff)):
        return {"occupancy_id": svc.block_dock(p, dock_id, b.event_id, b.start, b.end, b.type, b.reason)}

    @app.get("/console/escalations")
    def escalations(p: Principal = Depends(staff)):
        return {"escalations": svc.list_escalations(p)}

    @app.post("/console/threads/{thread_id}/takeover")
    def takeover(thread_id: str, body: Takeover, p: Principal = Depends(staff)):
        svc.takeover(p, thread_id, body.reason)
        return {"human_active": True}

    @app.post("/console/threads/{thread_id}/handback")
    def handback(thread_id: str, body: Handback, p: Principal = Depends(staff)):
        svc.handback(p, thread_id, body.summary)
        return {"human_active": False}

    return app
