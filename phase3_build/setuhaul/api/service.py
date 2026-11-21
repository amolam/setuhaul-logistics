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
Responses use the tool envelope from `phase2_design/04_tool_contracts_and_states.md` §1.
"""
from __future__ import annotations

import hashlib
import logging
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
from ..domain.feasibility import FeasibilityEngine, Option
from .db import Db
from .security import Principal

log = logging.getLogger("setuhaul.service")
IST = timezone(timedelta(hours=5, minutes=30))
OPTION_TTL = timedelta(minutes=5)
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


BOOKING_TOOLS = {"hold_option", "submit_booking_request"}
TOOLS = {"get_driver_context": None, "record_driver_report": DriverReport, "find_feasible_options": FindArgs,
         "select_option": OptionArg, "confirm_selection": ConfirmArgs, "hold_option": OptionArg,
         "submit_booking_request": OptionArg, "release_hold": OptionArg, "get_booking_status": StatusArgs,
         "escalate_to_human": EscalateArgs}


class BookingService:
    def __init__(self, db: Db, clock: Callable[[], datetime], facilities: tuple[str, ...], hold_ttl_s: int = 180,
                 booking_enabled: bool = False):
        self.db, self.clock, self.facilities, self.hold_ttl = db, clock, tuple(facilities), hold_ttl_s
        # Production booking switch (review R0.8): OFF by default. Until durable inbound delivery (step 4) is in
        # place, the agent may understand, find options and read back, but not hold or request a slot.
        self.booking_enabled = booking_enabled

    def now(self) -> datetime:
        return self.clock().astimezone(IST)

    # ---------------------------------------------------------------- shared helpers
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
            for oid, dock, during, appr, exp in c.execute(
                    """SELECT o.option_id, d.dock_code, o.during, o.needs_approval, o.expires_at
                         FROM shown_options o JOIN docks d USING (dock_id)
                        WHERE o.thread_id=%s AND o.created_at=%s ORDER BY o.rank""", (p.thread_id, last)):
                f.shown[str(oid)] = {"dock_code": dock, "start": during.lower, "end": during.upper,
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
                flow["options"] = [{"option_id": str(o), "dock_code": dc, "start": human_time(d.lower), "end": human_time(d.upper),
                                    "start_ts": d.lower.isoformat(), "needs_approval": na}
                                   for o, dc, d, na in c.execute(
                                       """SELECT o.option_id, k.dock_code, o.during, o.needs_approval
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

    def _publish(self, p, sid, fac, res):
        out, shown = [], {}
        with self.db.tx("feasibility", (fac,)) as c:                    # options are the service's decision
            sv = c.execute("SELECT schedule_version FROM facilities WHERE facility_id=%s", (fac,)).fetchone()[0]
            docks = dict(c.execute("SELECT dock_code, dock_id FROM docks WHERE facility_id=%s", (fac,)).fetchall())
            for rank, o in enumerate(res.options, 1):
                oid = uuid.uuid4()
                c.execute("INSERT INTO shown_options VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()+%s,now())",
                          (oid, p.thread_id, sid, docks[o.dock], Range(o.start, o.end, "[)"), o.needs_approval, rank, sv, OPTION_TTL))
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

    # ---------------------------------------------------------------- console (people decide)
    @staticmethod
    def require(p: Principal, action: str):
        if not (p.roles & PERMISSIONS[action]):
            raise ServiceError(403, "FORBIDDEN", f"{action} needs one of: {', '.join(sorted(PERMISSIONS[action]))}")

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

    def decide(self, p: Principal, appointment_id: str, decision: str, note: str | None, key: str) -> dict:
        self.require(p, "decide")
        with self.db.tx("console", p.facilities, readonly=True) as c:
            row = c.execute("""SELECT a.shipment_id, d.dock_code, d.facility_id, lower(a.during), upper(a.during), a.status
                                 FROM appointments a JOIN docks d USING (dock_id) WHERE a.appointment_id=%s""",
                            (appointment_id,)).fetchone()
        if not row:
            raise ServiceError(404, "APPOINTMENT_UNKNOWN")
        sid, dock, fac, start, end, status = row
        if decision == "CONFIRM" and status == "PENDING_CONFIRMATION":
            # advisory (B-WF-2 §5): a clear early message; the database function re-proves every invariant itself
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
        drv = r[0]
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
            if len(norm.split()) > 3 and c.execute(
                    """SELECT 1 FROM chat_messages WHERE thread_id=%s AND sender_type='DRIVER' AND content_hash=%s
                         AND external_message_id IS DISTINCT FROM %s AND created_at > now() - interval '2 minutes'""",
                    (th[0], digest, external_id)).fetchone():
                log.info("same text redelivered under a new id within 2 minutes: treated as a duplicate")
                return {"duplicate": True, "content_duplicate": True, "thread_id": th[0], "external_message_id": external_id}
            m = c.execute("""INSERT INTO chat_messages (thread_id,sender_type,channel,external_message_id,content_hash,body_masked)
                             VALUES (%s,'DRIVER','WHATSAPP',%s,%s,%s) ON CONFLICT (channel, external_message_id) DO NOTHING
                             RETURNING message_id""",
                          (th[0], external_id, digest, masked)).fetchone()
            duplicate = m is None
            if duplicate:                                   # provider retry: find what was stored the first time
                row = c.execute("""SELECT m.message_id, m.thread_id, m.body_masked, t.shipment_id
                                     FROM chat_messages m JOIN chat_threads t USING (thread_id)
                                    WHERE m.channel='WHATSAPP' AND m.external_message_id=%s""", (external_id,)).fetchone()
                if not row:
                    return {"duplicate": True, "external_message_id": external_id}
                mid, th, masked = row[0], (row[1], row[3]), row[2]
            else:
                mid = m[0]
                c.execute("INSERT INTO inbound_receipts (message_id, thread_id) VALUES (%s,%s)", (mid, th[0]))
            rec = c.execute("SELECT attestation_status, enqueued FROM inbound_receipts WHERE message_id=%s", (mid,)).fetchone()
            fac = None
            if th[1]:
                fac = c.execute("SELECT destination_facility_id FROM shipments WHERE shipment_id=%s", (th[1],)).fetchone()[0]
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
            else:
                claimed = c.execute("""UPDATE inbound_receipts SET enqueued=true, enqueued_at=now(), updated_at=now()
                                        WHERE message_id=%s AND NOT enqueued RETURNING generation""", (mid,)).fetchone()
        if not claimed:
            return {"duplicate": True, "thread_id": th[0], "external_message_id": external_id}
        # The caller (InboundRelay) publishes this event and then marks the receipt published. If it crashes in
        # between, recover_inbound() finds the receipt and publishes it again (B2-14).
        return {"thread_id": th[0], "message_id": mid, "external_message_id": external_id, "driver_id": drv,
                "generation": claimed[0], "session_token": sessions.mint(drv, th[0], fac), "attestation_status": status,
                "recovered": recovered}

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

    # ---------------------------------------------------------------- S4-04: migration 0011 cutover (RUNBOOK_0011.md)
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
