"""One driver turn, as a fixed pipeline (05_agent_observability_cost.md §1.1):

    inbound event → [model] extract → code checks → code decides the next tool → tools (BookingService)
                  → reply: deterministic template (en / hi / hi-Latn) → store reply

The model is used in exactly one place, extraction, and decides nothing. Since owner decision OD-3 no model writes
any reply: option lists are templates like every other reply (the old phrasing loop survives only in the B-AI-3
ablation, evals/phrase_ablation.py). Every booking step goes through the service, the
booking flow gate and the database functions. If the model fails, the rules extractor and templates take over
(degraded mode, F5). Any tool result that needs a person becomes a durable escalation BEFORE the driver is told a
coordinator will help (review R0.9, B2-18); if that escalation fails, the driver is not promised a coordinator.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import ValidationError

from ..api.db import TURN_FENCE
from ..api.security import Principal, SessionTokens
from ..api.service import BookingService
from ..domain.booking import classify_confirmation
from . import replies, rules_extractor
from .extraction import SAFE_PLACE, Checked, Extraction, check, kind_stated
from .models import ModelClient, ModelError, Trace, with_retry

log = logging.getLogger("setuhaul.agent")
IST = timezone(timedelta(hours=5, minutes=30))
ESCALATION_REASON = {           # requires_human.reason_code → escalation reason the agent may use
    "CONTRADICTORY_DATA": "CONTRADICTORY_DATA", "NO_FEASIBLE_SLOT": "NO_FEASIBLE_SLOT", "SAFETY": "SAFETY",
    "BOOKING_DISABLED": "DRIVER_REQUEST", "NO_CONFIRMER_AVAILABLE": "DRIVER_REQUEST",
    "AUTOMATION_PAUSED": "DRIVER_REQUEST", "REQUIRES_HUMAN": "DRIVER_REQUEST", "BREAKDOWN": "BREAKDOWN",
}


class TurnLeaseLost(RuntimeError):
    """The worker no longer held a live lease when it tried to finish: the message stays unprocessed."""


@dataclass
class TurnResult:
    reply: str | None
    path: list[str] = field(default_factory=list)              # what code decided, in order (for traces and tests)
    tools: list[tuple[str, str]] = field(default_factory=list)  # (tool, status)
    traces: list[Trace] = field(default_factory=list)
    extractor: str = "model"                                    # model | rules-fallback
    escalation_id: str | None = None
    extraction: dict | None = None                              # what the model said, after merging earlier turns
    fallback_reason: str | None = None                          # why the rules reader was used (T52: budget, timeout...)


_NUMBER_ANSWER = re.compile(r"^\s*(?:no\.?\s*)?([1-9])\s*(?:number|no\.?|wala|vala|वाला|नंबर)?\s*[.!]?\s*$", re.I)


def _bare_number(text: str) -> int | None:
    """A reply that is only a number ("2", "2 number", "no. 2"): the answer to a numbered question."""
    m = _NUMBER_ANSWER.match(text or "")
    return int(m.group(1)) if m else None


class Orchestrator:
    def __init__(self, service: BookingService, sessions: SessionTokens, model: ModelClient | None,
                 clock: Callable[[], datetime] | None = None, lease_s: int = 120,
                 on_heartbeat: Callable[[dict], None] | None = None):
        self.svc, self.sessions, self.model = service, sessions, model
        self.lease_s = lease_s            # renewed every lease_s / 3 while the turn runs (S4-02)
        self.on_heartbeat = on_heartbeat  # e.g. extend the SQS message's visibility timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- entry point
    def handle(self, event: dict) -> TurnResult | None:
        """One turn per inbound message, however often the queue delivers it (B-WF-1, R1.9 S4-02/S4-03).

        * The processing lease is claimed for the event's generation; an older generation is refused.
        * While the turn runs, a heartbeat renews the lease (and, through `on_heartbeat`, the queue visibility).
        * Every write the turn makes is fenced (`db.TURN_FENCE`): once this worker loses the lease, to expiry, a
          take-over or a new generation, its next write fails and the turn stops without marking anything processed.
        * A failed turn hands the message back for redelivery. Returns None for a duplicate delivery."""
        p = self.sessions.verify(event["session_token"])
        mid, gen = int(event["message_id"]), int(event.get("generation", 0))
        owner = f"orch:{uuid.uuid4().hex[:12]}"
        if not self.svc.claim_turn(p, mid, owner, self.lease_s, gen):
            return None
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(self.lease_s / 3):
                try:
                    if not self.svc.renew_turn(p, mid, owner, self.lease_s, gen):
                        log.warning("lost the lease for message %s; further writes are fenced", mid)
                        return
                    if self.on_heartbeat:
                        self.on_heartbeat(event)
                except Exception:                                       # noqa: BLE001 — the fence still holds
                    log.exception("lease renewal failed for message %s", mid)
        hb = threading.Thread(target=heartbeat, daemon=True)
        token = TURN_FENCE.set((mid, owner, gen))
        hb.start()
        try:
            res = self._turn(p, event)
            if not self.svc.finish_turn(p, mid, owner, gen):            # R2.0 S4-R2-01: not a successful turn
                raise TurnLeaseLost(f"message {mid}: the lease was lost before the turn finished; it will be redelivered")
        except BaseException:
            try:
                self.svc.release_turn(p, mid, owner)
            except Exception:                                           # noqa: BLE001
                log.exception("could not release message %s", mid)
            raise
        finally:
            stop.set()
            TURN_FENCE.reset(token)
            hb.join(5)
        return res

    def _turn(self, p, event: dict) -> TurnResult:
        turn = int(event["message_id"])
        res = TurnResult(reply=None)

        def tool(name, **args):
            r = self.svc.call_tool(p, name, turn, args)
            res.tools.append((name, r["status"]))
            return r

        ctx = tool("get_driver_context")
        if ctx["status"] == "REQUIRES_HUMAN":                       # a person owns the conversation: stay silent
            res.path.append("human-owns-thread")
            return res
        text = self.svc.read_driver_message(p, turn) or ""
        flow, data = ctx["data"]["flow"], ctx["data"]
        shown = [o["option_id"] for o in flow["options"]]
        ex = self._extract(text, data, res)
        # B-AI-3: answer in the conversation's language, not the last message's (a one-word "हाँ" or "yes")
        earlier = [b for mid, b in data.get("recent_driver_messages", []) if mid < turn]
        lang = replies.reply_language(text, ex.language, earlier)

        # Earlier turns' facts come from the database (migration 0008), never from the model's memory.
        # Facts are cleared whenever an option is selected, so any stored facts in SELECTED come from a constraint question
        # asked after the selection (S3-01) and must be used.
        tf = data.get("turn_facts") if flow["state"] != "HUMAN" else None
        stored, asked = (tf["facts"], tf["asked"]) if tf else ({}, None)
        safe_answer = None
        if asked == "safety_status":                                 # OD-2: "are you in a safe place?" was asked
            safe_answer, stored = classify_confirmation(text), {}   # the breakdown is already escalated: do not re-merge
        own_eta = bool(ex.declared_eta_local or ex.declared_eta_range_local or ex.declared_delay_min is not None)
        verdict = classify_confirmation(text) if asked == "delay_kind" and stored.get("candidate_eta") and not own_eta else None
        ex = self._merge(ex, stored, keep_eta=verdict not in ("YES", "NO"), answering=asked is not None)
        if asked == "latest_finish_kind" and ex.latest_finish_kind in (None, "UNKNOWN"):
            # The answer to our own question ("Gate-out.") often carries no time, and the model then leaves the kind
            # empty (G3 v3 run, BB10). Read it from the driver's words with the same check that validates it.
            k = next((k for k in ("GATE_OUT", "UNLOAD_START", "UNLOAD_END") if kind_stated(k, text)), None)
            if k:
                ex = ex.model_copy(update={"latest_finish_kind": k})
        if asked == "shipment_id" and not ex.shipment_ref_mentioned and (n := _bare_number(text)):
            # S4-R2-08: the question numbers the orders (in the order data["live_shipments"] has, by shipment id), so
            # "1" or "2 number" names that order. It is not a slot pick, so any option the extractor read is dropped.
            ships = data["live_shipments"]
            if 1 <= n <= len(ships):
                ex = ex.model_copy(update={"shipment_ref_mentioned": ships[n - 1]["order_reference"],
                                           "selected_option_id": None})
                res.path.append("shipment-by-number")
        res.extraction = ex.model_dump()
        chk = check(ex, self.clock(), self._base_eta(data, ex.shipment_ref_mentioned), text, shown)
        if verdict == "YES":                                         # "yes, around 11:00": the computed time stands
            chk.declared_eta, chk.eta_precision = datetime.fromisoformat(stored["candidate_eta"]), "VAGUE"
            chk.missing = [m for m in chk.missing if m not in ("declared_eta_local", "delay_kind")]
            res.path.append("eta-confirmed")
            if chk.breakdown in ("UNREPAIRED_SAFE", "UNREPAIRED_UNKNOWN") and ex.vehicle_fixed is not False and \
                    stored.get("delay_kind") != "REPAIR_DURATION":
                chk.breakdown = "REPAIRED"                           # G3 replay L08: "late by 4 h, now fixed" + "yes"
        elif verdict == "NO":                                        # the reading was wrong: ask for the time itself
            chk.missing = ["declared_eta_local"]
        res.reply = self._safety_answer(safe_answer, chk, lang, res, text) if asked == "safety_status" else None
        if chk.safety and tf:                                        # forget facts before a person takes the thread
            self._forget(p, turn)
        if res.reply is None:
            res.reply = self._decide(p, chk, flow, data, lang, tool, res, turn)
        self._remember(p, turn, ex, chk, res)
        if res.reply:                                                # S4-08: one reply per message and generation
            self.svc.record_reply(p, res.reply, reply_to=turn, generation=int(event.get("generation", 0)))
        return res

    def _forget(self, p: Principal, turn: int) -> None:
        try:
            self.svc.note_turn_facts(p, turn, None)
        except Exception as e:
            log.warning("could not clear turn facts: %s", e)

    @staticmethod
    def _safety_answer(answer: str | None, c: Checked, lang: str, res: TurnResult, text: str = "") -> str | None:
        """OD-2: the driver's answer to "are you and the truck in a safe place?". NO, or any danger words, is a safety
        takeover (handled by _decide). YES is acknowledged. Anything else is asked again, without a second escalation,
        unless the message is a new report (the truck is moving again, or a new ETA)."""
        if answer == "NO":
            c.safety = True
            return None
        if c.safety:
            return None
        if answer == "YES" or c.breakdown == "UNREPAIRED_SAFE" or SAFE_PLACE.search(text or ""):   # "haan safe hai"
            res.path.append("safe-confirmed")
            return replies.t("safe_ack", lang)
        if c.breakdown == "REPAIRED" or c.declared_eta or c.candidate_eta or c.intent == "CHECK_STATUS":
            return None
        res.path.append("ask:safety_status")
        return replies.t("ask_safe", lang)

    # ---------------------------------------------------------------- memory across turns (G3 finding 4)
    _ETA_FIELDS = ("delay_kind", "declared_eta_local", "declared_eta_range_local", "declared_delay_min")
    _CARRIED = _ETA_FIELDS + ("shipment_ref_mentioned", "earliest_start_local", "latest_finish_local",
                              "latest_finish_kind", "exception_type")

    @classmethod
    def _merge(cls, ex: Extraction, stored: dict, keep_eta: bool = True, answering: bool = False) -> Extraction:
        """Fill what this message did not say from what the driver said before. This turn always wins."""
        if not stored:
            return ex
        upd = {}
        own_eta = bool(ex.declared_eta_local or ex.declared_eta_range_local or ex.declared_delay_min is not None)
        for k in cls._CARRIED:
            if k in cls._ETA_FIELDS and (own_eta or not keep_eta):
                continue
            mine, theirs = getattr(ex, k), stored.get(k)
            if theirs is not None and mine in (None, "UNKNOWN"):
                upd[k] = theirs
        continues = ex.intent in ("OTHER", "BOOK") or (answering and ex.intent not in ("CHECK_STATUS", "CANCEL"))
        if continues and not ex.selected_option_id and not own_eta and \
                stored.get("intent") in ("REPORT_DELAY", "ASK_OPTIONS", "EARLY_ARRIVAL"):
            upd["intent"] = stored["intent"]                         # an answer to our question continues the request
        return ex.model_copy(update=upd) if upd else ex

    def _remember(self, p: Principal, turn: int, ex: Extraction, c: Checked, res: TurnResult) -> None:
        """After a question, keep the unresolved facts; once the report is acted on, forget them."""
        last = res.path[-1] if res.path else ""
        if "safety" in res.path or "human-owns-thread" in res.path:
            return                                                   # a person owns the thread now
        try:
            if last.startswith("ask:") or last == "ask-shipment":
                facts = {k: getattr(ex, k) for k in self._CARRIED + ("intent",)
                         if getattr(ex, k) not in (None, "UNKNOWN")}
                if facts.get("intent") == "BOOK":                    # a pick held back by a question: re-offer after it
                    facts["intent"] = "ASK_OPTIONS"
                if c.candidate_eta:
                    facts["candidate_eta"] = c.candidate_eta.isoformat()
                self.svc.note_turn_facts(p, turn, facts, "shipment_id" if last == "ask-shipment" else last[4:])
            elif any(x in ("options", "keep-current", "book", "select", "safe-confirmed") or x.startswith("escalated")
                     for x in res.path):
                self.svc.note_turn_facts(p, turn, None)
        except Exception as e:                                       # memory is a convenience, never a reason to fail
            log.warning("could not store turn facts: %s", e)

    # ---------------------------------------------------------------- understanding
    def _extract(self, text: str, data: dict, res: TurnResult) -> Extraction:
        flow = data["flow"]
        if self.model is not None:
            try:
                raw, tr = with_retry(lambda: self.model.extract(self._user_block(text, data)))
                res.traces.append(tr)
                return Extraction(**raw)
            except (ModelError, ValidationError, TypeError) as e:          # model down or off-contract → rules
                res.fallback_reason = getattr(e, "category", None) or (
                    "invalid_schema" if isinstance(e, (ValidationError, TypeError)) else "model_error")
                log.warning("extraction fell back to rules (%s)", res.fallback_reason)
        res.extractor = "rules-fallback" if self.model is not None else "rules"
        tf = data.get("turn_facts") if flow["state"] != "HUMAN" else None
        return rules_extractor.extract(text, [o["option_id"] for o in flow["options"]],
                                       awaiting_confirmation=flow["state"] == "SELECTED",
                                       asked=tf.get("asked") if tf else None)      # H-1: bare time answers

    def _user_block(self, text: str, data: dict) -> str:
        """Per-turn context for the extractor. No phone numbers, no coordinates, no other drivers."""
        now = self.clock().astimezone(IST)
        lines = [f"Now (India time): {now:%Y-%m-%d %H:%M}"]
        for s in data["live_shipments"]:
            ca = s["current_appointment"]
            lines.append(f"Shipment {s['order_reference']} ({s['shipment_id']}), current slot: "
                         f"{ca['dock_code'] + ' ' + ca['start'] + ' ' + ca['status'] if ca else 'none'}")
        flow = data["flow"]
        for i, o in enumerate(flow["options"], 1):
            lines.append(f"Option {i}: option_id={o['option_id']} dock {o['dock_code']} at {o['start']}")
        if flow["state"] == "SELECTED":
            lines.append(f"Read back and awaiting a yes for option_id={flow['selected_option']}")
        # H-5 review: the coordinator's note is NOT in this block. Extraction reads only the driver's own words, because its
        # output drives the tool calls. Since OD-3 no model writes the reply either, so the note reaches no model at all.
        lines.append(f"Driver message: {text}")
        return "\n".join(lines)

    @classmethod
    def _base_eta(cls, data: dict, ref: str | None = None) -> datetime | None:
        sid = cls._resolve_ref(ref, data) or data.get("thread_shipment_id")
        ships = data["live_shipments"]
        s = next((x for x in ships if x["shipment_id"] == sid), ships[0] if len(ships) == 1 else None)
        return datetime.fromisoformat(s["planned_eta"]) if s else None

    # ---------------------------------------------------------------- deciding (code, not the model)
    def _decide(self, p: Principal, c: Checked, flow: dict, data: dict, lang: str, tool, res: TurnResult, turn: int):
        state = flow["state"]
        if c.safety:
            res.path.append("safety")
            r = tool("record_driver_report", message_id=turn, safety_concern=True, delay_reason="ACCIDENT")
            return self._human(r, lang, tool, res, "(safety report)", default_reason="SAFETY")

        if c.breakdown in ("UNREPAIRED_SAFE", "UNREPAIRED_UNKNOWN"):    # OD-2: operational escalation, no takeover
            return self._breakdown(c, data, lang, tool, res, turn)

        if not data["live_shipments"]:                              # G3 replay THR012 / AT10: nothing to book
            res.path.append("no-live-shipment")
            ts = data.get("thread_shipment") or {}
            if ts.get("status") == "CANCELLED":
                return replies.t("cancelled", lang, ref=ts["order_reference"])
            if data.get("cancelled_shipments"):                     # G3 replay AT10: say WHY there is nothing
                return replies.t("cancelled", lang, ref=", ".join(data["cancelled_shipments"]))
            return replies.t("no_live", lang)

        if c.shipment_ref and not self._resolve_ref(c.shipment_ref, data):   # G3 replay ADV03: not this driver's
            res.path.append("refused")
            return replies.t("not_yours", lang, refs=self._refs(data))

        # Review S3-01: a message that brings a new time constraint never selects or confirms in the same turn. The
        # constraint is resolved first (asked if ambiguous), then options are re-offered under it.
        new_constraint = bool(c.earliest_start or c.latest_finish or "latest_finish_kind" in c.missing)
        if new_constraint and (state == "SELECTED" or c.selected_option_id):
            res.path.append("constraint-before-select")
            if c.missing:
                res.path.append(f"ask:{c.missing[0]}")
                return replies.ask(c.missing[0], lang, refs=self._refs(data))
            return self._options_turn(c.earliest_start, c.latest_finish, lang, tool, res, data)

        if state == "SELECTED":                                     # a read-back is waiting for an answer
            res.path.append("confirmation")
            r = tool("confirm_selection", message_id=turn)
            verdict = r["data"].get("verdict")
            if verdict == "YES":
                return self._book(flow["selected_option"], lang, tool, res)
            if verdict == "NO":
                return replies.options_text(flow["options"], lang) if flow["options"] else replies.t("ack", lang)
            return replies.t("ask_yes", lang)

        if c.selected_option_id and state in ("OPTIONS_SHOWN", "CONFIRMED_BY_DRIVER"):
            res.path.append("select")
            r = tool("select_option", option_id=c.selected_option_id)
            if r["status"] == "OK":
                return r["data"]["read_back"]                       # exact read-back text the database recorded
            return self._options_turn(None, None, lang, tool, res, data)

        if c.intent == "CHECK_STATUS":
            res.path.append("status")
            named = self._resolve_ref(c.shipment_ref, data)        # live G3 replay AT03: use the order they named
            r = tool("get_booking_status", **({"shipment_id": named} if named else {}))
            if r["status"] != "OK":
                res.path.append("ask-shipment")                     # remembered, so a numbered answer is understood
                return replies.ask("shipment_id", lang, refs=self._refs(data))
            appts = r["data"]["appointments"]                    # newest first; a pending request is not "current"
            a = next((x for x in appts if x["status"] == "PENDING_CONFIRMATION"), None) or \
                next((x for x in appts if x["current"]), None)
            if a:
                return replies.t("status", lang, shipment_id=r["data"]["shipment_id"], dock_code=a["dock_code"],
                                 start=a["start"], status=a["status"])
            # G3 replay AT02 / THR008: no valid booking on a live shipment (e.g. one the database refused) is not
            # a dead end: say so, then offer slots.
            none = replies.t("status_none", lang, shipment_id=r["data"]["shipment_id"])
            return none + "\n" + self._options_turn(None, None, lang, tool, res, data)

        if c.intent == "CANCEL":
            res.path.append("cancel")
            if state == "HELD" and flow.get("selected_option"):
                tool("release_hold", option_id=flow["selected_option"])
            return replies.t("ack", lang)

        if data["needs_shipment_choice"] and not c.shipment_ref:
            res.path.append("ask-shipment")
            return replies.ask("shipment_id", lang, refs=self._refs(data))

        if c.missing:
            res.path.append(f"ask:{c.missing[0]}")
            if c.declared_eta or c.delay_reason:                     # record what we do know first
                tool("record_driver_report", **self._report_args(c, turn, data))
            if c.missing[0] == "delay_kind" and c.candidate_eta:     # propose the computed time, one question
                return replies.t("confirm_eta", lang, eta=self._clock_text(c.candidate_eta))
            return replies.ask(c.missing[0], lang, refs=self._refs(data))

        if c.intent in ("REPORT_DELAY", "ASK_OPTIONS", "EARLY_ARRIVAL", "BOOK"):
            if c.declared_eta or c.delay_reason:
                r = tool("record_driver_report", **self._report_args(c, turn, data))
                if r["status"] == "REQUIRES_HUMAN":
                    return self._human(r, lang, tool, res, "(delay report)")
                if r["status"] not in ("OK", "NEEDS_INFO"):
                    res.path.append(f"report:{r['status']}")
                keep = self._keep_current(r, c, data, lang, res)
                if keep:
                    return keep
            return self._options_turn(c.earliest_start, c.latest_finish, lang, tool, res, data)

        res.path.append("unclear")
        return replies.t("unclear", lang)

    def _breakdown(self, c: Checked, data: dict, lang: str, tool, res: TurnResult, turn: int) -> str:
        """OD-2: a truck still broken down. A durable BREAKDOWN escalation in DECIDE mode: a coordinator helps, but the
        conversation stays with the agent (no takeover). If the driver did not say they are somewhere safe, ask once;
        a NO, or any danger words, then becomes a safety takeover. The coordinator is promised only once the escalation
        row exists (B2-18)."""
        res.path.append("breakdown")
        if not data["needs_shipment_choice"] or c.shipment_ref:
            tool("record_driver_report", **self._report_args(c, turn, data))
        told = replies.t("breakdown", lang, sla=10)
        e = tool("escalate_to_human", reason_code="BREAKDOWN", mode="DECIDE",
                 driver_last_message="(breakdown report)", what_driver_was_told=told[:500])
        ask = c.breakdown == "UNREPAIRED_UNKNOWN"
        if e["status"] != "OK":
            res.path.append("escalation-failed")
            if ask:
                res.path.append("ask:safety_status")
                return replies.t("ask_safe", lang)
            return replies.t("unclear", lang) if lang != "en" else \
                "Sorry, I could not arrange this right now. Please call your dispatcher."
        res.escalation_id = str(e["data"]["escalation_id"])
        res.path.append("escalated:BREAKDOWN")
        if ask:
            res.path.append("ask:safety_status")
            return told + "\n" + replies.t("ask_safe", lang)
        return told

    @staticmethod
    def _keep_current(r: dict, c: Checked, data: dict, lang: str, res: TurnResult) -> str | None:
        """G3 finding 2: a delay that still fits the current slot changes nothing (`appointment_at_risk` false, as
        computed by the feasibility engine). Asking for options, or giving a time window, still gets options."""
        if r["status"] != "OK" or r["data"].get("appointment_at_risk") is not False:
            return None
        if c.intent == "ASK_OPTIONS" or c.earliest_start or c.latest_finish:
            return None
        sid = r["data"].get("shipment_id")
        cur = next((s["current_appointment"] for s in data["live_shipments"] if s["shipment_id"] == sid), None)
        if not cur:
            return None
        res.path.append("keep-current")
        return replies.t("keep", lang, dock_code=cur["dock_code"], start=cur["start"], eta=r["data"]["effective_eta"])

    def _clock_text(self, ts: datetime) -> str:
        t = ts.astimezone(IST)
        s = f"{(t.hour - 1) % 12 + 1}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
        return s if t.date() == self.clock().astimezone(IST).date() else f"{s} on {t.day} {t:%b}"

    def _report_args(self, c: Checked, turn: int, data: dict) -> dict:
        args = {"message_id": turn}
        ship = self._resolve_ref(c.shipment_ref, data)
        if ship:
            args["shipment_id"] = ship
        if c.declared_eta:
            args.update(declared_eta=c.declared_eta.isoformat(), eta_precision=c.eta_precision)
        if c.delay_reason:
            args["delay_reason"] = c.delay_reason
        return args

    @staticmethod
    def _resolve_ref(ref: str | None, data: dict) -> str | None:
        if not ref:
            return None
        for s in data["live_shipments"]:
            if ref.upper() in (s["shipment_id"].upper(), s["order_reference"].upper()):
                return s["shipment_id"]
        return None

    @staticmethod
    def _refs(data: dict) -> str:
        return ", ".join(s["order_reference"] for s in data["live_shipments"])

    def _options_turn(self, es, lf, lang, tool, res: TurnResult, data: dict) -> str:
        res.path.append("options")
        args = {}
        if es:
            args["earliest_start"] = es.isoformat()
        if lf:
            args["latest_finish"] = lf.isoformat()
        r = tool("find_feasible_options", **args)
        if r["status"] == "ERROR" and r["data"].get("code") == "STALE_SHIPMENT":
            res.path.append("stale-shipment-retry")       # B2-17: another turn moved the conversation; plan for its shipment
            r = tool("find_feasible_options", **args)
        if r["status"] == "OK":
            return self._options_reply(r["data"]["options"], lang, res)
        if r.get("requires_human"):
            return self._human(r, lang, tool, res, "(asked for a slot)")
        return replies.t("no_slot", lang)

    def _book(self, option_id: str, lang: str, tool, res: TurnResult) -> str:
        res.path.append("book")
        h = tool("hold_option", option_id=option_id)
        if h["status"] == "LOST" or h["status"] == "OPTION_EXPIRED":
            fresh = h["data"].get("fresh_options") or []
            return replies.t("lost", lang) + "\n" + (replies.options_text(fresh, lang) if fresh else replies.t("no_slot", lang))
        if h["status"] != "OK":
            return self._human(h, lang, tool, res, "(confirmed a slot)")
        s = tool("submit_booking_request", option_id=option_id)
        if s["status"] != "OK":
            return self._human(s, lang, tool, res, "(booking request)")
        o = next((x for x in self._last_options(tool) if x["option_id"] == option_id), None)
        key = "booked" if s["data"]["status"] == "CONFIRMED" else "pending"
        return replies.t(key, lang, dock_code=o["dock_code"] if o else "", start=o["start"] if o else "")

    def _last_options(self, tool) -> list[dict]:
        r = tool("get_driver_context")
        return r["data"]["flow"]["options"] if r["status"] == "OK" else []

    # ---------------------------------------------------------------- people (B2-18)
    def _human(self, r: dict, lang: str, tool, res: TurnResult, driver_said: str, default_reason: str | None = None) -> str:
        """Make sure a durable escalation exists before telling the driver a coordinator will help."""
        code = (r.get("requires_human") or {}).get("reason_code") or r["data"].get("code") or default_reason or "REQUIRES_HUMAN"
        if code == "HUMAN_ACTIVE":
            res.path.append("human-owns-thread")
            return None
        esc_id = r["data"].get("escalation_id")                     # some tools escalate themselves (safety)
        if not esc_id:
            reason = ESCALATION_REASON.get(code, "DRIVER_REQUEST")
            told = replies.t("safety" if reason == "SAFETY" else "coordinator", lang, sla=5 if reason == "SAFETY" else 10)
            e = tool("escalate_to_human", reason_code=reason, mode="TAKE_OVER" if reason == "SAFETY" else "DECIDE",
                     driver_last_message=driver_said[:500], what_driver_was_told=told[:500])
            if e["status"] != "OK":
                res.path.append("escalation-failed")
                log.error("escalation failed (%s); not promising a coordinator", e)
                return replies.t("unclear", lang) if lang != "en" else \
                    "Sorry, I could not arrange this right now. Please call your dispatcher."
            esc_id = e["data"]["escalation_id"]
        res.escalation_id = str(esc_id)
        res.path.append(f"escalated:{code}")
        return replies.t("safety", lang) if code == "SAFETY" else replies.t("coordinator", lang, sla=10)

    # ---------------------------------------------------------------- option replies
    def _options_reply(self, options: list[dict], lang: str, res: TurnResult) -> str:
        # Owner decision OD-3 (human-check review round 4, 21 Nov): option replies are the deterministic template in the
        # conversation's language (en / hi / hi-Latn). No phrasing model is called. After H-5r3 the model could add
        # only one fixed courtesy line, so it added cost and a model in the reply path for no value. Extraction is
        # unchanged and still independent of the reply.
        res.path.append("phrase:template")
        return replies.options_text(options, lang)
