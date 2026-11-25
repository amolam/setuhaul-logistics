"""Booking service: the agent's tools and the console's actions, over the database functions.

Division of labour (Build invariant: the agent requests; the application and database decide):
  * Identity: who the agent acts for comes from the verified inbound path (session token). Tool arguments cannot
    name a driver or thread.
  * Feasibility: computed here under a read-only system role; options are PUBLISHED by the system role. The agent
    role cannot insert options.
  * Confirmation: the database writes the read-back message; the trusted inbound path classifies the driver's next
    stored message and writes an ATTESTATION. The agent role can only read attestations and pass their id on;
    hold_option() and submit_booking_request() verify them.
  * Provenance: ETA reports must cite a DRIVER message in this thread, for this thread's shipment.
  * People: once a person owns a thread, every agent tool refuses. Console actions follow PERMISSIONS below.
Responses use the tool envelope from `docs/2-design/04_tool_contracts_and_states.md` §1.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

import psycopg
from psycopg.types.json import Jsonb
from psycopg.types.range import Range
from pydantic import AwareDatetime, BaseModel, Field, ValidationError, model_validator

from ..adapters.pg_view import load_view
from ..domain.booking import BookingFlow, S, classify_confirmation, idem_key, map_db_error
from ..domain.feasibility import FeasibilityEngine, Option, hm
from ..domain.optimizer import (FEASIBLE, OPTIMAL, OptimizationRequest, OptimizationResult, OptimizerConfig,
                                SlotOptimizer)
from ..domain.scheduling import ENGINE_VERSION, NOT_A_BOOKING, RULES, Proposal, propose, snapshot_from_data
from ..observability import tracing
from ..ops import telemetry
from .db import CORRELATION_ID, TURN_FENCE, Db
from .security import Principal

log = logging.getLogger("setuhaul.service")
IST = timezone(timedelta(hours=5, minutes=30))


def option_ttl_s(env: dict | None = None) -> int:
    """How long a shown option or dispatcher offer stays acceptable (SETUHAUL_OPTION_TTL_S, default 300 s, 30..3600).
    Distinct from the dock-hold TTL (180 s) and the warehouse confirmation window (pending_expires_at)."""
    raw = (os.environ if env is None else env).get("SETUHAUL_OPTION_TTL_S", "300")
    v = int(raw)
    if not 30 <= v <= 3600:
        raise ValueError("SETUHAUL_OPTION_TTL_S must be between 30 and 3600 seconds")
    return v


OPTION_TTL = timedelta(seconds=option_ttl_s())
CLASSIFIER_VERSION = "lexicon-v1"
CENTRAL = "CENTRAL"

# Console role / action matrix (P1-7). The database enforces facility scope; this matrix enforces role.
PERMISSIONS: dict[str, frozenset] = {
    "list_requests": frozenset({"planner", "ops_lead", "operations"}),
    "decide":        frozenset({"operations", "planner", "ops_lead"}),   # which kinds each may decide: the database (B-WF-2 §8)
    "withdraw":      frozenset({"operations", "planner", "ops_lead"}),
    "booking_hold":  frozenset({"ops_lead"}),
    "policy_report": frozenset({"planner", "ops_lead"}),             # B-WF-2a override rates per rule
    "block_dock":    frozenset({"planner", "ops_lead"}),
    "pause":         frozenset({"ops_lead"}),
    "takeover":      frozenset({"operations", "planner", "ops_lead"}),
    "handback":      frozenset({"operations", "planner", "ops_lead"}),
}
# Facility scheduling tool (migration 0025), kept apart from the P1-7 matrix above, which tests/test_api.py pins.
SCHEDULE_PERMISSIONS: dict[str, frozenset] = {
    "schedule_proposal": frozenset({"planner", "ops_lead"}),         # view / store a facility proposal (no booking)
    # applying a move changes a time a driver was told: MOVE_CONFIRMED_PROMISE in approval_role_rules (B-WF-2 §8);
    # the database checks the same rule itself
    "move_appointment":  frozenset({"ops_lead"}),
}
# Option D (migration 0026): the booking kill switch. The database refuses every new booking commitment while it is
# engaged, whatever the deployment says; an ops_lead operates it from the console, the owner with ops/kill_switch.py.
KILL_SWITCH_PERMISSIONS: dict[str, frozenset] = {
    "booking_kill_switch": frozenset({"ops_lead"}),
    "view_kill_switch":    frozenset({"planner", "ops_lead", "operations"}),
}
# Phase G (migration 0027): read-only optimizer recommendations and the observability dashboard. Planner and ops_lead
# only: the operations role keeps exactly its Step 6 permission set (tests/test_console_me.py pins it). Facility scope
# is the database's (RLS) as for every console read.
PHASE_G_PERMISSIONS: dict[str, frozenset] = {
    "view_recommendations": frozenset({"planner", "ops_lead"}),
    "view_observability":   frozenset({"planner", "ops_lead"}),
}
# Roadmap Phase 1/5 (migration 0035): dispatcher actions. TEMPORARY ROLE MAPPING (owner, 25 Nov 2026): there is no
# dispatcher group yet, so `operations` (and ops_lead) act as dispatcher. Offers they send are decided as PLANNER_REVIEW
# in the database, so the role that sends an offer can never approve the booking it leads to. Review when a dispatcher
# Cognito group exists. Planners may read the dispatch board but not send offers.
DISPATCH_PERMISSIONS: dict[str, frozenset] = {
    "view_dispatch":     frozenset({"operations", "planner", "ops_lead"}),
    "send_offer":        frozenset({"operations", "ops_lead"}),
    "link_driver":       frozenset({"operations", "ops_lead"}),
}


def env(status: str = "OK", data: dict | None = None, missing: list | None = None, requires_human: dict | None = None,
        say_hint: str | None = None, schedule_version: int | None = None) -> dict:
    return {"status": status, "data": data or {}, "missing_fields": missing or [], "requires_human": requires_human,
            "say_hint": say_hint, "schedule_version": schedule_version}


def human_time(d: datetime) -> str:
    d = d.astimezone(IST)
    return f"{d.strftime('%I:%M %p').lstrip('0')} on {d.day} {d.strftime('%b')}"


class ServiceError(Exception):
    def __init__(self, http: int, code: str, detail: str = ""):
        super().__init__(code)
        self.http, self.code, self.detail = http, code, detail


# ---------------------------------------------------------------- tool argument schemas (strict, timezone-aware)
class _Strict(BaseModel):
    model_config = {"extra": "forbid"}


def _ordered(es, lf):
    if es and lf and es >= lf:
        raise ValueError("earliest_start must be before latest_finish")


class DriverReport(_Strict):
    """What the extraction model (Haiku, forced tool) produced from the driver's message. Validated here again."""
    message_id: int
    shipment_id: str | None = None
    declared_eta: AwareDatetime | None = None
    eta_precision: Literal["PRECISE", "VAGUE"] | None = None
    delay_reason: Literal["TRAFFIC", "BREAKDOWN", "ACCIDENT", "LOADING_DELAY", "DOCUMENTS", "WEATHER", "REST", "OTHER"] | None = None
    safety_concern: bool = False
    earliest_start: AwareDatetime | None = None
    latest_finish: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self):
        _ordered(self.earliest_start, self.latest_finish)
        return self


class FindArgs(_Strict):
    shipment_id: str | None = None
    earliest_start: AwareDatetime | None = None
    latest_finish: AwareDatetime | None = None
    max_options: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def _check(self):
        _ordered(self.earliest_start, self.latest_finish)
        return self


class OptionArg(_Strict):
    option_id: uuid.UUID


class ConfirmArgs(_Strict):
    message_id: int


class StatusArgs(_Strict):
    shipment_id: str | None = None


class EscalateArgs(_Strict):
    reason_code: Literal["NO_FEASIBLE_SLOT", "SAFETY", "CONTRADICTORY_DATA", "CLARIFICATION_LIMIT", "DRIVER_REQUEST",
                         "OUT_OF_SCOPE", "AGENT_FAILURE", "BREAKDOWN"]          # BREAKDOWN: OD-2, migration 0009
    mode: Literal["DECIDE", "TAKE_OVER"] = "DECIDE"
    driver_last_message: str = Field(max_length=500)
    what_driver_was_told: str = Field(max_length=500)


class ScheduleArgs(_Strict):
    """Structured only: which rule. The facility is the conversation's (the driver's shipment), never an argument."""
    rule: Literal["FACILITY_SCORE", "FCFS", "EARLIEST_ETA"] = "FACILITY_SCORE"


# Booking switch (SETUHAUL_BOOKING_ENABLED, off by default): these change dock bookings and are refused while it is off.
# move_appointment is a console route, never an agent tool; it is listed so one switch covers every booking change.
BOOKING_TOOLS = {"hold_option", "submit_booking_request", "move_appointment"}
TOOLS = {"propose_facility_schedule": ScheduleArgs,
         "get_driver_context": None,"record_driver_report": DriverReport, "find_feasible_options": FindArgs,
         "select_option": OptionArg, "confirm_selection": ConfirmArgs, "hold_option": OptionArg,
         "submit_booking_request": OptionArg, "release_hold": OptionArg, "get_booking_status": StatusArgs,
         "escalate_to_human": EscalateArgs}


class BookingService:
    def __init__(self, db: Db, clock: Callable[[], datetime], facilities: tuple[str, ...], hold_ttl_s: int = 180,
                 booking_enabled: bool = False, optimizer_ranking: bool = False,
                 optimizer_config: OptimizerConfig | None = None, option_ttl_s: int | None = None):
        self.db, self.clock, self.facilities, self.hold_ttl = db, clock, tuple(facilities), hold_ttl_s
        # shown options and dispatcher offers (SETUHAUL_OPTION_TTL_S); the dock hold keeps its own hold_ttl_s
        self.option_ttl = timedelta(seconds=option_ttl_s) if option_ttl_s is not None else OPTION_TTL
        # Phase G1: rank the driver's options with the slot optimizer (labour, capacity and the soft objectives).
        # OFF by default: which slots a driver is offered first is booking policy, switched on by the owner per
        # environment (SETUHAUL_OPTIMIZER_RANKING=on). The options stay the feasibility engine's either way.
        self.optimizer_ranking = optimizer_ranking
        self.optimizer_config = optimizer_config or OptimizerConfig.from_assumptions()
        # Production booking switch (review R0.8): OFF by default. Until durable inbound delivery (step 4) is in
        # place, the agent may understand, find options and read back, but not hold or request a slot.
        self.booking_enabled = booking_enabled

    def now(self) -> datetime:
        return self.clock().astimezone(IST)

    # ---------------------------------------------------------------- shared helpers
    def _has_login(self, role: str) -> bool:
        """psycopg (local, tests): every role; the Data API backend: only the logins this workload was given."""
        have = getattr(self.db, "secret_arns", None)
        return have is None or role in have

    def _engine(self, scope: tuple[str, ...] | None = None) -> FeasibilityEngine:
        """Feasibility role, read-only transaction. Facility-wide facts the driver must never see stay inside the service."""
        with self.db.tx("feasibility", scope or self.facilities, readonly=True) as c:
            return FeasibilityEngine(load_view(c, self.now()))

    def _thread(self, c, p: Principal) -> dict:
        r = c.execute("SELECT thread_id, driver_id, shipment_id, human_active, status FROM chat_threads WHERE thread_id=%s",
                      (p.thread_id,)).fetchone()
        if not r or r[1] != p.driver_id:
            raise ServiceError(403, "FORBIDDEN")
        return dict(zip(("thread_id", "driver_id", "shipment_id", "human_active", "status"), r))

    def _flow(self, c, p: Principal, thread: dict) -> tuple[BookingFlow, int]:
        row = c.execute("""SELECT state, selected_option, yes_message_id, hold_option, appointment_id, version, attestation_id
                             FROM thread_flow WHERE thread_id=%s""", (p.thread_id,)).fetchone()
        if not row:
            c.execute("INSERT INTO thread_flow (thread_id) VALUES (%s) ON CONFLICT DO NOTHING", (p.thread_id,))
            row = ("NO_OPTIONS", None, None, None, None, 1, None)
        f = BookingFlow(p.thread_id, S(row[0]), selected=str(row[1]) if row[1] else None, yes_message_id=row[2],
                        hold_option=str(row[3]) if row[3] else None, appointment_id=row[4],
                        attestation_id=str(row[6]) if row[6] else None)
        last = c.execute("SELECT max(created_at) FROM shown_options WHERE thread_id=%s", (p.thread_id,)).fetchone()[0]
        if last:
            # lower()/upper(), not the range itself: the RDS Data API cannot return range types (seen on AWS,
            # 22 Nov 2026: UnsupportedResultException, so every option list failed and the driver was told "no slot")
            for oid, dock, start, end, appr, exp in c.execute(
                    """SELECT o.option_id, d.dock_code, lower(o.during), upper(o.during), o.needs_approval, o.expires_at
                         FROM shown_options o JOIN docks d USING (dock_id)
                        WHERE o.thread_id=%s AND o.created_at=%s ORDER BY o.rank""", (p.thread_id, last)):
                f.shown[str(oid)] = {"dock_code": dock, "start": start, "end": end,
                                     "needs_approval": appr, "expires_at": exp}
        version = row[5]
        if f.state is S.SUBMITTED and f.appointment_id:
            # Human test H-3: the warehouse (or expiry, or a breakdown) decides OUTSIDE the conversation. Read the
            # database's answer here, so a decided request never leaves the conversation stuck in SUBMITTED:
            # CONFIRMED → BOOKED, anything else that is no longer pending → CLOSED. Both may look for slots again.
            st = c.execute("SELECT status FROM appointments WHERE appointment_id=%s", (f.appointment_id,)).fetchone()
            if st and st[0] != "PENDING_CONFIRMATION":
                f.warehouse_decided(st[0])
                if c.execute("""UPDATE thread_flow SET state=%s, version=version+1, updated_at=now()
                                 WHERE thread_id=%s AND version=%s""", (f.state.value, p.thread_id, version)).rowcount:
                    version += 1
        if thread["human_active"]:
            f.state = S.HUMAN
        return f, version

    def _save(self, c, f: BookingFlow, version: int):
        if f.state is S.HUMAN:
            return
        n = c.execute("""UPDATE thread_flow SET state=%s, selected_option=%s, yes_message_id=%s, hold_option=%s,
                                appointment_id=%s, attestation_id=%s, version=version+1, updated_at=now()
                          WHERE thread_id=%s AND version=%s""",
                      (f.state.value, f.selected, f.yes_message_id, f.hold_option, f.appointment_id, f.attestation_id,
                       f.thread_id, version)).rowcount
        if n != 1:
            raise ServiceError(409, "CONCURRENT_TURN", "another turn changed this conversation; retry")

    def _shipment(self, p: Principal, requested: str | None) -> tuple[str | None, str | None, list]:
        """Resolve the conversation's shipment. B2-17: reading the thread, choosing the shipment (requested, else bound,
        else the driver's only live one) and binding it happen in ONE database function under the conversation lock
        (resolve_thread_shipment), which returns the authoritative shipment. B2-16: the function itself checks that the
        conversation and the shipment belong to this driver session."""
        with self.db.tx("agent", driver=p.driver_id, readonly=True) as c:
            self._thread(c, p)
            # every facility the driver's shipments go to: the bind checks the previous shipment's facility too
            scope = tuple(r[0] for r in c.execute("SELECT DISTINCT destination_facility_id FROM shipments"))
        try:
            with self.db.tx("agent", scope, p.driver_id) as c:
                r = c.execute("SELECT resolve_thread_shipment(%s,%s,%s)", (p.thread_id, requested, p.name)).fetchone()[0]
        except psycopg.Error as e:
            code = (e.diag.message_primary or "").split(":")[0]
            raise ServiceError({"FINISH_CURRENT_BOOKING_FIRST": 409, "HUMAN_ACTIVE": 409}.get(code, 403),
                               code or "FORBIDDEN", "a hold or request is in progress for the other shipment"
                               if code == "FINISH_CURRENT_BOOKING_FIRST" else "not your shipment"
                               if code == "FORBIDDEN" else "") from e
        return r["shipment_id"], r["facility_id"], [tuple(x) for x in r["live"]]

    def _human_active(self, p: Principal) -> bool:
        with self.db.tx("agent", driver=p.driver_id, readonly=True) as c:
            return self._thread(c, p)["human_active"]

    def _escalate(self, p: Principal, fac: str | None, reason: str, mode: str, last: str, told: str, key: str) -> dict:
        with self.db.tx("agent", (fac,) if fac else (), p.driver_id) as c:
            return c.execute("SELECT escalate_thread(%s,%s,%s,%s,%s,%s,%s)",
                             (p.thread_id, reason, mode, last, told, key, p.name)).fetchone()[0]

    # ---------------------------------------------------------------- agent tools
    def call_tool(self, p: Principal, tool: str, turn_id: int, raw_args: dict) -> dict:
        if tool not in TOOLS:
            raise ServiceError(404, "TOOL_NOT_AVAILABLE_TO_AGENT")
        if tool in BOOKING_TOOLS and not self.booking_enabled:
            return env("REQUIRES_HUMAN", {"code": "BOOKING_DISABLED"}, requires_human={"reason_code": "BOOKING_DISABLED"},
                       say_hint="Tell the driver a coordinator will confirm the slot.")
        if self._human_active(p):                                     # P0-3: a person owns the thread
            return env("REQUIRES_HUMAN", {"code": "HUMAN_ACTIVE"}, requires_human={"reason_code": "HUMAN_ACTIVE"},
                       say_hint="Do not reply. A coordinator is handling this conversation.")
        model = TOOLS[tool]
        try:
            args = model(**raw_args) if model else None
        except ValidationError as e:
            errs = e.errors(include_url=False, include_context=False)
            return env("NEEDS_INFO", {"errors": [{"loc": list(x["loc"]), "msg": x["msg"]} for x in errs]},
                       missing=sorted({str(x["loc"][0]) for x in errs if x["loc"]}) or ["arguments"])
        key = idem_key(p.thread_id, tool, args.model_dump(mode="json") if args else {}, turn_id)
        try:
            return getattr(self, f"t_{tool}")(p, args, key)
        except psycopg.Error as e:
            msg = e.diag.message_primary or str(e)
            status = map_db_error(e.sqlstate, msg)
            rh = {"reason_code": "AUTOMATION_PAUSED" if "PAUSED" in msg else msg.split(":")[0]} if status == "REQUIRES_HUMAN" else None
            return env(status, {"code": msg.split(":")[0]}, requires_human=rh)

    def t_get_driver_context(self, p, _args, _key):
        with self.db.tx("agent", driver=p.driver_id, readonly=True) as c:
            th = self._thread(c, p)
            ships = c.execute("""SELECT shipment_id, order_reference, current_status, destination_facility_id, priority_code,
                                        original_eta_ts FROM shipments
                                  WHERE current_status NOT IN ('COMPLETED','CANCELLED') ORDER BY shipment_id""").fetchall()
            ts_row = c.execute("SELECT current_status, order_reference FROM shipments WHERE shipment_id=%s",
                               (th["shipment_id"],)).fetchone() if th["shipment_id"] else None
            # the driver's last few messages, so a short reply ("yes", "2") is answered in the conversation's language
            recent = [(r[0], r[1]) for r in c.execute("""SELECT message_id, body_masked FROM chat_messages
                       WHERE thread_id=%s AND sender_type='DRIVER' ORDER BY message_id DESC LIMIT 6""",
                       (p.thread_id,)).fetchall()][::-1]
            cancelled = [r[0] for r in c.execute("""SELECT order_reference FROM shipments WHERE current_status = 'CANCELLED'
                                                   ORDER BY shipment_id""").fetchall()]
            # H-5: what the coordinator settled on the call, as background for 12 hours (never a tool argument)
            hb = c.execute("""SELECT handback_note, handback_at FROM chat_threads WHERE thread_id=%s
                               AND handback_at > now() - interval '12 hours'""", (p.thread_id,)).fetchone()
        facs = tuple(sorted({r[3] for r in ships}))
        appts, flow = {}, {"state": "NO_OPTIONS", "selected_option": None, "options": []}
        with self.db.tx("agent", facs, p.driver_id, readonly=True) as c:   # appointments are facility-scoped (RLS)
            for sid, appt, st, start, dock in c.execute(
                    """SELECT a.shipment_id, a.appointment_id, a.status, lower(a.during), d.dock_code
                         FROM appointments a JOIN docks d USING (dock_id)
                        WHERE a.shipment_id = ANY(%s) AND a.is_current
                          AND a.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')""", ([r[0] for r in ships],)):
                appts[sid] = {"appointment_id": appt, "status": st, "dock_code": dock, "start": human_time(start)}
            row = c.execute("SELECT state, selected_option FROM thread_flow WHERE thread_id=%s", (p.thread_id,)).fetchone()
            if row:
                flow["state"], flow["selected_option"] = row[0], str(row[1]) if row[1] else None
            if th["human_active"]:
                flow["state"] = "HUMAN"
            tf = c.execute("""SELECT facts, asked FROM thread_facts
                               WHERE thread_id=%s AND updated_at > now() - interval '2 hours'""", (p.thread_id,)).fetchone()
            last = c.execute("SELECT max(created_at) FROM shown_options WHERE thread_id=%s", (p.thread_id,)).fetchone()[0]
            if last:
                flow["options"] = [{"option_id": str(o), "dock_code": dc, "start": human_time(lo), "end": human_time(hi),
                                    "start_ts": lo.isoformat(), "needs_approval": na}
                                   for o, dc, lo, hi, na in c.execute(        # lower()/upper(): see _flow
                                       """SELECT o.option_id, k.dock_code, lower(o.during), upper(o.during), o.needs_approval
                                            FROM shown_options o JOIN docks k USING (dock_id)
                                           WHERE o.thread_id=%s AND o.created_at=%s AND o.expires_at > now() ORDER BY o.rank""",
                                       (p.thread_id, last))]
        live = [{"shipment_id": r[0], "order_reference": r[1], "status": r[2], "facility_id": r[3], "priority": r[4],
                 "planned_eta": r[5].isoformat(), "current_appointment": appts.get(r[0])} for r in ships]
        return env("OK", {"driver_id": p.driver_id, "thread_shipment_id": th["shipment_id"], "live_shipments": live,
                          "needs_shipment_choice": len(live) > 1 and th["shipment_id"] is None, "flow": flow,
                          "turn_facts": {"facts": tf[0], "asked": tf[1]} if tf else None,
                          "thread_shipment": {"status": ts_row[0], "order_reference": ts_row[1]} if ts_row else None,
                          "cancelled_shipments": cancelled, "recent_driver_messages": recent,
                          "coordinator_note": {"note": hb[0], "at": human_time(hb[1])} if hb else None})

    # ---------------------------------------------------------------- orchestrator helpers (not agent tools)
    def read_driver_message(self, p: Principal, message_id: int) -> str | None:
        """The stored DRIVER text for a message in the caller's own thread (masked body)."""
        with self.db.tx("agent", driver=p.driver_id, readonly=True) as c:
            self._thread(c, p)
            r = c.execute("""SELECT body_masked FROM chat_messages
                              WHERE message_id=%s AND thread_id=%s AND sender_type='DRIVER'""", (message_id, p.thread_id)).fetchone()
        return r[0] if r else None

    def note_turn_facts(self, p: Principal, message_id: int, facts: dict | None, asked: str | None = None) -> None:
        """Remember (or, with facts=None, forget) what the driver said in earlier turns. The database function checks
        ownership, the allowed keys and the size; the agent role cannot write the table itself (migration 0008)."""
        with self.db.tx("agent", driver=p.driver_id) as c:
            c.execute("SELECT note_turn_facts(%s,%s,%s,%s,%s)",
                      (p.thread_id, message_id, Jsonb(facts or {}), asked, facts is None))

    def record_reply(self, p: Principal, text: str, reply_to: int | None = None, generation: int | None = None) -> int:
        """Store the agent's reply as an AGENT message (outbound delivery is the step 4 relay's job). S4-08: with
        reply_to, at most one reply per inbound message and generation; a re-run turn gets the stored reply's id."""
        with self.db.tx("agent", driver=p.driver_id) as c:
            self._thread(c, p)
            r = c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,content_hash,body_masked,
                                                        reply_to,reply_generation)
                             VALUES (%s,'AGENT','WHATSAPP',%s,%s,%s,%s)
                             ON CONFLICT (reply_to, reply_generation) WHERE sender_type='AGENT' AND reply_to IS NOT NULL
                             DO NOTHING RETURNING message_id""",
                          (p.thread_id, hashlib.sha256(text[:2000].encode()).hexdigest(), text[:2000], reply_to,
                           generation if reply_to is not None else None)).fetchone()
            if r:
                return r[0]
            return c.execute("""SELECT message_id FROM chat_messages WHERE sender_type='AGENT' AND reply_to=%s
                                  AND reply_generation=%s""", (reply_to, generation)).fetchone()[0]

    def t_record_driver_report(self, p, a: DriverReport, key):
        safety = a.safety_concern or a.delay_reason == "ACCIDENT"
        sid, fac, live = self._shipment(p, a.shipment_id)
        if sid is not None and (a.declared_eta or a.delay_reason):
            with self.db.tx("agent", (fac,), p.driver_id) as c:          # P0-2: provenance checked in the database
                c.execute("SELECT record_driver_eta(%s,%s,%s,%s,%s,%s,%s,%s)",
                          (p.thread_id, a.message_id, sid, a.declared_eta, a.eta_precision, a.delay_reason, key, p.name))
        if safety:                                                         # P0-3: durable hand-off, not just a reply
            esc = self._escalate(p, fac, "SAFETY", "TAKE_OVER", f"(driver message {a.message_id})",
                                 "A coordinator will call you now.", key)
            return env("REQUIRES_HUMAN", {"shipment_id": sid, "escalation_id": esc["escalation_id"], "queue": esc["queue"]},
                       requires_human={"reason_code": "SAFETY"},
                       say_hint="Tell the driver a coordinator will call them now. Do not discuss slots.")
        if sid is None:
            return env("NEEDS_INFO", {"live_shipments": [r[0] for r in live]}, missing=["shipment_id"],
                       say_hint="Ask which shipment (order reference) the driver is talking about.")
        if not a.declared_eta:
            return env("NEEDS_INFO", {"shipment_id": sid}, missing=["declared_eta"],
                       say_hint="Ask roughly what time they expect to reach the gate.")
        r = self._engine().find_options(sid, self.now(), max_options=1)
        e = r.eta
        return env("OK", {"shipment_id": sid, "effective_eta": human_time(e.eta), "eta_source": e.source,
                          "eta_confidence": e.confidence, "flags": list(e.flags),
                          "appointment_at_risk": r.facts["appointment_at_risk"],
                          "current_appointment": r.facts["current_appointment"]},
                   say_hint=("Our latest location suggests a later arrival; confirm the time with the driver."
                             if "OPTIMISTIC_DECLARATION" in e.flags else None))

    def _options(self, p, sid, fac, es, lf, n) -> dict:
        with self.db.tx("agent", (fac,), p.driver_id) as c:              # may create the flow row on first use
            f, _ = self._flow(c, p, self._thread(c, p))
            ok = f.allow("find_feasible_options")
            if not ok.allowed:
                return env("REQUIRES_HUMAN" if ok.reason == "HUMAN_ACTIVE" else "ERROR", {"code": ok.reason})
        if self.optimizer_ranking:
            now = self.now()
            with self.db.tx("feasibility", self.facilities, readonly=True) as c:
                data = load_view(c, now)
                sv = c.execute("SELECT schedule_version FROM facilities WHERE facility_id=%s", (fac,)).fetchone()
            eng = FeasibilityEngine(data)
            res = eng.find_options(sid, now, earliest_start=es, latest_finish=lf, max_options=n)
            if res.outcome_code in ("OPTIONS", "NEEDS_APPROVAL", "NO_FEASIBLE_SLOT"):
                res = self._rank_with_optimizer(data, eng, res, sid, fac, es, lf, n, now, sv[0] if sv else None)
        else:
            res = self._engine().find_options(sid, self.now(), earliest_start=es, latest_finish=lf, max_options=n)
        if res.outcome_code == "CONTRADICTORY_DATA":
            return env("REQUIRES_HUMAN", {"shipment_id": sid}, requires_human={"reason_code": "CONTRADICTORY_DATA",
                       "detail": "driver or truck is assigned to an overlapping shipment"},
                       say_hint="Say a coordinator will confirm the slot; do not offer times.")
        if res.outcome_code in ("NOT_ELIGIBLE", "FIXED"):
            return env("NOT_FEASIBLE", {"reason": res.outcome})
        if not res.options:
            return env("NOT_FEASIBLE", {"reason": "no feasible slot", "rejections": res.rejections},
                       requires_human={"reason_code": "NO_FEASIBLE_SLOT"})
        try:
            out, shown, sv = self._publish(p, sid, fac, res)
        except psycopg.Error as e:
            if (e.diag.message_primary or "").startswith("STALE_SHIPMENT"):    # B2-17: the conversation moved on
                return env("ERROR", {"code": "STALE_SHIPMENT", "shipment_id": sid},
                           say_hint="The conversation moved to another shipment; ask which one the driver means.")
            raise
        with self.db.tx("agent", (fac,), p.driver_id) as c:
            th = self._thread(c, p)
            if th["shipment_id"] != sid:                                   # B2-17: re-check before showing them
                return env("ERROR", {"code": "STALE_SHIPMENT", "shipment_id": sid},
                           say_hint="The conversation moved to another shipment; ask which one the driver means.")
            f, ver = self._flow(c, p, th)
            f.options_shown(shown)
            self._save(c, f, ver)
        return env("OK", {"shipment_id": sid, "options": out}, schedule_version=sv,
                   say_hint="List the numbered options exactly as given; ask the driver to choose one.")

    def _rank_with_optimizer(self, data, eng, res, sid, fac, es, lf, n, now, sv):
        """G1 in the driver path: the optimizer re-proves every candidate (HC01..HC12, incl. labour and capacity) and
        ranks the survivors. A failing optimizer never blocks the driver: the engine's own options stand (fallback)."""
        cfg = OptimizerConfig(**{**self.optimizer_config.__dict__, "max_options": n})
        with tracing.span("optimizer", "chain", inputs={"facility_id": fac, "earliest_start": es, "latest_finish": lf,
                                                        "max_options": n}) as so:
            opt = SlotOptimizer(data, cfg, engine=eng).optimize(
                OptimizationRequest(sid, fac, es, lf, request_id=CORRELATION_ID.get() or str(uuid.uuid4()),
                                    data_as_of=now, schedule_version=sv), now)
            tracing.safe(lambda: so.end({"solver_status": opt.solver_status, "outcome_code": opt.outcome_code,
                                  "options": len(opt.options), "rejected": len(opt.rejected),
                                  "candidates_evaluated": opt.candidates_evaluated, "duration_ms": opt.duration_ms,
                                  "hard_constraints": len(getattr(opt, "constraints_evaluated", {}) or {})}))
        fallback = opt.solver_status not in (OPTIMAL, FEASIBLE, "INFEASIBLE")
        self._record_run(opt, "agent", "feasibility", (fac,), fallback=fallback)
        if fallback:                                             # ERROR / UNKNOWN / STALE: keep the engine's options
            return res
        res.options = [o.option() for o in opt.options]
        if not res.options:
            res.outcome, res.outcome_code = "ESCALATE (NO_FEASIBLE_SLOT)", "NO_FEASIBLE_SLOT"
        elif all(o.needs_approval for o in res.options):
            res.outcome, res.outcome_code = "OPTIONS NEED APPROVAL (after last new start time)", "NEEDS_APPROVAL"
        else:
            res.outcome, res.outcome_code = f"{len(res.options)} feasible option(s)", "OPTIONS"
        return res

    def _record_run(self, opt: OptimizationResult, caller: str, role: str, scope: tuple[str, ...],
                    fallback: bool = False) -> None:
        """Persist the run summary (0027) and log it. Best effort: telemetry never fails a driver turn or a request."""
        top = opt.options[0] if opt.options else None
        telemetry.emit("optimizer.run", opt.request_id, caller=caller, facility_id=opt.facility_id,
                       shipment_id=opt.shipment_id, solver_status=opt.solver_status, outcome_code=opt.outcome_code,
                       options=len(opt.options), rejected=len(opt.rejected), candidates=opt.candidates_evaluated,
                       duration_ms=opt.duration_ms, fallback=fallback)
        if opt.facility_id is None or not self._has_login(role):
            return
        body = {"request_id": opt.request_id[:80], "facility_id": opt.facility_id, "shipment_id": opt.shipment_id,
                "caller": caller, "engine": opt.engine_version, "solver_status": opt.solver_status,
                "outcome_code": opt.outcome_code or "NONE", "recommended_action": opt.recommended_action,
                "options_count": len(opt.options), "rejected_count": len(opt.rejected),
                "candidates_evaluated": opt.candidates_evaluated, "duration_ms": opt.duration_ms,
                "input_fingerprint": opt.input_fingerprint or None, "schedule_version": opt.schedule_version,
                "data_as_of": opt.data_as_of, "top_dock_id": top.dock_id if top else None,
                "top_start": top.start.isoformat() if top else None, "top_score": top.score if top else None,
                "warnings": len(opt.warnings), "fallback_used": fallback}
        token = TURN_FENCE.set(None)                             # evidence of a computation, not a turn's write
        try:
            with self.db.tx(role, scope) as c:
                c.execute("SELECT record_optimization_run(%s)", (Jsonb(body),))
        except Exception:                                        # noqa: BLE001 — logged, never fatal
            log.warning("optimizer run not recorded (%s)", opt.request_id)
        finally:
            TURN_FENCE.reset(token)

    def record_turn_metrics(self, res) -> None:
        """Phase G3: one row per finished turn (tool names, statuses, latencies; no driver data). Best effort."""
        tools = [{"tool": t["tool"], "status": t["status"], "ms": t["ms"]} for t in res.tool_calls][:20]
        model_ms = sum(getattr(t, "latency_ms", 0) or 0 for t in res.traces) or None
        turn_ref = getattr(res, "turn_ref", None) or res.correlation_id
        telemetry.emit("agent.turn", res.correlation_id, turn_ref=turn_ref,
                       ingest_correlation_id=getattr(res, "ingest_correlation_id", None), facility_id=res.facility_id,
                       extractor=res.extractor, fallback_reason=res.fallback_reason,
                       fallback_detail=getattr(res, "fallback_detail", None), turn_ms=res.turn_ms, model_ms=model_ms,
                       tools=[f"{t['tool']}:{t['status']}" for t in tools], escalated=bool(res.escalation_id),
                       path=res.path[:10])
        if not self._has_login("feasibility"):
            return
        ext = res.extractor if res.extractor in ("model", "rules-fallback", "rules") else "none"
        body = {"turn_ref": turn_ref, "facility_id": res.facility_id, "extractor": ext,
                "fallback_reason": str(res.fallback_reason)[:60] if res.fallback_reason else None, "tools": tools,
                "turn_ms": res.turn_ms, "model_ms": model_ms, "escalated": bool(res.escalation_id)}
        try:
            with self.db.tx("feasibility", self.facilities) as c:
                c.execute("SELECT record_agent_turn(%s)", (Jsonb(body),))
        except Exception:                                        # noqa: BLE001
            log.warning("turn metrics not recorded (%s)", res.correlation_id)

    def _publish(self, p, sid, fac, res):
        out, shown = [], {}
        with self.db.tx("feasibility", (fac,)) as c:                    # options are the service's decision
            sv = c.execute("SELECT schedule_version FROM facilities WHERE facility_id=%s", (fac,)).fetchone()[0]
            docks = dict(c.execute("SELECT dock_code, dock_id FROM docks WHERE facility_id=%s", (fac,)).fetchall())
            for rank, o in enumerate(res.options, 1):
                oid = uuid.uuid4()
                c.execute("INSERT INTO shown_options VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()+%s,now())",
                          (oid, p.thread_id, sid, docks[o.dock], Range(o.start, o.end, "[)"), o.needs_approval, rank, sv, self.option_ttl))
                out.append({"option_id": str(oid), "dock_code": o.dock, "start": human_time(o.start),
                            "end": human_time(o.end), "needs_approval": o.needs_approval})
                shown[str(oid)] = {"dock_code": o.dock, "start": o.start, "end": o.end, "needs_approval": o.needs_approval}
        return out, shown, sv

    def t_find_feasible_options(self, p, a: FindArgs, key):
        sid, fac, live = self._shipment(p, a.shipment_id)
        if sid is None:
            return env("NEEDS_INFO", {"live_shipments": [r[0] for r in live]}, missing=["shipment_id"])
        return self._options(p, sid, fac, a.earliest_start, a.latest_finish, a.max_options)

    def _driver_tx(self, p):
        sid, fac, _ = self._shipment(p, None)
        if sid is None:
            raise ServiceError(409, "NO_SHIPMENT_IN_THREAD")
        return sid, fac

    def t_select_option(self, p, a: OptionArg, key):
        sid, fac = self._driver_tx(p)
        with self.db.tx("agent", (fac,), p.driver_id) as c:
            f, ver = self._flow(c, p, self._thread(c, p))
            ok = f.allow("select_option", {"option_id": str(a.option_id)})
            if not ok.allowed:
                return env("ERROR", {"code": ok.reason})
            o = f.shown[str(a.option_id)]
            if o["expires_at"] < datetime.now(timezone.utc):
                return env("OPTION_EXPIRED", {"code": "OPTION_EXPIRED"}, say_hint="Availability changed; fetch new options.")
            text = f"You selected dock {o['dock_code']} at {human_time(o['start'])}. Shall I request it? Reply YES to confirm."
            rb = c.execute("SELECT record_read_back(%s,%s,%s,%s)", (a.option_id, p.thread_id, text, p.name)).fetchone()[0]
            f.option_selected(str(a.option_id))
            self._save(c, f, ver)
        return env("OK", {"option_id": str(a.option_id), "dock_code": o["dock_code"], "start": human_time(o["start"]),
                          "read_back": text, "read_back_message_id": rb["read_back_message_id"]},
                   say_hint="The read-back has been recorded; send it and wait for the driver's reply.")

    def t_confirm_selection(self, p, a: ConfirmArgs, key):
        """Reads the attestation the trusted inbound path wrote for this message. The agent cannot create one."""
        sid, fac = self._driver_tx(p)
        with self.db.tx("agent", (fac,), p.driver_id) as c:
            f, ver = self._flow(c, p, self._thread(c, p))
            row = c.execute("""SELECT a.attestation_id, a.verdict, a.option_id FROM confirmation_attestations a
                                WHERE a.thread_id=%s AND a.confirmation_message_id=%s AND a.expires_at > now()
                                  AND a.selection_id = (SELECT max(selection_id) FROM option_selections WHERE thread_id=%s)
                                  AND a.confirmation_message_id = (SELECT s.latest_reply_message_id FROM option_selections s
                                                                    WHERE s.selection_id = a.selection_id)""",
                            (p.thread_id, a.message_id, p.thread_id)).fetchone()
            if not row:
                return env("NEEDS_INFO", {"code": "NO_DRIVER_CONFIRMATION"}, missing=["message_id"],
                           say_hint="Only the driver's reply to the read-back counts. Ask them to reply YES.")
            verdict = f.confirmation_attested(a.message_id, str(row[0]), row[1], str(row[2]))
            self._save(c, f, ver)
        hint = {"YES": None, "NO": "Offer the other options again.",
                "UNCLEAR": "Ask the driver to reply YES to confirm, or choose another option.",
                "IGNORED": "No option is waiting for confirmation."}[verdict]
        return env("OK" if verdict == "YES" else "NEEDS_INFO", {"verdict": verdict}, say_hint=hint)

    def t_hold_option(self, p, a: OptionArg, key):
        sid, fac = self._driver_tx(p)
        try:
            with self.db.tx("agent", (fac,), p.driver_id) as c:
                f, ver = self._flow(c, p, self._thread(c, p))
                ok = f.allow("hold_option", {"option_id": str(a.option_id)})
                if not ok.allowed:
                    return env("REQUIRES_HUMAN" if ok.reason == "HUMAN_ACTIVE" else "ERROR", {"code": ok.reason})
                r = c.execute("SELECT hold_option(%s,%s,%s,%s,%s,%s)",
                              (a.option_id, p.thread_id, f.attestation_id, key, p.name, timedelta(seconds=self.hold_ttl))).fetchone()[0]
                f.hold_result("HELD", str(a.option_id))
                self._save(c, f, ver)
            return env("OK", {"status": "HELD", "hold_expires_at": r["expires_at"]},
                       say_hint="Tell the driver the slot is being held while the request goes to the warehouse.")
        except psycopg.Error as e:
            status = map_db_error(e.sqlstate, e.diag.message_primary or str(e))
            if status not in ("LOST", "OPTION_EXPIRED"):
                raise
        fresh = self._options(p, sid, fac, None, None, 3)                   # LOST: new options in the same turn
        return env(status, {"fresh_options": fresh["data"].get("options", [])},
                   say_hint="Say briefly that the slot was just taken and offer the new options.")

    def t_submit_booking_request(self, p, a: OptionArg, key):
        sid, fac = self._driver_tx(p)
        with self.db.tx("agent", (fac,), p.driver_id) as c:
            f, ver = self._flow(c, p, self._thread(c, p))
            ok = f.allow("submit_booking_request", {"option_id": str(a.option_id)})
            if not ok.allowed:
                return env("ERROR", {"code": ok.reason})
            r = c.execute("SELECT submit_booking_request(%s,%s,%s,%s,%s)",
                          (a.option_id, p.thread_id, f.attestation_id, key, p.name)).fetchone()[0]
            f.submit_result(r["status"], r["appointment_id"])
            self._save(c, f, ver)
        return env("OK", {"status": r["status"], "appointment_id": r["appointment_id"],
                          "pending_expires_at": r.get("pending_expires_at")},
                   say_hint=("Tell the driver the request is with the warehouse and they will hear back."
                             if r["status"] == "PENDING" else "Tell the driver the booking is confirmed."))

    def t_release_hold(self, p, a: OptionArg, key):
        sid, fac = self._driver_tx(p)
        with self.db.tx("agent", (fac,), p.driver_id) as c:
            f, ver = self._flow(c, p, self._thread(c, p))
            ok = f.allow("release_hold", {"option_id": str(a.option_id)})
            if not ok.allowed:
                return env("ERROR", {"code": ok.reason})
            n = c.execute("SELECT release_hold(%s,%s,%s)", (a.option_id, p.thread_id, p.name)).fetchone()[0]
            f.released()
            self._save(c, f, ver)
        return env("OK", {"released": n})

    def t_get_booking_status(self, p, a: StatusArgs, key):
        sid, fac, live = self._shipment(p, a.shipment_id)
        if sid is None:
            return env("NEEDS_INFO", {"live_shipments": [r[0] for r in live]}, missing=["shipment_id"])
        with self.db.tx("agent", (fac,), p.driver_id, readonly=True) as c:
            rows = c.execute("""SELECT a.appointment_id, a.status, a.is_current, lower(a.during), d.dock_code, a.pending_expires_at
                                  FROM appointments a JOIN docks d USING (dock_id)
                                 WHERE a.shipment_id=%s ORDER BY a.created_at DESC LIMIT 5""", (sid,)).fetchall()
        return env("OK", {"shipment_id": sid, "appointments": [
            {"appointment_id": r[0], "status": r[1], "current": r[2], "start": human_time(r[3]), "dock_code": r[4]} for r in rows]})

    def t_escalate_to_human(self, p, a: EscalateArgs, key):
        with self.db.tx("agent", driver=p.driver_id, readonly=True) as c:
            sid = self._thread(c, p)["shipment_id"]
            fac = c.execute("SELECT destination_facility_id FROM shipments WHERE shipment_id=%s", (sid,)).fetchone()[0] if sid else None
        r = self._escalate(p, fac, a.reason_code, a.mode, a.driver_last_message, a.what_driver_was_told, key)
        return env("OK", r, say_hint="Tell the driver a coordinator has their details and will respond within 10 minutes.")

    # ---------------------------------------------------------------- facility scheduling tool (proposals only)
    PROPOSAL_TTL_S = 600

    def _facility_view(self, facility: str, now: datetime):
        """The facility's structured state (feasibility role, read-only). The scope adds the other configured
        facilities only so the double-assignment check sees a driver's or truck's other shipments, as the
        feasibility engine does; every proposal covers `facility` alone."""
        with self.db.tx("feasibility", (facility,) + tuple(x for x in self.facilities if x != facility), readonly=True) as c:
            row = c.execute("SELECT schedule_version FROM facilities WHERE facility_id=%s", (facility,)).fetchone()
            if not row:
                raise ServiceError(404, "NOT_FOUND", "no such facility")
            data = load_view(c, now)
        if facility not in data.timings:
            raise ServiceError(409, "FACILITY_NOT_CONFIGURED", "no active facility_config")
        return snapshot_from_data(data, facility, now), row[0]

    def _schedule(self, facility: str, rule: str) -> tuple[Proposal, int]:
        """Run the deterministic planner on structured data. Writes nothing."""
        if rule not in RULES:
            raise ServiceError(400, "BAD_RULE", f"one of {', '.join(RULES)}")
        snap, sv = self._facility_view(facility, self.now())
        return propose(snap, rule), sv

    def t_propose_facility_schedule(self, p, a: ScheduleArgs, key):
        """Agent tool, read only. The facility comes from the conversation's shipment. The driver channel gets this
        driver's own row and facility totals only: other trucks, their drivers and the reasons that name them stay in
        the service. The result is a PROPOSAL, not a booking, and carries no id a move could be applied with."""
        sid, fac, live = self._shipment(p, None)
        if sid is None:
            return env("NEEDS_INFO", {"live_shipments": [r[0] for r in live]}, missing=["shipment_id"])
        with self.db.tx("agent", (fac,), p.driver_id) as c:
            f, _ = self._flow(c, p, self._thread(c, p))
            ok = f.allow("propose_facility_schedule")
            if not ok.allowed:
                return env("REQUIRES_HUMAN" if ok.reason == "HUMAN_ACTIVE" else "ERROR", {"code": ok.reason})
        prop, _ = self._schedule(fac, a.rule)
        mine = next((x for x in prop.assignments if x.shipment_id == sid), None)
        left = next((u for u in prop.unscheduled if u.shipment_id == sid), None)
        keys = ("trucks", "planned", "missed_promises", "changed_promises", "unscheduled")
        return env("OK", {
            "status": prop.status, "not_a_booking": True, "note": NOT_A_BOOKING, "rule": prop.rule, "facility_id": fac,
            "input_fingerprint": prop.input_fingerprint, "shipment_id": sid,
            "proposed": {"dock_code": mine.dock_code, "start": human_time(mine.start), "start_ts": mine.start.isoformat(),
                         "action": mine.action, "keeps_promise": mine.action == "KEEP",
                         "wait_min": mine.score.wait_min} if mine else None,
            "needs_person": {"reason_code": left.reason_code} if left else None,
            "in_window": sid not in prop.out_of_horizon,
            "facility_totals": {k: prop.metrics[k] for k in keys}},
            say_hint="This is a planning proposal for the dock office, not a booking. Do not tell the driver this time "
                     "is confirmed; offer bookable times with find_feasible_options, or escalate.")

    def schedule_proposal(self, p: Principal, facility: str, rule: str = "FACILITY_SCORE") -> dict:
        """Console, read only: the facility's proposal as computed now. Stores nothing. A facility outside this person's
        scope is indistinguishable from one that does not exist (404), as for conversations."""
        self.require(p, "schedule_proposal")
        if facility not in p.facilities:
            raise ServiceError(404, "NOT_FOUND", "no such facility")
        prop, sv = self._schedule(facility, rule)
        return prop.to_dict() | {"schedule_version": sv, "stored": False, "proposal_id": None}

    def store_schedule_proposal(self, p: Principal, facility: str, rule: str = "FACILITY_SCORE",
                                ttl_s: int | None = None) -> dict:
        """Console: compute and store a proposal, so a person can apply its moves (one at a time) before it expires.
        Storing changes no booking. The database re-validates the rows (facility, current appointments, no overlap)."""
        self.require(p, "schedule_proposal")
        if facility not in p.facilities:
            raise ServiceError(404, "NOT_FOUND", "no such facility")
        prop, sv = self._schedule(facility, rule)
        d = prop.to_dict()
        rows = [{"seq": r["seq"], "shipment_id": r["shipment_id"], "appointment_id": r["appointment_id"],
                 "action": r["action"], "dock_id": r["dock_id"], "start": r["start"], "end": r["end"],
                 "from_dock_id": (r["promised"] or {}).get("dock_id"), "from_start": (r["promised"] or {}).get("start"),
                 "score": r["score"], "reason": r["reason"][:500]} for r in d["assignments"]]
        stored = self._console_call(
            p, "SELECT record_schedule_proposal(%s,%s,%s,%s,%s,%s,%s,%s)",
            (facility, rule, ENGINE_VERSION, prop.input_fingerprint, sv, Jsonb(prop.metrics), Jsonb(rows),
             ttl_s or self.PROPOSAL_TTL_S),
            ("APPOINTMENT_CHANGED", "APPOINTMENT_NOT_MOVABLE", "PROPOSAL_ROWS_OVERLAP"))
        return d | {"schedule_version": sv, "stored": True, "proposal_id": str(stored["proposal_id"]),
                    "expires_at": stored["expires_at"]}

    MOVE_CONFLICTS = ("PROPOSAL_EXPIRED", "ALREADY_APPLIED", "APPOINTMENT_IN_PROGRESS", "SHIPMENT_IN_DOCK",
                      "APPOINTMENT_CHANGED", "SLOT_IN_PAST", "OPERATIONAL_HOLD", "OUTSIDE_HOURS", "SLOT_TAKEN",
                      "REQUEST_PENDING", "CROSS_FACILITY", "DOCK_WRONG_FACILITY", "DOCK_WEIGHT_EXCEEDED",
                      "DOCK_NOT_REFRIGERATED", "DOCK_NOT_HEAVY", "COMPAT_CHANGE_NEEDS_READ_COMMITTED",
                      "IDEMPOTENCY_KEY_REUSED", "NOT_FEASIBLE")

    def move_appointment(self, p: Principal, appointment_id: str, proposal_id, key: str, note: str | None = None) -> dict:
        """Console: apply ONE move of a stored, unexpired proposal. Refused while the booking switch is off, exactly
        like the agent's booking tools. The database function re-proves every booking rule under the decide locks;
        the readiness check here is advisory (has the truck's ETA moved past the proposed start since?)."""
        if not self.booking_enabled:
            raise ServiceError(409, "BOOKING_DISABLED", "booking changes are switched off; the dock office calls the driver")
        self.require(p, "move_appointment")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            row = c.execute("""SELECT r.shipment_id, r.start_at, pr.facility_id
                                 FROM schedule_proposal_rows r JOIN schedule_proposals pr USING (proposal_id)
                                WHERE r.proposal_id=%s AND r.appointment_id=%s AND r.action='MOVE'""",
                            (proposal_id, appointment_id)).fetchone()
        if row:
            sid, start, fac = row
            snap, _ = self._facility_view(fac, self.now())
            job = next((j for j in snap.jobs if j.shipment_id == sid), None)
            if job is not None and job.release > start + timedelta(minutes=1):
                raise ServiceError(409, "NOT_FEASIBLE", f"the truck is now ready at {hm(job.release)}, after the proposed start")
            if job is None and sid in snap.out_of_horizon and start < snap.horizon_end:
                raise ServiceError(409, "NOT_FEASIBLE", "the truck is now ready only after the planning window")
        try:
            with self.db.tx("console", p.facilities, staff=self._staff(p)) as c:
                return c.execute("SELECT move_appointment(%s,%s,%s,%s)",
                                 (proposal_id, appointment_id, key, note)).fetchone()[0]
        except psycopg.Error as e:
            if e.sqlstate == "23P01":                     # a concurrent block or hold won the same dock time
                raise ServiceError(409, "SLOT_TAKEN", "another change took that dock time first") from e
            code = (e.diag.message_primary or "").split(":")[0]
            if code in ("ROLE_NOT_ALLOWED", "FORBIDDEN"):
                raise ServiceError(403, code, e.diag.message_primary) from e
            if code in ("PROPOSAL_UNKNOWN", "APPOINTMENT_UNKNOWN", "NOT_A_PROPOSED_MOVE"):
                raise ServiceError(404, code) from e
            raise ServiceError(409 if code in self.MOVE_CONFLICTS else 400, code or "ERROR",
                               e.diag.message_primary or "") from e

    # ---------------------------------------------------------------- Phase G: optimizer + observability (read only)
    RECOMMEND_NOTE = ("Recommendation only, not a booking. Options are ranked by the deterministic optimizer; a booking "
                      "still goes through the driver's read-back and yes, or a person, and the database re-checks it.")

    def recommend(self, p: Principal, shipment_id: str, earliest: datetime | None = None, latest: datetime | None = None,
                  days: int = 1) -> dict:
        """Console (planner, ops_lead): ranked dock/slot options for one truck, with every hard constraint evaluated and
        the rejected alternatives. Reads with the CONSOLE role in the person's own facility scope, so it needs no
        feasibility login (the pilot api has none; see PHASE_G_FAILURE_DEMONSTRATIONS.md, Confirm 500) and it cannot
        see another facility's shipments. Records a run summary; changes nothing."""
        self.require(p, "view_recommendations")
        now = self.now()
        corr = CORRELATION_ID.get() or str(uuid.uuid4())
        with self.db.tx("console", p.facilities, readonly=True) as c:
            row = c.execute("SELECT destination_facility_id FROM shipments WHERE shipment_id=%s", (shipment_id,)).fetchone()
            if not row or row[0] not in p.facilities:
                raise ServiceError(404, "NOT_FOUND", "no such shipment")
            fac = row[0]
            data = load_view(c, now)
            sv = c.execute("SELECT schedule_version FROM facilities WHERE facility_id=%s", (fac,)).fetchone()[0]
        cfg = OptimizerConfig(**{**self.optimizer_config.__dict__, "operating_days_ahead": days, "max_options": 5})
        opt = SlotOptimizer(data, cfg).optimize(
            OptimizationRequest(shipment_id, fac, earliest, latest, request_id=corr, data_as_of=now, schedule_version=sv), now)
        self._record_run(opt, "console", "console", (fac,))
        return opt.to_dict() | {"status": "RECOMMENDATION", "note": self.RECOMMEND_NOTE, "correlation_id": corr}

    DLQ_NOTE = ("Queue and dead-letter depth live in SQS/CloudWatch, not in the database; the console does not read "
                "AWS metrics. Proxy shown: inbound redeliveries and unprocessed messages from inbound_receipts.")

    def observability(self, p: Principal, facility: str | None = None, hours: int = 24) -> dict:
        """Console (planner, ops_lead): every number comes from a persisted row or a measured timing, in this person's
        facility scope (RLS). What cannot be measured here is reported as unavailable, with the reason."""
        self.require(p, "view_observability")
        scope = self._scope(p, facility)
        now = self.now()
        since = now - timedelta(hours=max(1, min(hours, 24 * 31)))

        def rows(c, sql, *a):
            cur = c.execute(sql, a)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        def kv(c, sql, *a):
            return {str(k): int(v) for k, v in c.execute(sql, a).fetchall()}

        with self.db.tx("console", scope, readonly=True) as c:
            overview = {
                "shipments_by_status": kv(c, "SELECT current_status, count(*) FROM shipments GROUP BY 1 ORDER BY 1"),
                "appointments_by_status": kv(c, """SELECT a.status, count(*) FROM appointments a WHERE a.is_current
                                                   GROUP BY 1 ORDER BY 1"""),
                "open_escalations_by_reason": kv(c, """SELECT reason_code, count(*) FROM escalations
                                                       WHERE status IN ('OPEN','ACKNOWLEDGED') GROUP BY 1 ORDER BY 1"""),
                "open_approval_requests": c.execute("SELECT count(*) FROM approval_requests WHERE status='OPEN'").fetchone()[0],
                "docks_blocked_now": c.execute("""SELECT count(DISTINCT dock_id) FROM dock_events
                                                  WHERE during @> now()""").fetchone()[0],
                "conversations_active": c.execute("""SELECT count(DISTINCT thread_id) FROM chat_messages
                                                     WHERE created_at >= %s""", (since,)).fetchone()[0],
            }
            turns = rows(c, """SELECT count(*) AS turns,
                                      count(*) FILTER (WHERE extractor = 'model') AS model_turns,
                                      count(*) FILTER (WHERE extractor <> 'model') AS rules_turns,
                                      count(*) FILTER (WHERE fallback_reason IS NOT NULL) AS fallbacks,
                                      count(*) FILTER (WHERE escalated) AS escalated,
                                      percentile_disc(0.5) WITHIN GROUP (ORDER BY turn_ms) AS turn_p50_ms,
                                      percentile_disc(0.95) WITHIN GROUP (ORDER BY turn_ms) AS turn_p95_ms
                                 FROM agent_turn_metrics WHERE created_at >= %s""", since)[0]
            tools = rows(c, """SELECT t->>'tool' AS tool, count(*) AS calls,
                                      count(*) FILTER (WHERE t->>'status' NOT IN ('OK','NEEDS_INFO')) AS not_ok,
                                      percentile_disc(0.5) WITHIN GROUP (ORDER BY (t->>'ms')::int) AS p50_ms,
                                      percentile_disc(0.95) WITHIN GROUP (ORDER BY (t->>'ms')::int) AS p95_ms
                                 FROM agent_turn_metrics m, jsonb_array_elements(m.tools) t
                                WHERE m.created_at >= %s GROUP BY 1 ORDER BY 2 DESC, 1""", since)
            fallbacks = kv(c, """SELECT fallback_reason, count(*) FROM agent_turn_metrics
                                 WHERE created_at >= %s AND fallback_reason IS NOT NULL GROUP BY 1 ORDER BY 1""", since)
            optim = rows(c, """SELECT count(*) AS runs,
                                      count(*) FILTER (WHERE options_count > 0) AS with_options,
                                      count(*) FILTER (WHERE fallback_used) AS fallbacks,
                                      percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
                                      percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms,
                                      max(duration_ms) AS max_ms,
                                      coalesce(round(avg(candidates_evaluated))::int, 0) AS avg_candidates,
                                      coalesce(round(avg(rejected_count))::int, 0) AS avg_rejected
                                 FROM optimization_runs WHERE created_at >= %s""", since)[0]
            optim["by_status"] = kv(c, """SELECT solver_status, count(*) FROM optimization_runs WHERE created_at >= %s
                                          GROUP BY 1 ORDER BY 1""", since)
            optim["by_caller"] = kv(c, """SELECT caller, count(*) FROM optimization_runs WHERE created_at >= %s
                                          GROUP BY 1 ORDER BY 1""", since)
            optim["by_outcome"] = kv(c, """SELECT outcome_code, count(*) FROM optimization_runs WHERE created_at >= %s
                                           GROUP BY 1 ORDER BY 1""", since)
            reliability = {
                "approval_requests_by_status": kv(c, """SELECT status, count(*) FROM approval_requests
                                                        WHERE created_at >= %s GROUP BY 1 ORDER BY 1""", since),
                "booking_events_by_action": kv(c, """SELECT action, count(*) FROM booking_events WHERE at >= %s
                                                     GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20""", since),
                "notifications_by_status": kv(c, """SELECT status, count(*) FROM notifications WHERE created_at >= %s
                                                    GROUP BY 1 ORDER BY 1""", since),
                "notification_retries": c.execute("""SELECT count(*) FROM notifications WHERE created_at >= %s
                                                     AND attempts > 1""", (since,)).fetchone()[0],
                "outbox_by_status": kv(c, """SELECT status, count(*) FROM outbox WHERE created_at >= %s
                                             GROUP BY 1 ORDER BY 1""", since),
                "inbound": rows(c, """SELECT count(*) AS received,
                                             count(*) FILTER (WHERE processed_at IS NOT NULL) AS processed,
                                             count(*) FILTER (WHERE deliveries > 1) AS redelivered,
                                             count(*) FILTER (WHERE attempts > 1) AS retried,
                                             count(*) FILTER (WHERE processed_at IS NULL
                                                              AND created_at < now() - interval '5 minutes') AS stuck_over_5_min
                                        FROM inbound_receipts WHERE created_at >= %s""", since)[0],
                "queue_dlq": {"status": "unavailable", "reason": self.DLQ_NOTE},
            }
            evals = rows(c, """SELECT suite, mode, dataset_version, total, passed, failed, skipped, cost_usd::text AS cost_usd,
                                      summary, code_version, finished_at
                                 FROM evaluation_runs ORDER BY finished_at DESC LIMIT 5""")
            glob = c.execute("SELECT observability_global(%s)", (since,)).fetchone()[0]
            # G.1: recent turns (facility scope by RLS) for per-interaction trace links; ids and codes only
            recent = rows(c, """SELECT turn_ref::text AS turn_ref, created_at, facility_id, extractor, fallback_reason,
                                       turn_ms, escalated
                                  FROM agent_turn_metrics WHERE created_at >= %s ORDER BY created_at DESC LIMIT 20""", since)
            # G.1 (0028): cost source, refusals vs errors, reconciliation. Absent before 0028 is applied: say so.
            detail = None
            if c.execute("SELECT to_regprocedure('setuhaul.observability_model_detail(timestamptz)') IS NOT NULL"
                         ).fetchone()[0]:
                detail = c.execute("SELECT observability_model_detail(%s)", (since,)).fetchone()[0]
        for e in evals:
            e["finished_at"] = e["finished_at"].isoformat() if hasattr(e["finished_at"], "isoformat") else e["finished_at"]
        reliability["idempotency"] = glob.get("idempotency")
        reliability["approval_callbacks"] = glob.get("approval_callbacks")
        model = glob.get("model_usage") or {}
        agent = {**turns, "tools": tools, "fallback_reasons": fallbacks,
                 "model_usage": model if model.get("calls") else {**model, "status": "no model calls in the window"},
                 "model_usage_scope": "all facilities (the model ledger has no facility column; no driver data)"}
        agent["model_outcomes"] = self._model_outcomes(turns, fallbacks, model, detail)
        if not turns["turns"]:
            agent["status"] = "unavailable: no agent turns recorded in the window"
            agent["status_code"] = "NO_RECENT_TURNS"
        else:
            agent["status_code"] = "OK"
        ls = self._langsmith_links(recent, evals, agent["model_outcomes"])
        agent["recent_turns"] = ls.pop("recent_turns")
        return {"generated_at": now.isoformat(), "since": since.isoformat(), "facilities": list(scope),
                "data_label": "SYNTHETIC DATA - pilot/local only, no real drivers or shipments",
                "overview": overview, "agent": agent, "optimization": optim, "reliability": reliability,
                "evaluation": evals or [{"status": "unavailable: no evaluation run recorded"}], "langsmith": ls}

    @staticmethod
    def _model_outcomes(turns: dict, fallbacks: dict, model: dict, detail: dict | None) -> dict:
        """G.1 (G3/G5): what happened to model use, split so the parts add up to the totals.

        Turns (this person's facilities): model extraction + rules reader after a failure, by outcome class
        (provider failure / budget refusal / never started) + rules reader with no model configured.
        Attempts (ledger, all facilities): ok + provider errors + budget refusals + unsettled reservations."""
        from ..agent.errors import NOT_STARTED, outcome_class, normalize
        by_cat: dict[str, int] = {}
        for reason, n in (fallbacks or {}).items():
            cat = normalize(reason)
            by_cat[cat] = by_cat.get(cat, 0) + int(n)
        classes = {"PROVIDER_FAILURE": 0, "BUDGET_REFUSAL": 0, NOT_STARTED: 0}
        for cat, n in by_cat.items():
            classes[outcome_class(cat)] += n
        total = int(turns.get("turns") or 0)
        model_turns = int(turns.get("model_turns") or 0)
        fallback_turns = int(turns.get("fallbacks") or 0)
        rules_only = int(turns.get("rules_turns") or 0) - fallback_turns
        src = detail or model
        attempts = int(src.get("attempts", src.get("calls")) or 0)
        parts = {k: int(src.get(k) or 0) for k in ("ok", "errors", "budget_refusals", "unsettled")}
        ledger_by_error: dict[str, int] = {}
        for k, n in (src.get("by_error") or {}).items():
            ledger_by_error[normalize(k)] = ledger_by_error.get(normalize(k), 0) + int(n)
        out = {
            "turns": {"total": total, "model_extraction": model_turns, "rules_reader_after_failure": fallback_turns,
                      "rules_reader_no_model": max(rules_only, 0), "fallback_by_class": classes,
                      "fallback_by_category": by_cat,
                      "reconciles": total == model_turns + fallback_turns + max(rules_only, 0)},
            "attempts": {"total": attempts, **parts, "reconciles": attempts == sum(parts.values()),
                         "by_category": ledger_by_error},
            "tokens": {"input": src.get("input_tokens"), "output": src.get("output_tokens"),
                       "calls_with_reported_usage": src.get("usage_reported"),
                       "note": "provider-reported usage only; failed or refused calls report none (never estimated)"},
            "cost": ({"provider_reported_usd": detail.get("cost_provider_usd"),
                      "estimated_usd": detail.get("cost_estimated_usd"),
                      "estimated_note": "kept reservation: the provider did not report a cost for these attempts",
                      "open_reservations_usd": detail.get("reserved_open_usd"),
                      "by_cost_source": detail.get("by_cost_source")}
                     if detail else {"total_usd": model.get("cost_usd"),
                                     "note": "provider vs estimated split unavailable: migration 0028 not applied"}),
            "budget": {"day_boundary": (detail or {}).get("budget_day_boundary", "00:00 UTC (05:30 Asia/Kolkata)"),
                       "today_spent_usd": model.get("today_spent_usd"), "today_reserved_usd": model.get("today_reserved_usd"),
                       "days_utc": (detail or {}).get("budget_days_utc")},
        }
        if total == 0 and attempts == 0:
            out["status_code"], out["status"] = "NO_ACTIVITY", "no agent turns and no model calls in the window"
        elif attempts == 0 and classes[NOT_STARTED]:
            out["status_code"], out["status"] = "NOT_STARTED", "the model client never started (see fallback_by_category)"
        elif attempts and parts["ok"] == 0 and parts["errors"]:
            out["status_code"], out["status"] = "PROVIDER_FAILING", "every model attempt in the window failed"
        elif attempts and parts["ok"] == 0 and parts["budget_refusals"]:
            out["status_code"], out["status"] = "BUDGET_REFUSED", "every model attempt was refused by the daily budget"
        elif parts["errors"] or parts["budget_refusals"] or sum(classes.values()):
            failed = parts["errors"] + parts["budget_refusals"]
            out["status_code"], out["status"] = "DEGRADED", (f"{failed} of {attempts} model attempts failed or were "
                                                             f"refused; {fallback_turns} turn(s) used the rules reader")
        else:
            out["status_code"], out["status"] = "OK", "model calls succeeded in the window"
        return out

    @staticmethod
    def _langsmith_links(recent: list[dict], evals: list[dict], outcomes: dict) -> dict:
        """G.1 (G6): links only (no credential, no call to LangSmith from here or the browser). A link that is not
        configured is reported as unavailable, never guessed."""
        import os
        from ..observability.langsmith import ALLOWED_UI, ui_url_from_env
        from ..observability.tracing import trace_url
        ui = ui_url_from_env(dict(os.environ))
        tracing_on = (os.environ.get("SETUHAUL_LANGSMITH_TRACING") or "").lower() in ("on", "true", "1")
        latest = next((e for e in evals if not e.get("status")), None)
        ls_meta = ((latest or {}).get("summary") or {}).get("langsmith") or {}
        exp = ls_meta.get("experiment_url")
        exp = exp if isinstance(exp, str) and exp.startswith(ALLOWED_UI) else None
        for r in recent:
            r["created_at"] = r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"]
            r["trace_url"] = trace_url(ui, r["turn_ref"]) if tracing_on else None
        return {"tracing": "on" if tracing_on else "off", "project_url": ui,
                "project_status": None if ui else "unavailable: SETUHAUL_LANGSMITH_UI_URL not configured",
                "experiment_url": exp, "dataset_version": ls_meta.get("dataset_version") or (latest or {}).get("dataset_version"),
                "evaluator_version": ls_meta.get("evaluator_version"),
                "latest_eval": ({"suite": latest["suite"], "passed": latest["passed"], "total": latest["total"],
                                 "failed": latest["failed"], "finished_at": latest["finished_at"]} if latest else None),
                "failure_categories": outcomes["turns"]["fallback_by_category"],
                "recent_turns": recent}

    # ---------------------------------------------------------------- console (people decide)
    @staticmethod
    def require(p: Principal, action: str):
        allowed = (PERMISSIONS.get(action) or SCHEDULE_PERMISSIONS.get(action) or PHASE_G_PERMISSIONS.get(action)
                   or DISPATCH_PERMISSIONS.get(action) or KILL_SWITCH_PERMISSIONS[action])
        if not (p.roles & allowed):
            raise ServiceError(403, "FORBIDDEN", f"{action} needs one of: {', '.join(sorted(allowed))}")

    def _scope(self, p: Principal, facility: str | None) -> tuple[str, ...]:
        if facility is not None and facility not in p.facilities:
            raise ServiceError(403, "FORBIDDEN", "facility outside your scope")
        return (facility,) if facility else p.facilities

    @staticmethod
    def _staff(p: Principal) -> tuple[str, frozenset, tuple[str, ...]]:
        """The verified identity the database records for one console transaction (B-WF-2 §15)."""
        return f"staff:{p.sub}", p.roles, p.facilities

    def _console_call(self, p: Principal, sql: str, args: tuple, conflict=()):
        try:
            with self.db.tx("console", p.facilities, staff=self._staff(p)) as c:
                return c.execute(sql, args).fetchone()[0]
        except psycopg.Error as e:
            code = (e.diag.message_primary or "").split(":")[0]
            if code in ("ROLE_NOT_ALLOWED", "FORBIDDEN"):
                raise ServiceError(403, code, e.diag.message_primary) from e
            if code in ("APPOINTMENT_UNKNOWN", "HOLD_UNKNOWN"):
                raise ServiceError(404, code) from e
            if code == "BOOKING_KILLED":
                raise ServiceError(409, code, "the booking kill switch is engaged") from e
            raise ServiceError(409 if code in conflict else 400, code or "ERROR") from e

    def list_requests(self, p: Principal, facility: str | None) -> list[dict]:
        self.require(p, "list_requests")
        with self.db.tx("console", self._scope(p, facility), readonly=True) as c:
            rows = c.execute("""SELECT a.appointment_id, a.shipment_id, d.facility_id, d.dock_code, lower(a.during), upper(a.during),
                                       r.expires_at, a.needs_approval_reason, a.replaced_appointment_id, r.approval_id, r.kind
                                  FROM appointments a JOIN docks d USING (dock_id)
                                  JOIN approval_requests r ON r.appointment_id = a.appointment_id AND r.status = 'OPEN'
                                 WHERE a.status='PENDING_CONFIRMATION' ORDER BY r.expires_at""").fetchall()
        keys = ("appointment_id", "shipment_id", "facility_id", "dock_code", "start", "end", "pending_expires_at",
                "needs_approval_reason", "replaces", "approval_id", "kind")
        return [dict(zip(keys, r)) for r in rows]

    def policy_outcomes(self, p: Principal, facility: str | None) -> list[dict]:
        """B-WF-2a: how people decided the requests each confirmation rule produced (override rate per rule)."""
        self.require(p, "policy_report")
        with self.db.tx("console", self._scope(p, facility), readonly=True) as c:
            cur = c.execute("SELECT * FROM policy_rule_outcomes ORDER BY facility_id, rule_id, rule_version")
            names = [d.name for d in cur.description]
            return [{k: (float(v) if k == "override_rate" and v is not None else v) for k, v in zip(names, r)} for r in cur]

    def list_escalations(self, p: Principal) -> list[dict]:
        self.require(p, "list_requests")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            rows = c.execute("""SELECT escalation_id, reason_code, mode, facility_id, shipment_id, thread_id, status
                                  FROM escalations ORDER BY created_at DESC LIMIT 200""").fetchall()
        return [dict(zip(("escalation_id", "reason_code", "mode", "facility_id", "shipment_id", "thread_id", "status"), r))
                for r in rows]

    # ---------------------------------------------------------------- read-only conversation views (hosted console)
    THREAD_PAGE_MAX = 50
    MESSAGES_MAX = 200

    @staticmethod
    def _mask_phone(phone: str | None) -> str | None:
        return None if not phone else phone[:4] + "•" * max(len(phone) - 8, 0) + phone[-4:]

    @staticmethod
    def _delivery(provider_message_id: str | None) -> str | None:
        """Which kind of sender accepted it, never the id itself beyond its kind (test sink vs a real provider)."""
        if not provider_message_id:
            return None
        return "test_sink" if provider_message_id.startswith("testsink.") else "provider"

    def whoami(self, p: Principal) -> dict:
        """Read only (Step 6 WORKLOAD_IDENTITY check 5): who the server says this person is, derived ONLY from the
        verified Cognito token (groups -> roles, facility_scope claim), and which console actions that allows. Nothing
        the browser sends can change it; the database receives the same roles in begin_staff_context."""
        allowed = {**PERMISSIONS, **SCHEDULE_PERMISSIONS, **KILL_SWITCH_PERMISSIONS, **PHASE_G_PERMISSIONS,
                   **DISPATCH_PERMISSIONS}
        return {"principal": f"staff:{p.sub}", "roles": sorted(p.roles), "facilities": list(p.facilities),
                "permissions": {a: bool(p.roles & r) for a, r in sorted(allowed.items())}}

    def list_threads(self, p: Principal, limit: int = 25, before: datetime | None = None) -> dict:
        """Conversations visible to this person (row-level security: only their facilities), newest activity first.
        Read only. Pages by the latest-message time (`before`); at most THREAD_PAGE_MAX per page."""
        self.require(p, "list_requests")
        limit = max(1, min(int(limit), self.THREAD_PAGE_MAX))
        with self.db.tx("console", p.facilities, readonly=True) as c:
            rows = c.execute("""
                SELECT t.thread_id, t.driver_id, d.phone_e164, s.destination_facility_id, t.shipment_id, t.status,
                       t.human_active, f.state, m.created_at, m.sender_type, left(m.body_masked, 120),
                       tf.facts, n.status, n.template_id, n.provider_message_id
                  FROM chat_threads t
                  LEFT JOIN drivers d ON d.driver_id = t.driver_id
                  LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
                  LEFT JOIN thread_flow f ON f.thread_id = t.thread_id
                  LEFT JOIN thread_facts tf ON tf.thread_id = t.thread_id
                  LEFT JOIN LATERAL (SELECT created_at, sender_type, body_masked FROM chat_messages x
                                      WHERE x.thread_id = t.thread_id ORDER BY created_at DESC, message_id DESC LIMIT 1) m ON true
                  LEFT JOIN LATERAL (SELECT status, template_id, provider_message_id FROM notifications y
                                      WHERE y.thread_id = t.thread_id ORDER BY created_at DESC LIMIT 1) n ON true
                 WHERE m.created_at IS NOT NULL AND (%s::timestamptz IS NULL OR m.created_at < %s::timestamptz)
                 ORDER BY m.created_at DESC, t.thread_id
                 LIMIT %s""", (before, before, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        out = [{"thread_id": r[0], "driver_id": r[1], "driver_phone": self._mask_phone(r[2]), "facility_id": r[3],
                "shipment_id": r[4], "status": r[5], "human_active": r[6], "flow_state": r[7],
                "last_message_at": r[8], "last_sender": r[9], "last_message_preview": r[10],
                "intent": (r[11] or {}).get("intent"), "notification_status": r[12], "notification_template": r[13],
                "notification_delivery": self._delivery(r[14])} for r in rows]
        return {"threads": out, "next_before": out[-1]["last_message_at"] if more and out else None}

    def thread_detail(self, p: Principal, thread_id: str) -> dict:
        """One conversation as stored: messages (masked), the facts the reader carried, the ETA it recorded, the
        options shown and picked, and every notification with its delivery status. Read only. A thread outside this
        person's facilities is indistinguishable from one that does not exist (404)."""
        self.require(p, "list_requests")
        if not thread_id or len(thread_id) > 80:
            raise ServiceError(404, "NOT_FOUND", "no such conversation")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            t = c.execute("""SELECT t.thread_id, t.driver_id, d.phone_e164, s.destination_facility_id, t.shipment_id,
                                    t.status, t.human_active, t.human_active_since, t.opened_at, t.closed_at,
                                    t.clarification_turns, f.state, f.selected_option, f.appointment_id, f.updated_at
                               FROM chat_threads t
                               LEFT JOIN drivers d ON d.driver_id = t.driver_id
                               LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
                               LEFT JOIN thread_flow f ON f.thread_id = t.thread_id
                              WHERE t.thread_id = %s""", (thread_id,)).fetchone()
            if t is None:
                raise ServiceError(404, "NOT_FOUND", "no such conversation")
            msgs = c.execute("""SELECT * FROM (
                                  SELECT message_id, sender_type, channel, left(body_masked, 1000), created_at, reply_to
                                    FROM chat_messages WHERE thread_id = %s ORDER BY created_at DESC, message_id DESC LIMIT %s
                                ) m ORDER BY created_at, message_id""", (thread_id, self.MESSAGES_MAX)).fetchall()
            facts = c.execute("SELECT facts, asked, updated_at FROM thread_facts WHERE thread_id = %s", (thread_id,)).fetchone()
            eta = c.execute("""SELECT source, derived_eta, confidence, uncertainty_min, observed_at FROM eta_signals
                                WHERE shipment_id = %s ORDER BY observed_at DESC LIMIT 5""", (t[4],)).fetchall() if t[4] else []
            options = c.execute("""SELECT o.option_id, k.dock_code, lower(o.during), upper(o.during), o.needs_approval,
                                          o.rank, o.expires_at, o.created_at
                                     FROM shown_options o LEFT JOIN docks k ON k.dock_id = o.dock_id
                                    WHERE o.thread_id = %s ORDER BY o.created_at DESC, o.rank LIMIT 30""", (thread_id,)).fetchall()
            picks = c.execute("""SELECT option_id, selected_at FROM option_selections WHERE thread_id = %s
                                  ORDER BY selected_at DESC LIMIT 10""", (thread_id,)).fetchall()
            # the stored read-back each YES is checked against (record_read_back): evidence, never sent itself; the
            # reply the driver received is its own row (with reply_to and a notification)
            read_backs = {r[0] for r in c.execute("""SELECT read_back_message_id FROM option_selections
                                                      WHERE thread_id = %s""", (thread_id,)).fetchall()}
            notes = c.execute("""SELECT notification_id, template_id, channel, status, attempts, max_attempts,
                                        provider_message_id, last_error, chat_message_id, created_at, updated_at
                                   FROM notifications WHERE thread_id = %s ORDER BY created_at DESC LIMIT 50""",
                              (thread_id,)).fetchall()
        return {
            "thread": {"thread_id": t[0], "driver_id": t[1], "driver_phone": self._mask_phone(t[2]), "facility_id": t[3],
                       "shipment_id": t[4], "status": t[5], "human_active": t[6], "human_active_since": t[7],
                       "opened_at": t[8], "closed_at": t[9], "clarification_turns": t[10]},
            "flow": {"state": t[11], "selected_option": t[12], "appointment_id": t[13], "updated_at": t[14]},
            "messages": [{"message_id": m[0], "sender": m[1], "channel": m[2], "body_masked": m[3], "at": m[4],
                          "reply_to": m[5], "read_back_record": m[0] in read_backs} for m in msgs],
            "extracted": {"facts": facts[0] if facts else None, "asked": facts[1] if facts else None,
                          "updated_at": facts[2] if facts else None,
                          "eta_signals": [{"source": e[0], "derived_eta": e[1], "confidence": e[2],
                                           "uncertainty_min": e[3], "observed_at": e[4]} for e in eta]},
            "options": [{"option_id": o[0], "dock_code": o[1], "start": o[2], "end": o[3], "needs_approval": o[4],
                         "rank": o[5], "expires_at": o[6], "shown_at": o[7]} for o in options],
            "selections": [{"option_id": s[0], "selected_at": s[1]} for s in picks],
            "notifications": [{"notification_id": n[0], "template_id": n[1], "channel": n[2], "status": n[3],
                               "attempts": n[4], "max_attempts": n[5], "delivery": self._delivery(n[6]),
                               "test_sink_reference": n[6] if self._delivery(n[6]) == "test_sink" else None,
                               "last_error": (n[7] or "")[:120] or None, "chat_message_id": n[8],
                               "created_at": n[9], "updated_at": n[10]} for n in notes],
            "limits": {"messages": self.MESSAGES_MAX, "options": 30, "notifications": 50},
        }

    def decide(self, p: Principal, appointment_id: str, decision: str, note: str | None, key: str) -> dict:
        self.require(p, "decide")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            row = c.execute("""SELECT a.shipment_id, d.dock_code, d.facility_id, lower(a.during), upper(a.during), a.status
                                 FROM appointments a JOIN docks d USING (dock_id) WHERE a.appointment_id=%s""",
                            (appointment_id,)).fetchone()
        if not row:
            raise ServiceError(404, "APPOINTMENT_UNKNOWN")
        sid, dock, fac, start, end, status = row
        if decision == "CONFIRM" and status == "PENDING_CONFIRMATION" and self._has_login("feasibility"):
            # advisory (B-WF-2 §5): a clear early message; the database function re-proves every invariant itself.
            # The deployed api has no feasibility login (WORKLOAD_ROLES: ingest, console), so there it is skipped and
            # decide_booking_request alone decides (Option D, 23 Nov 2026: every console Confirm failed with a 500).
            ok, why = self._engine((fac,) + tuple(x for x in self.facilities if x != fac)).is_feasible(
                sid, Option(dock, start, end, (), False), self.now())
            if not ok:
                raise ServiceError(409, "NOT_FEASIBLE", why)
        return self._console_call(p, "SELECT decide_booking_request(%s,%s,%s,%s)", (appointment_id, decision, key, note),
                                  ("ALREADY_DECIDED", "PENDING_EXPIRED", "CAPACITY_LOST", "OPERATIONAL_HOLD",
                                   "SLOT_TOO_LATE", "OUTSIDE_HOURS", "SHIPMENT_NOT_BOOKABLE", "SHIPMENT_IN_DOCK"))

    def withdraw(self, p: Principal, appointment_id: str, reason_code: str, note: str, key: str) -> dict:
        self.require(p, "withdraw")
        return self._console_call(p, "SELECT withdraw_booking_request(%s,%s,%s,%s)",
                                  (appointment_id, reason_code, note, key), ("ALREADY_DECIDED",))

    def set_hold(self, p: Principal, facility: str, dock_id: str | None, reason: str) -> int:
        self.require(p, "booking_hold")
        return self._console_call(p, "SELECT set_booking_hold(%s,%s,%s)", (facility, dock_id, reason))

    def clear_hold(self, p: Principal, hold_id: int, note: str) -> bool:
        self.require(p, "booking_hold")
        return self._console_call(p, "SELECT clear_booking_hold(%s,%s)", (hold_id, note))

    def kill_switch(self, p: Principal) -> dict:
        self.require(p, "view_kill_switch")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            engaged = c.execute("SELECT booking_killed()").fetchone()[0]
        return {"engaged": bool(engaged), "booking_enabled": self.booking_enabled}

    def set_kill_switch(self, p: Principal, engaged: bool, reason: str) -> dict:
        self.require(p, "booking_kill_switch")
        v = self._console_call(p, "SELECT set_booking_kill_switch(%s,%s,%s)", (engaged, f"staff:{p.sub}", reason))
        return {"engaged": bool(v)}

    def revoke_provisional(self, p: Principal, environment: str, reason: str) -> dict:
        """0032 (Option P): an ops_lead revokes the open provisional booking authorization at once. Audited in the
        database; the same capability as the kill switch. Booking for that release stays refused afterwards."""
        self.require(p, "booking_kill_switch")
        n = self._console_call(p, "SELECT revoke_provisional_authorization_staff(%s,%s,%s)",
                               (environment, f"staff:{p.sub}", reason))
        return {"revoked": int(n or 0)}

    def pause(self, p: Principal, facility: str, paused: bool, reason: str) -> bool:
        self.require(p, "pause")
        with self.db.tx("console", self._scope(p, facility), staff=self._staff(p)) as c:
            return c.execute("SELECT set_automation_paused(%s,%s,%s,%s)", (facility, paused, f"staff:{p.sub}", reason)).fetchone()[0]

    def block_dock(self, p: Principal, dock_id: str, event_id: str, start: datetime, end: datetime, kind: str, reason: str) -> int:
        self.require(p, "block_dock")
        try:
            with self.db.tx("console", p.facilities, staff=self._staff(p)) as c:
                return c.execute("SELECT block_dock(%s,%s,%s,%s,%s,%s,%s)",
                                 (event_id, dock_id, start, end, kind, reason, f"staff:{p.sub}")).fetchone()[0]
        except psycopg.Error as e:
            if e.sqlstate == "23P01":
                raise ServiceError(409, "BOOKINGS_IN_WINDOW", "move or mark the affected bookings at risk first") from e
            raise ServiceError(403 if "FORBIDDEN" in str(e) else 400, (e.diag.message_primary or "ERROR").split(":")[0]) from e

    def takeover(self, p: Principal, thread_id: str, reason: str) -> None:
        self.require(p, "takeover")
        try:
            with self.db.tx("console", p.facilities, staff=self._staff(p)) as c:
                c.execute("SELECT take_over_thread(%s,%s,%s)", (thread_id, f"staff:{p.sub}", reason))
        except psycopg.Error as e:
            raise ServiceError(404, "THREAD_UNKNOWN") from e

    def handback(self, p: Principal, thread_id: str, summary: str) -> None:
        self.require(p, "handback")
        try:
            with self.db.tx("console", p.facilities, staff=self._staff(p)) as c:
                c.execute("SELECT hand_back_thread(%s,%s,%s)", (thread_id, f"staff:{p.sub}", summary))
        except psycopg.Error as e:
            code = (e.diag.message_primary or "ERROR").split(":")[0]
            raise ServiceError(409 if code == "THREAD_NOT_IN_HUMAN_HANDS" else 400, code) from e

    # ---------------------------------------------------------------- inbound WhatsApp (signature verified upstream)
    def ingest(self, phone: str, external_id: str, text: str, sessions) -> dict:
        """Store the driver's message (once), then classify it and write the attestation if it answers a read-back.
        Runs as setuhaul_ingest. A retried webhook resumes unfinished work recorded in inbound_receipts.
        Returns an INTERNAL event for the orchestrator queue; it is never sent back to the webhook caller."""
        with self.db.tx("ingest", readonly=True) as c:
            r = c.execute("SELECT driver_id FROM drivers WHERE phone_e164=%s AND status <> 'INACTIVE'", (phone,)).fetchone()
        if not r:
            return {"unknown_sender": True}
        return self.ingest_driver(r[0], external_id, text, sessions)

    def ingest_driver(self, drv: str, external_id: str, text: str, sessions, channel: str = "WHATSAPP",
                      agent_turn: bool = True) -> dict:
        """The inbound path once the CHANNEL has identified the driver (WhatsApp: the verified number; Telegram: the
        linked account, migration 0035). `agent_turn=False` stores and attests the message but gives it no agent
        turn (a button press the service itself completes): the receipt is marked processed in the same claim."""
        with self.db.tx("ingest", driver=drv) as c:
            th = c.execute("""SELECT thread_id, shipment_id FROM chat_threads WHERE driver_id=%s
                                AND status NOT IN ('CLOSED','RESOLVED') ORDER BY opened_at DESC LIMIT 1""", (drv,)).fetchone()
            if not th:
                live = c.execute("""SELECT shipment_id FROM shipments WHERE driver_id=%s
                                      AND current_status NOT IN ('COMPLETED','CANCELLED')""", (drv,)).fetchall()
                th = (f"THR-{uuid.uuid4().hex[:12]}", live[0][0] if len(live) == 1 else None)
                c.execute("INSERT INTO chat_threads (thread_id,driver_id,shipment_id,status) VALUES (%s,%s,%s,'OPEN')",
                          (th[0], drv, th[1]))
            # Same per-conversation lock as read-back, attestation, hold and request (migration 0006): a booking
            # step that is checking freshness waits for this message to commit, or this message waits for it.
            c.execute("SELECT thread_lock(%s)", (th[0],))
            norm = re.sub(r"\s+", " ", text.strip().lower())
            # Mask long digit runs (phone, licence). Order references keep their date part: masking it hid every
            # "ORD-260804-020" from the model (found by the live G3 replay, B-EV-2).
            masked = re.sub(r"(?<!ORD-)\d{6,}", "######", text)[:2000]
            digest = hashlib.sha256(norm.encode()).hexdigest()
            # F1 / G3 replay (THR009, AT05): a provider redelivery can carry a NEW message id. The same report from the
            # same driver in the same conversation within 2 minutes is the same message: no second reply. Short replies
            # ("yes", "2", "haan ji") are exempt: drivers repeat them on purpose (a yes to the ETA question, then a yes
            # to the read-back), and confirmations are protected by the attestation rules, not by this check.
            if agent_turn and len(norm.split()) > 3 and c.execute(
                    """SELECT 1 FROM chat_messages WHERE thread_id=%s AND sender_type='DRIVER' AND content_hash=%s
                         AND external_message_id IS DISTINCT FROM %s AND created_at > now() - interval '2 minutes'""",
                    (th[0], digest, external_id)).fetchone():
                log.info("same text redelivered under a new id within 2 minutes: treated as a duplicate")
                return {"duplicate": True, "content_duplicate": True, "thread_id": th[0], "external_message_id": external_id}
            m = c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,external_message_id,content_hash,body_masked)
                             VALUES (%s,'DRIVER',%s,%s,%s,%s) ON CONFLICT (channel, external_message_id) DO NOTHING
                             RETURNING message_id""",
                          (th[0], channel, external_id, digest, masked)).fetchone()
            duplicate = m is None
            if duplicate:                                   # provider retry: find what was stored the first time
                row = c.execute("""SELECT m.message_id, m.thread_id, m.body_masked, t.shipment_id
                                     FROM chat_messages m JOIN chat_threads t USING (thread_id)
                                    WHERE m.channel=%s AND m.external_message_id=%s""", (channel, external_id)).fetchone()
                if not row:
                    return {"duplicate": True, "external_message_id": external_id}
                mid, th, masked = row[0], (row[1], row[3]), row[2]
            else:
                mid = m[0]
                c.execute("INSERT INTO inbound_receipts (message_id, thread_id) VALUES (%s,%s)", (mid, th[0]))
            rec = c.execute("SELECT attestation_status, enqueued FROM inbound_receipts WHERE message_id=%s", (mid,)).fetchone()
            fac = None
            if th[1]:
                # 0035: during a reassignment offer the new driver's conversation is bound to a shipment that is not
                # theirs until they say yes; their session then carries no facility (RLS hides the shipment)
                row = c.execute("SELECT destination_facility_id FROM shipments WHERE shipment_id=%s", (th[1],)).fetchone()
                fac = row[0] if row else None
        status, enqueued = rec if rec else ("PENDING", False)
        recovered = False
        if not duplicate:
            status = self._attest(drv, th[0], mid, masked)
        elif status == "FAILED" and self._claim_retry(drv, mid):
            # a retried delivery resumes a FAILED attestation; PENDING means the first delivery is still working
            # (or crashed, which the reconciler handles), so a duplicate never races it
            status = self._attest(drv, th[0], mid, masked)
            recovered = status in ("DONE", "NOT_NEEDED")
        with self.db.tx("ingest", driver=drv) as c:              # atomic claim: one of two simultaneous deliveries wins
            if recovered:
                c.execute("SELECT thread_lock(%s)", (th[0],))           # S4-03: serialized with claim / renew / fence
                # B-WF-1: the message needs a fresh turn now that it is attested. A new generation gives it a new
                # queue deduplication id and makes it processable again; a turn still running for the old
                # generation loses its lease and cannot mark it processed.
                claimed = c.execute("""UPDATE inbound_receipts SET generation=generation+1, enqueued=true, enqueued_at=now(),
                                              published_at=NULL, processed_at=NULL, processing_owner=NULL,
                                              processing_until=NULL, updated_at=now()
                                        WHERE message_id=%s RETURNING generation""", (mid,)).fetchone()
            elif not agent_turn:                             # handled by the service itself: no agent turn
                claimed = c.execute("""UPDATE inbound_receipts SET enqueued=true, enqueued_at=now(), published_at=now(),
                                              processed_at=now(), updated_at=now()
                                        WHERE message_id=%s AND NOT enqueued RETURNING generation""", (mid,)).fetchone()
            else:
                claimed = c.execute("""UPDATE inbound_receipts SET enqueued=true, enqueued_at=now(), updated_at=now()
                                        WHERE message_id=%s AND NOT enqueued RETURNING generation""", (mid,)).fetchone()
        if not claimed:
            return {"duplicate": True, "thread_id": th[0], "external_message_id": external_id, "message_id": mid,
                    "driver_id": drv}
        if not agent_turn:
            return {"thread_id": th[0], "message_id": mid, "external_message_id": external_id, "driver_id": drv,
                    "attestation_status": status, "agent_turn": False, "facility_id": fac}
        # The caller (InboundRelay) publishes this event and then marks the receipt published. If it crashes in
        # between, recover_inbound() finds the receipt and publishes it again (B2-14).
        return {"thread_id": th[0], "message_id": mid, "external_message_id": external_id, "driver_id": drv,
                "generation": claimed[0], "session_token": sessions.mint(drv, th[0], fac), "attestation_status": status,
                "recovered": recovered,
                # G.1: the webhook request's id rides the queue event so the agent turn logs and traces under it
                "correlation_id": CORRELATION_ID.get()}

    def record_delivery_status(self, provider_message_id: str | None, correlation: str | None, status: str,
                               error: str | None = None) -> str:
        """B-WF-3: a provider status receipt, from the signed webhook (ingest role). Matched by the provider's id or by
        our notification id echoed back; unknown receipts are ignored."""
        import uuid as _uuid
        try:
            corr = str(_uuid.UUID(correlation)) if correlation else None
        except ValueError:
            corr = None
        with self.db.tx("ingest") as c:
            return c.execute("SELECT record_delivery_status(%s,%s,%s,%s)",
                             (provider_message_id, corr, status, error)).fetchone()[0]

    # ---------------------------------------------------------------- B-WF-1: inbound outbox stages (setuhaul_ingest)
    def mark_published(self, driver_id: str, message_id: int) -> None:
        with self.db.tx("ingest", driver=driver_id) as c:
            c.execute("UPDATE inbound_receipts SET published_at=now() WHERE message_id=%s AND published_at IS NULL",
                      (message_id,))

    def claim_turn(self, p: Principal, message_id: int, owner: str, lease_s: int = 120, generation: int = 0) -> bool:
        """Processing lease for one inbound message. Refused when the message was already processed (however late
        the duplicate arrives), another worker holds a live lease, or the event is for an older generation (S4-03).
        The message must belong to the session's thread. Taken under the conversation lock, like the fence."""
        with self.db.tx("ingest", driver=p.driver_id) as c:
            c.execute("SELECT thread_lock(%s)", (p.thread_id,))
            return c.execute("""UPDATE inbound_receipts SET processing_owner=%s, deliveries=deliveries+1,
                                       processing_until=clock_timestamp()+make_interval(secs=>%s)
                                 WHERE message_id=%s AND thread_id=%s AND generation=%s AND processed_at IS NULL
                                   AND (processing_until IS NULL OR processing_until < clock_timestamp())
                             RETURNING 1""", (owner, lease_s, message_id, p.thread_id, generation)).fetchone() is not None

    def renew_turn(self, p: Principal, message_id: int, owner: str, lease_s: int, generation: int) -> bool:
        """Heartbeat (S4-02): extend a lease this worker still holds. An expired, taken-over or superseded lease is
        never revived, so the fence keeps refusing that worker."""
        with self.db.tx("ingest", driver=p.driver_id) as c:
            c.execute("SELECT thread_lock(%s)", (p.thread_id,))
            return c.execute("""UPDATE inbound_receipts SET processing_until=clock_timestamp()+make_interval(secs=>%s)
                                 WHERE message_id=%s AND processing_owner=%s AND generation=%s AND processed_at IS NULL
                                   AND processing_until > clock_timestamp()
                             RETURNING 1""", (lease_s, message_id, owner, generation)).fetchone() is not None

    def finish_turn(self, p: Principal, message_id: int, owner: str, generation: int = 0) -> bool:
        with self.db.tx("ingest", driver=p.driver_id) as c:
            c.execute("SELECT thread_lock(%s)", (p.thread_id,))
            # R2.0 S4-R2-01: only a LIVE lease may mark the message processed. A worker whose lease expired, even
            # if nobody has taken the message yet, leaves it for redelivery.
            ok = c.execute("""UPDATE inbound_receipts SET processed_at=now(), processing_until=NULL
                               WHERE message_id=%s AND processing_owner=%s AND generation=%s AND processed_at IS NULL
                                 AND processing_until > clock_timestamp()
                           RETURNING 1""", (message_id, owner, generation)).fetchone() is not None
        if not ok:
            log.warning("turn for message %s finished without a live lease; it will be delivered again", message_id)
        return ok

    def release_turn(self, p: Principal, message_id: int, owner: str) -> None:
        """A failed turn gives the message back at once (the queue redelivers it)."""
        with self.db.tx("ingest", driver=p.driver_id) as c:
            c.execute("SELECT thread_lock(%s)", (p.thread_id,))
            c.execute("""UPDATE inbound_receipts SET processing_until=NULL
                          WHERE message_id=%s AND processing_owner=%s AND processed_at IS NULL""", (message_id, owner))

    def recover_inbound(self, sessions, unpublished_after_s: int = 30, stalled_after_s: int = 360) -> list[dict]:
        """Recovery sweep (every minute in AWS): re-claims and returns, as queue events, every message that was stored
        but not published (a crash between claim and publish), and every published message that no turn finished
        and no live lease holds after `stalled_after_s` (longer than the queue's 5-minute deduplication window, so a
        re-send is not dropped as a duplicate). The caller publishes them; the processing lease keeps it one turn."""
        with self.db.tx("ingest", (*self.facilities, CENTRAL)) as c:
            rows = c.execute("""
                WITH r AS (
                  UPDATE inbound_receipts r SET enqueued=true, enqueued_at=now(), published_at=NULL
                   WHERE r.processed_at IS NULL AND (r.processing_until IS NULL OR r.processing_until < now())
                     AND ((r.published_at IS NULL
                           AND coalesce(r.enqueued_at, r.created_at) < now() - make_interval(secs => %s))
                          OR r.published_at < now() - make_interval(secs => %s))
                  RETURNING r.message_id, r.thread_id, r.generation, r.attestation_status)
                SELECT r.message_id, r.thread_id, r.generation, r.attestation_status, t.driver_id,
                       s.destination_facility_id, m.external_message_id
                  FROM r JOIN chat_threads t USING (thread_id)
                  JOIN chat_messages m ON m.message_id = r.message_id
                  LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
                 ORDER BY r.message_id""", (unpublished_after_s, stalled_after_s)).fetchall()
        return [{"thread_id": tid, "message_id": mid, "external_message_id": ext, "driver_id": drv, "generation": gen,
                 "session_token": sessions.mint(drv, tid, fac), "attestation_status": st, "recovered": False,
                 "redelivered": True}
                for mid, tid, gen, st, drv, fac, ext in rows]

    # ---------------------------------------------------------------- S4-04: migration 0011 cutover (docs/operations/RUNBOOK_0011.md)
    def legacy_uncertain_receipts(self) -> list[dict]:
        """EVERY receipt that migration 0011 marked processed from the OLD path's enqueue claim alone (no processing
        claim was ever taken: owner NULL, deliveries 0). Any of them may have been lost in the old in-memory queue.
        R2.0 S4-R2-02: a later agent message in the same conversation does NOT exclude a receipt (it may answer a
        later message); it is shown as supporting information for the operator, with the next driver message."""
        with self.db.tx("ingest", (*self.facilities, CENTRAL), readonly=True) as c:
            rows = c.execute("""
                SELECT r.message_id, r.thread_id, m.external_message_id, m.created_at,
                       (SELECT count(*) FROM chat_messages a WHERE a.thread_id = r.thread_id
                           AND a.sender_type = 'AGENT' AND a.created_at >= m.created_at),
                       (SELECT min(a.created_at) FROM chat_messages a WHERE a.thread_id = r.thread_id
                           AND a.sender_type = 'AGENT' AND a.created_at >= m.created_at),
                       (SELECT min(d.created_at) FROM chat_messages d WHERE d.thread_id = r.thread_id
                           AND d.sender_type = 'DRIVER' AND d.message_id > m.message_id)
                  FROM inbound_receipts r JOIN chat_messages m ON m.message_id = r.message_id
                 WHERE r.processed_at IS NOT NULL AND r.published_at IS NOT NULL
                   AND r.processing_owner IS NULL AND r.deliveries = 0
                 ORDER BY r.message_id""").fetchall()
        return [{"message_id": a, "thread_id": b, "external_message_id": e, "received_at": t,
                 "later_agent_messages": n, "first_later_agent_message_at": fa, "next_driver_message_at": nd}
                for a, b, e, t, n, fa, nd in rows]

    def reopen_receipts(self, message_ids: list[int]) -> int:
        """Give reviewed legacy messages a fresh generation; the next recovery sweep publishes them."""
        n = 0
        for mid in message_ids:
            with self.db.tx("ingest", (*self.facilities, CENTRAL)) as c:
                tid = c.execute("SELECT thread_id FROM inbound_receipts WHERE message_id=%s", (mid,)).fetchone()
                if not tid:
                    continue
                c.execute("SELECT thread_lock(%s)", (tid[0],))
                n += c.execute("""UPDATE inbound_receipts SET generation=generation+1, processed_at=NULL, published_at=NULL,
                                         enqueued=true, enqueued_at=now() - interval '1 day', processing_owner=NULL,
                                         processing_until=NULL
                                   WHERE message_id=%s AND processing_owner IS NULL AND deliveries=0
                                     AND processed_at IS NOT NULL AND published_at IS NOT NULL""",   # S4-R2-03
                                 (mid,)).rowcount
        return n

    def _claim_retry(self, drv: str, message_id: int) -> bool:
        """Atomic claim: of several simultaneous deliveries, one retries a FAILED attestation."""
        with self.db.tx("ingest", driver=drv) as c:
            return c.execute("""UPDATE inbound_receipts SET attestation_status='PENDING', updated_at=now()
                                 WHERE message_id=%s AND attestation_status='FAILED' RETURNING 1""",
                             (message_id,)).fetchone() is not None

    def _attest(self, drv: str, thread_id: str, message_id: int, text: str) -> str:
        """Classify and attest in its own transaction; record the outcome in inbound_receipts. Never raises."""
        try:
            verdict = classify_confirmation(text)
            with self.db.tx("ingest", driver=drv) as c:
                a = c.execute("SELECT attest_confirmation(%s,%s,%s,%s,%s)",
                              (thread_id, message_id, verdict, CLASSIFIER_VERSION, "ingest")).fetchone()[0]
                status = "DONE" if a else "NOT_NEEDED"
                c.execute("""UPDATE inbound_receipts SET attestation_status=%s, attempts=attempts+1, last_error=NULL,
                                    updated_at=now() WHERE message_id=%s""", (status, message_id))
            return status
        except Exception as e:                              # recorded for the reconciler; the message itself is safe
            log.exception("attestation failed for message %s", message_id)
            try:
                with self.db.tx("ingest", driver=drv) as c:
                    c.execute("""UPDATE inbound_receipts SET attestation_status='FAILED', attempts=attempts+1,
                                        last_error=%s, updated_at=now() WHERE message_id=%s""", (str(e)[:300], message_id))
            except Exception:
                log.exception("could not record attestation failure for message %s", message_id)
            return "FAILED"

    def reconcile_inbound(self, older_than_s: int = 30) -> dict:
        """Background retry of unfinished attestations (runs as setuhaul_ingest, every minute in AWS)."""
        with self.db.tx("ingest", (*self.facilities, CENTRAL), readonly=True) as c:
            rows = c.execute("""SELECT r.message_id, r.thread_id, t.driver_id, m.body_masked
                                  FROM inbound_receipts r JOIN chat_threads t USING (thread_id)
                                  JOIN chat_messages m ON m.message_id = r.message_id
                                 WHERE r.attestation_status IN ('PENDING','FAILED')
                                   AND r.updated_at < now() - make_interval(secs => %s)
                                 ORDER BY r.message_id LIMIT 500""", (older_than_s,)).fetchall()
        out: dict = {}
        for mid, tid, drv, body in rows:
            st = self._attest(drv, tid, mid, body)
            out[st] = out.get(st, 0) + 1
        return out

    # ---------------------------------------------------------------- roadmap Phase 1/2: dispatcher offers (0035)
    OFFER_TEXT = ("Booking offer from your dispatcher for order {ref}: dock {dock} at {start}. Reply YES to accept, "
                  "NO to decline, or tell us another time that suits you.")
    REASSIGN_TEXT = ("New trip offered to you by your dispatcher: order {ref}, dock {dock} at {start}. Reply YES to "
                     "accept the trip and this slot, or NO to decline.")
    DISPATCH_CONFLICTS = {"SHIPMENT_NOT_BOOKABLE", "SHIPMENT_ALREADY_BOOKED", "BOOKING_IN_PROGRESS", "DRIVER_BUSY",
                          "DRIVER_NOT_AVAILABLE", "HUMAN_ACTIVE", "VEHICLE_CHANGE_NOT_SUPPORTED", "STALE_SHIPMENT",
                          "IDEMPOTENCY_KEY_REUSED", "OPTION_NOT_FEASIBLE"}

    def _staff_fn(self, p: Principal, sql: str, args: tuple):
        try:
            with self.db.tx("console", p.facilities, staff=self._staff(p)) as c:
                return c.execute(sql, args).fetchone()[0]
        except psycopg.Error as e:
            msg = e.diag.message_primary or ""
            code = msg.split(":")[0]
            if code in ("ROLE_NOT_ALLOWED", "FORBIDDEN", "PRINCIPAL_MISMATCH", "UNTRUSTED_CONTEXT"):
                raise ServiceError(403, code, msg) from e
            if code in ("SHIPMENT_UNKNOWN", "DRIVER_NOT_IN_SCOPE"):
                raise ServiceError(404, code) from e
            raise ServiceError(409 if code in self.DISPATCH_CONFLICTS else 400, code or "ERROR", msg) from e

    @staticmethod
    def _offer_status(status: str, expires_at: datetime) -> str:
        """An OFFERED row past its expiry IS expired (the database applies the same rule; no worker needed)."""
        return "EXPIRED" if status == "OFFERED" and expires_at <= datetime.now(timezone.utc) else status

    def list_dispatch_shipments(self, p: Principal, facility: str | None = None) -> list[dict]:
        """Dispatcher board: live shipments in scope with their assignment, current appointment and latest offer."""
        self.require(p, "view_dispatch")
        with self.db.tx("console", self._scope(p, facility), readonly=True) as c:
            rows = c.execute("""
                SELECT s.shipment_id, s.order_reference, s.destination_facility_id, s.current_status, s.priority_code,
                       s.original_eta_ts, s.driver_id, d.driver_name, s.vehicle_id, v.vehicle_type_code,
                       a.appointment_id, a.status, lower(a.during), k.dock_code,
                       o.offer_id, o.kind, o.status, o.expires_at, o.driver_id
                  FROM shipments s JOIN drivers d ON d.driver_id = s.driver_id JOIN vehicles v ON v.vehicle_id = s.vehicle_id
                  LEFT JOIN LATERAL (SELECT * FROM appointments x WHERE x.shipment_id = s.shipment_id
                                       AND (x.is_current OR x.status = 'PENDING_CONFIRMATION') AND x.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
                                     ORDER BY x.created_at DESC LIMIT 1) a ON true
                  LEFT JOIN docks k ON k.dock_id = a.dock_id
                  LEFT JOIN LATERAL (SELECT * FROM dispatch_offers y WHERE y.shipment_id = s.shipment_id
                                     ORDER BY y.created_at DESC LIMIT 1) o ON true
                 WHERE s.current_status IN ('PLANNED','ASSIGNED','IN_TRANSIT')
                 ORDER BY s.original_eta_ts, s.shipment_id LIMIT 200""").fetchall()
        out = []
        for r in rows:
            out.append({"shipment_id": r[0], "order_reference": r[1], "facility_id": r[2], "status": r[3],
                        "priority": r[4], "planned_eta": r[5].isoformat(), "driver_id": r[6], "driver_name": r[7],
                        "vehicle_id": r[8], "vehicle_type": r[9],
                        "appointment": {"appointment_id": r[10], "status": r[11], "start": r[12].isoformat(),
                                        "dock_code": r[13]} if r[10] else None,
                        "latest_offer": {"offer_id": str(r[14]), "kind": r[15], "status": self._offer_status(r[16], r[17]),
                                         "expires_at": r[17].isoformat(), "driver_id": r[18]} if r[14] else None,
                        "bookable": r[10] is None})
        return out

    def _dispatch_engine(self, p: Principal, shipment_id: str):
        now = self.now()
        with self.db.tx("console", p.facilities, readonly=True) as c:
            row = c.execute("""SELECT destination_facility_id, order_reference, driver_id, vehicle_id FROM shipments
                                WHERE shipment_id=%s""", (shipment_id,)).fetchone()
            if not row or row[0] not in p.facilities:
                raise ServiceError(404, "NOT_FOUND", "no such shipment")
            data = load_view(c, now)
            sv = c.execute("SELECT schedule_version FROM facilities WHERE facility_id=%s", (row[0],)).fetchone()[0]
            docks = dict(c.execute("SELECT dock_code, dock_id FROM docks WHERE facility_id=%s", (row[0],)).fetchall())
        res = FeasibilityEngine(data).find_options(shipment_id, now, max_options=3)
        return row, res, sv, docks

    def dispatch_options(self, p: Principal, shipment_id: str) -> dict:
        """Feasible slots for a dispatcher offer: the SAME feasibility engine the driver conversation uses (dock type,
        weight, reefer, opening hours, capacity, the driver's and truck's other bookings). A recommendation only."""
        self.require(p, "view_dispatch")
        (fac, ref, drv, veh), res, sv, _ = self._dispatch_engine(p, shipment_id)
        return {"shipment_id": shipment_id, "facility_id": fac, "order_reference": ref, "driver_id": drv,
                "vehicle_id": veh, "outcome": res.outcome, "outcome_code": res.outcome_code, "schedule_version": sv,
                "options": [{"dock_code": o.dock, "start": o.start.isoformat(), "end": o.end.isoformat(),
                             "start_label": human_time(o.start), "needs_approval": o.needs_approval}
                            for o in res.options],
                "note": "Options are rechecked when you send the offer and again when the driver accepts."}

    def create_offer(self, p: Principal, shipment_id: str, driver_id: str, vehicle_id: str, dock_code: str,
                     start: datetime, key: str) -> dict:
        """Send a booking offer to a driver (the assigned one: initial booking; another one: pre-confirmation
        reassignment). The slot must be one the engine finds feasible NOW; the database re-checks the shipment, the
        assignment, the driver's conversation and the idempotency key, supersedes any older offer, and delivers the
        offer as a read-back in the driver's conversation. Nothing is held or booked until the driver says yes."""
        self.require(p, "send_offer")
        (fac, ref, cur_drv, cur_veh), res, sv, docks = self._dispatch_engine(p, shipment_id)
        if res.outcome_code == "CONTRADICTORY_DATA":
            raise ServiceError(409, "CONTRADICTORY_DATA", "driver or truck is assigned to an overlapping shipment")
        hit = next((o for o in res.options if o.dock == dock_code and o.start == start), None)
        if hit is None:
            raise ServiceError(409, "OPTION_NOT_FEASIBLE", "that dock and time is not feasible now; reload the options")
        text = (self.OFFER_TEXT if driver_id == cur_drv else self.REASSIGN_TEXT).format(
            ref=ref, dock=hit.dock, start=human_time(hit.start))
        r = self._staff_fn(p, "SELECT create_dispatch_offer(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (shipment_id, driver_id, vehicle_id, docks[hit.dock], hit.start, hit.end, hit.needs_approval,
                            sv, int(self.option_ttl.total_seconds()), text, key, f"staff:{p.sub}"))
        telemetry.emit("dispatch.offer", CORRELATION_ID.get() or str(r["offer_id"]), offer_id=r["offer_id"],
                       kind=r["kind"], facility_id=fac, replayed=r.get("replayed"))
        return r

    def list_offers(self, p: Principal, facility: str | None = None, limit: int = 50) -> list[dict]:
        self.require(p, "view_dispatch")
        with self.db.tx("console", self._scope(p, facility), readonly=True) as c:
            rows = c.execute("""
                SELECT o.offer_id, o.kind, o.shipment_id, o.facility_id, o.driver_id, o.previous_driver_id, o.status,
                       o.status_reason, o.expires_at, o.responded_at, o.appointment_id, o.created_at, o.offered_by,
                       k.dock_code, lower(so.during), a.status, f.state
                  FROM dispatch_offers o JOIN shown_options so ON so.option_id = o.option_id
                  JOIN docks k ON k.dock_id = so.dock_id
                  LEFT JOIN appointments a ON a.appointment_id = o.appointment_id
                  LEFT JOIN thread_flow f ON f.thread_id = o.thread_id
                 ORDER BY o.created_at DESC LIMIT %s""", (max(1, min(limit, 200)),)).fetchall()
        return [{"offer_id": str(r[0]), "kind": r[1], "shipment_id": r[2], "facility_id": r[3], "driver_id": r[4],
                 "previous_driver_id": r[5], "status": self._offer_status(r[6], r[8]), "status_reason": r[7],
                 "expires_at": r[8].isoformat(), "responded_at": r[9].isoformat() if r[9] else None,
                 "appointment_id": r[10], "created_at": r[11].isoformat(), "offered_by": r[12], "dock_code": r[13],
                 "start": r[14].isoformat(), "appointment_status": r[15], "conversation_state": r[16]} for r in rows]

    def issue_link_code(self, p: Principal, driver_id: str, ttl_s: int | None = None) -> dict:
        """A one-time Telegram link code for a driver in this person's scope. The code is returned once (to be passed
        to the driver out of band) and only its hash is stored."""
        from ..notify.telegram import new_link_code
        self.require(p, "link_driver")
        ttl = ttl_s or int(os.environ.get("SETUHAUL_TELEGRAM_LINK_CODE_TTL_S", "900"))
        code, h = new_link_code()
        r = self._staff_fn(p, "SELECT issue_driver_link_code(%s,%s,%s,%s)", (driver_id, h, ttl, f"staff:{p.sub}"))
        return {"driver_id": driver_id, "code": code, "expires_at": r["expires_at"],
                "instructions": "Give this code to the driver; they send /start CODE to the SetuHaul bot. Single use."}

    # ---------------------------------------------------------------- the driver answers an offer (verified channel)
    def _driver_principal(self, drv: str, thread_id: str, facility: str | None) -> Principal:
        return Principal("driver", drv, frozenset({"driver"}), (facility,) if facility else (), driver_id=drv,
                         thread_id=thread_id)

    def _offer_for_driver(self, offer_id: str, drv: str) -> dict:
        try:
            oid = str(uuid.UUID(str(offer_id)))
        except ValueError:
            return {"status": "NOT_FOUND"}
        with self.db.tx("ingest", driver=drv) as c:
            return c.execute("SELECT dispatch_offer_for_driver(%s,%s)", (oid, drv)).fetchone()[0]

    def respond_to_offer(self, drv: str, offer_id: str, action: str, external_id: str, sessions=None,
                         channel: str = "TELEGRAM") -> dict:
        """A driver's button press on an offer, from a channel that has ALREADY verified the driver (never from the
        offer id alone). Accept/decline are stored as the driver's message and attested exactly like a typed YES/NO;
        the database moves the offer. An accepted offer then continues through the existing hold and booking request,
        with every agent-tool guard (booking switch, takeover, kill switch) and planner approval. Idempotent per
        external id: a repeated press reports the current state and does nothing new."""
        if action not in ("ACCEPT", "DECLINE", "ALTERNATIVE"):
            raise ServiceError(400, "BAD_ACTION")
        o = self._offer_for_driver(offer_id, drv)
        if o["status"] == "NOT_FOUND":
            return {"result": "NOT_FOUND"}
        if o["status"] == "OFFERED" and not o.get("is_latest_selection"):
            return {"result": "SUPERSEDED", "offer_id": o["offer_id"]}
        if o["status"] != "OFFERED":
            return {"result": o["status"], "offer_id": o["offer_id"]}
        text = {"ACCEPT": "YES", "DECLINE": "NO", "ALTERNATIVE": "Please offer another time"}[action]
        ev = self.ingest_driver(drv, external_id, text, sessions, channel=channel, agent_turn=False)
        if ev.get("thread_id") != o["thread_id"]:
            return {"result": "SUPERSEDED", "offer_id": o["offer_id"]}
        if ev.get("duplicate"):
            now = self._offer_for_driver(offer_id, drv)
            return {"result": now["status"], "offer_id": o["offer_id"], "duplicate": True}
        if action == "ALTERNATIVE":
            try:
                with self.db.tx("ingest", driver=drv) as c:
                    r = c.execute("SELECT request_offer_alternative(%s,%s,%s)",
                                  (o["offer_id"], drv, ev["message_id"])).fetchone()[0]
                return {"result": r["status"], "offer_id": o["offer_id"]}
            except psycopg.Error:
                now = self._offer_for_driver(offer_id, drv)
                return {"result": now["status"], "offer_id": o["offer_id"]}
        after = self._offer_for_driver(offer_id, drv)
        if action == "DECLINE" or after["status"] != "ACCEPTED":
            return {"result": after["status"], "offer_id": o["offer_id"]}
        # accepted: the existing booking path, as the driver, in their own conversation
        with self.db.tx("ingest", driver=drv, readonly=True) as c:
            fac = c.execute("SELECT destination_facility_id FROM shipments WHERE shipment_id=%s",
                            (after["shipment_id"],)).fetchone()
        p = self._driver_principal(drv, o["thread_id"], fac[0] if fac else None)
        mid, steps = ev["message_id"], {}
        steps["confirm"] = self.call_tool(p, "confirm_selection", mid, {"message_id": mid})
        if steps["confirm"]["status"] != "OK":
            return {"result": "ACCEPTED", "offer_id": o["offer_id"], "booking": steps}
        steps["hold"] = self.call_tool(p, "hold_option", mid, {"option_id": o["option_id"]})
        if steps["hold"]["status"] != "OK":
            log.warning("accepted offer %s could not be held: %s %s", o["offer_id"], steps["hold"]["status"],
                        (steps["hold"].get("data") or {}).get("code"))
            res = "SLOT_TAKEN" if steps["hold"]["status"] in ("LOST", "OPTION_EXPIRED") else "ACCEPTED"
            return {"result": res, "offer_id": o["offer_id"], "booking": steps}
        steps["submit"] = self.call_tool(p, "submit_booking_request", mid, {"option_id": o["option_id"]})
        ok = steps["submit"]["status"] == "OK"
        return {"result": "SUBMITTED" if ok else "ACCEPTED", "offer_id": o["offer_id"], "booking": steps,
                "appointment_id": steps["submit"]["data"].get("appointment_id") if ok else None}

    # ---------------------------------------------------------------- roadmap Phase 4: driver location (0035)
    def record_location(self, drv: str, report, dedupe_key: str, source: str = "TELEGRAM", config=None) -> dict:
        """An explicitly shared, one-off location from a verified channel. Assessed deterministically
        (domain/location.py), stored coarsely with its reasons, linked to the driver's open delay report. It changes
        no ETA, booking or approval; a driver who declines still has their delay report."""
        from ..domain.location import JourneyContext, LocationConfig, assess
        with self.db.tx("ingest", driver=drv, readonly=True) as c:
            th = c.execute("""SELECT thread_id FROM chat_threads WHERE driver_id=%s AND status NOT IN ('CLOSED','RESOLVED')
                               ORDER BY opened_at DESC LIMIT 1""", (drv,)).fetchone()
        if not th:
            return {"assessment": "UNAVAILABLE", "reasons": ["NO_CONVERSATION"], "stored": False}
        with self.db.tx("ingest", driver=drv) as c:
            ctx = c.execute("SELECT driver_location_context(%s,%s)", (drv, th[0])).fetchone()[0]

        def ts(v):
            return datetime.fromisoformat(v) if isinstance(v, str) else v
        fpos = ((ctx["facility_latitude"], ctx["facility_longitude"])
                if ctx.get("facility_latitude") is not None else None)
        jc = JourneyContext(has_shipment=ctx.get("shipment_id") is not None, facility_position=fpos,
                            declared_eta=ts(ctx.get("declared_eta")), previous_geohash=ctx.get("previous_geohash"),
                            previous_observed_at=ts(ctx.get("previous_observed_at")))
        a = assess(report, jc, datetime.now(timezone.utc), config or LocationConfig.from_env())
        with self.db.tx("ingest", driver=drv) as c:
            r = c.execute("SELECT record_driver_location(%s,%s,%s,%s)",
                          (drv, th[0], dedupe_key, Jsonb(a.as_record(source)))).fetchone()[0]
        return {**r, "stored": True}

    # ---------------------------------------------------------------- roadmap Phase 5: Telegram inbound (0035)
    def handle_telegram_update(self, update: dict, sessions) -> dict:
        """One verified Telegram update -> at most one backend action. Returns {"reply": text | None, "event": queue
        event | None}. An unlinked account learns nothing and changes nothing (beyond redeeming a valid code)."""
        from ..domain.location import LocationReport
        from ..notify import telegram as tg
        ev = tg.parse_update(update)
        if ev.kind == "ignored" or ev.user_id is None:
            return {"reply": None, "event": None}
        if ev.kind == "start" and ev.code:
            with self.db.tx("ingest") as c:
                r = c.execute("SELECT redeem_driver_link_code(%s,%s,%s)",
                              (tg.hash_code(ev.code), ev.user_id, ev.chat_id)).fetchone()[0]
            if r["linked"]:
                return {"reply": tg.TEXT_LINKED, "event": None, "linked": True}
            return {"reply": {"ACCOUNT_ALREADY_LINKED": tg.TEXT_ACCOUNT_TAKEN,
                              "DRIVER_ALREADY_LINKED": tg.TEXT_DRIVER_TAKEN}.get(r["reason"], tg.TEXT_LINK_FAILED),
                    "event": None, "linked": False}
        with self.db.tx("ingest", readonly=True) as c:
            drv = c.execute("SELECT driver_for_channel('TELEGRAM', %s)", (ev.user_id,)).fetchone()[0]
        if drv is None:
            return {"reply": tg.TEXT_NOT_LINKED, "event": None}
        if ev.kind == "start":
            return {"reply": tg.TEXT_LINKED, "event": None}
        if ev.kind == "callback":
            r = self.respond_to_offer(drv, ev.offer_id, ev.action, ev.external_id, sessions)
            return {"reply": tg.OFFER_REPLIES.get(r["result"], tg.OFFER_REPLIES["ACCEPTED"]), "event": None, "result": r}
        if ev.kind == "location":
            r = self.record_location(drv, LocationReport(True, ev.latitude, ev.longitude, ev.observed_at, ev.accuracy_m),
                                     ev.external_id)
            return {"reply": tg.TEXT_LOCATION.get(r["assessment"]), "event": None, "result": r}
        e = self.ingest_driver(drv, ev.external_id, ev.text, sessions, channel="TELEGRAM")
        return {"reply": None, "event": e if "session_token" in e else None}
