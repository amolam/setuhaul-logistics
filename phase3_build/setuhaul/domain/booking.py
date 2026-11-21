"""Booking conversation flow (app layer): SELECT → READ_BACK → EXPLICIT_YES → HOLD → SUBMIT → WAREHOUSE.

The agent *requests*; this module and the database *decide*. The LLM never calls the database: it proposes a
next step, and `BookingFlow.allow()` either permits the matching tool call or refuses it. Refusals are data
(a reason code), not exceptions the model can argue with.

Also here:
  * `classify_confirmation`: deterministic yes / no / unclear for the read-back reply, English + Hindi +
    Hinglish. Anything not clearly yes is NOT a yes (no hold is placed on an ambiguous reply).
  * `idem_key`: sha256(thread | tool | canonical(args) | turn), per the tool envelope (04_tool_contracts §1).
  * `map_db_error`: database error tokens → the tool statuses the agent understands (§5).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum


class S(str, Enum):
    NO_OPTIONS = "NO_OPTIONS"
    OPTIONS_SHOWN = "OPTIONS_SHOWN"
    SELECTED = "SELECTED"              # read-back sent, waiting for an explicit yes
    CONFIRMED_BY_DRIVER = "CONFIRMED_BY_DRIVER"
    HELD = "HELD"
    SUBMITTED = "SUBMITTED"            # PENDING at the warehouse
    BOOKED = "BOOKED"                  # CONFIRMED
    CLOSED = "CLOSED"                  # rejected / expired / released / cancelled → may restart with fresh options
    HUMAN = "HUMAN"                    # a person owns the thread; the agent does nothing


# tool → states in which the flow allows it
ALLOWED = {
    # BOOKED: a driver delayed again after confirmation may ask for a new slot; the request replaces the confirmed
    # booking and goes to the warehouse like any other (human test H-3)
    "find_feasible_options": {S.NO_OPTIONS, S.OPTIONS_SHOWN, S.SELECTED, S.CONFIRMED_BY_DRIVER, S.CLOSED, S.BOOKED},
    "select_option":         {S.OPTIONS_SHOWN, S.SELECTED},
    "hold_option":           {S.CONFIRMED_BY_DRIVER},
    "submit_booking_request": {S.HELD},
    "release_hold":          {S.HELD},
    "get_booking_status":    set(S) - {S.HUMAN},
    "escalate_to_human":     set(S) - {S.HUMAN},
}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


@dataclass
class BookingFlow:
    thread_id: str
    state: S = S.NO_OPTIONS
    shown: dict[str, dict] = field(default_factory=dict)     # option_id -> option facts (from the tool, not the LLM)
    selected: str | None = None
    yes_message_id: int | None = None
    attestation_id: str | None = None          # written by the trusted inbound path, verified by the database
    hold_option: str | None = None
    appointment_id: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)

    def _go(self, new: S, why: str):
        self.history.append((self.state.value, f"{new.value}: {why}"))
        self.state = new

    # ---- the gate every tool call passes through -------------------------------------------
    def allow(self, tool: str, args: dict | None = None) -> Decision:
        args = args or {}
        if self.state is S.HUMAN:
            return Decision(False, "HUMAN_ACTIVE")
        if tool not in ALLOWED:
            return Decision(False, "TOOL_NOT_AVAILABLE_TO_AGENT")
        if self.state not in ALLOWED[tool]:
            return Decision(False, f"NOT_ALLOWED_IN_{self.state.value}")
        if tool in ("select_option", "hold_option") and args.get("option_id") not in self.shown:
            return Decision(False, "OPTION_NOT_SHOWN_IN_THREAD")                       # R1: no invented options
        if tool == "hold_option" and (args.get("option_id") != self.selected or self.yes_message_id is None):
            return Decision(False, "NO_EXPLICIT_YES_FOR_THIS_OPTION")                   # R1
        if tool in ("submit_booking_request", "release_hold") and args.get("option_id") != self.hold_option:
            return Decision(False, "NOT_THE_HELD_OPTION")                               # R2
        return Decision(True)

    # ---- events (called by the orchestrator with tool results / driver messages) ------------
    def options_shown(self, options: dict[str, dict]):
        self.shown, self.selected, self.yes_message_id, self.attestation_id = dict(options), None, None, None
        self._go(S.OPTIONS_SHOWN if options else S.NO_OPTIONS, f"{len(options)} options")

    def option_selected(self, option_id: str):
        d = self.allow("select_option", {"option_id": option_id})
        if not d.allowed:
            raise PermissionError(d.reason)
        self.selected, self.yes_message_id, self.attestation_id = option_id, None, None
        self._go(S.SELECTED, f"read-back of {option_id} sent")

    def confirmation_attested(self, message_id: int, attestation_id: str, verdict: str, option_id: str) -> str:
        """Apply a database attestation (the trusted classification of the driver's reply to the read-back)."""
        if self.state not in (S.SELECTED, S.CONFIRMED_BY_DRIVER) or option_id != self.selected:
            return "IGNORED"
        if verdict == "YES":
            self.yes_message_id, self.attestation_id = message_id, attestation_id
            self._go(S.CONFIRMED_BY_DRIVER, f"attested yes in message {message_id}")
        elif verdict == "NO":
            self.selected, self.yes_message_id, self.attestation_id = None, None, None
            self._go(S.OPTIONS_SHOWN, "driver declined the read-back")
        else:                                       # a later unclear reply withdraws an earlier yes
            self.yes_message_id, self.attestation_id = None, None
            self._go(S.SELECTED, "driver reply unclear; waiting for a clear yes")
        return verdict

    def driver_replied(self, message_id: int, text: str) -> str:
        """Only meaningful in SELECTED. Returns YES / NO / UNCLEAR."""
        if self.state is not S.SELECTED:
            return "IGNORED"
        verdict = classify_confirmation(text)
        if verdict == "YES":
            self.yes_message_id = message_id
            self._go(S.CONFIRMED_BY_DRIVER, f"explicit yes in message {message_id}")
        elif verdict == "NO":
            self.selected = None
            self._go(S.OPTIONS_SHOWN, "driver declined the read-back")
        return verdict

    def hold_result(self, status: str, option_id: str, fresh: dict | None = None):
        if status == "HELD":
            self.hold_option = option_id
            self._go(S.HELD, f"hold on {option_id}")
        else:                                                    # LOST / NOT_FEASIBLE / OPTION_EXPIRED
            self.options_shown(fresh or {})

    def submit_result(self, status: str, appointment_id: str | None):
        self.appointment_id = appointment_id
        self._go({"PENDING": S.SUBMITTED, "CONFIRMED": S.BOOKED}.get(status, S.CLOSED), f"submit → {status}")

    def warehouse_decided(self, status: str):
        self._go(S.BOOKED if status == "CONFIRMED" else S.CLOSED, f"warehouse → {status}")

    def released(self):
        self.hold_option = None
        self._go(S.OPTIONS_SHOWN if self.shown else S.CLOSED, "hold released")

    def human_takeover(self):
        self._go(S.HUMAN, "human took over")


# ---- explicit-yes classification ------------------------------------------------------------
_YES = {"yes", "y", "yeah", "yep", "yup", "ok", "okay", "k", "confirm", "confirmed", "sure", "done", "book it",
        "go ahead", "please book", "book", "correct", "right", "fine", "perfect",
        "haan", "han", "haa", "ha", "haanji", "haan ji", "haaji", "haji", "ha ji", "haa ji", "ji", "ji haan", "theek hai", "thik hai", "theek", "thik",
        "chalega", "kar do", "kardo", "book kar do", "pakka", "sahi hai", "bilkul",
        "हाँ", "हां", "हा", "जी", "जी हाँ", "ठीक है", "ठीक", "कर दो", "बुक कर दो", "पक्का", "सही है", "बिल्कुल"}
_NO = {"no", "n", "nope", "nah", "cancel", "dont", "don't", "do not", "wait", "not now", "stop", "change",
       "nahi", "nahin", "na", "mat", "mat karo", "ruko", "cancel karo", "nahi chahiye",
       "नहीं", "ना", "मत", "रुको", "मत करो"}
_HEDGE = re.compile(r"\b(but|lekin|par|magar|if|agar|maybe|shayad|later|baad me|baad mein|instead|other|dusra|doosra|"
                    r"kya|\?|which|kaun|kab|when|what)\b|\?|लेकिन|पर |अगर|शायद|बाद में|दूसरा|क्या", re.IGNORECASE)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFC", text).lower().strip()
    t = re.sub(r"[\U0001F44D\U0001F44C✅✔]", " yes ", t)        # 👍 👌 ✅ ✔
    t = re.sub(r"[!.,;:\-_*~\"'()]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_confirmation(text: str) -> str:
    """YES only for a short, unhedged affirmative. Questions, conditions and anything long are UNCLEAR."""
    raw = text or ""
    t = _norm(raw)
    if not t:
        return "UNCLEAR"
    if _HEDGE.search(raw.lower()) or _HEDGE.search(t):
        return "UNCLEAR"
    if t in _NO or any(t.startswith(n + " ") for n in _NO if " " not in n):
        return "NO"
    words = t.split()
    if t in _YES:
        return "YES"
    if len(words) <= 4 and all(w in {x for y in _YES for x in y.split()} | {"please", "pls", "plz", "sir", "bhai", "ji", "yes"}
                               for w in words):
        return "YES"
    return "UNCLEAR"


# ---- idempotency and error mapping ------------------------------------------------------------
def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def idem_key(thread_id: str, tool: str, args: dict, turn_id: int) -> str:
    return hashlib.sha256(f"{thread_id}|{tool}|{canonical(args)}|{turn_id}".encode()).hexdigest()


ERROR_MAP = {
    "OPTION_UNKNOWN": "NOT_FEASIBLE", "OPTION_EXPIRED": "OPTION_EXPIRED", "HOLD_EXPIRED": "OPTION_EXPIRED",
    "HOLD_NOT_FOUND": "OPTION_EXPIRED", "AUTOMATION_PAUSED": "REQUIRES_HUMAN", "HUMAN_ACTIVE": "REQUIRES_HUMAN",
    "ATTESTATION_INVALID": "NEEDS_INFO", "ATTESTATION_EXPIRED": "NEEDS_INFO", "SELECTION_CHANGED": "NEEDS_INFO",
    "MESSAGE_NOT_FROM_DRIVER": "FORBIDDEN", "SHIPMENT_NOT_IN_THREAD": "FORBIDDEN", "ETA_OUT_OF_RANGE": "NEEDS_INFO",
    "BAD_READ_BACK_TEXT": "ERROR", "CONFIRMATION_SUPERSEDED": "NEEDS_INFO",
    "NO_CONFIRMER_AVAILABLE": "REQUIRES_HUMAN", "SHIPMENT_IN_DOCK": "NOT_FEASIBLE",
    "REQUIRES_PERSON": "REQUIRES_HUMAN", "OPERATIONAL_HOLD": "REQUIRES_HUMAN",      # B-WF-2a person-always rules; B-WF-2 holds
    "NO_DRIVER_CONFIRMATION": "NEEDS_INFO", "OPTION_NOT_FOR_THREAD": "FORBIDDEN", "THREAD_UNKNOWN": "FORBIDDEN",
    "SHIPMENT_NOT_VISIBLE": "FORBIDDEN", "FORBIDDEN": "FORBIDDEN", "DOCK_WRONG_FACILITY": "NOT_FEASIBLE",
    "DOCK_WEIGHT_EXCEEDED": "NOT_FEASIBLE", "DOCK_NOT_REFRIGERATED": "NOT_FEASIBLE", "DOCK_NOT_HEAVY": "NOT_FEASIBLE",
    "SHIPMENT_NOT_ELIGIBLE": "NOT_FEASIBLE", "IDEMPOTENCY_KEY_REUSED": "ERROR",
}


def map_db_error(sqlstate: str | None, message: str) -> str:
    if sqlstate == "23P01":
        return "LOST"                     # exclusion constraint: someone else holds the slot or the truck is busy
    if sqlstate == "42501":
        return "FORBIDDEN"                # privilege or RLS
    token = (message or "").split(":")[0].strip()
    return ERROR_MAP.get(token, "ERROR")
