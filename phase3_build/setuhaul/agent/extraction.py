"""The extraction contract (`record_driver_report`, Phase 1 §6c) and the code that checks what the model returned.

The model (Haiku, forced tool call) only turns the driver's words into this form. Every value is then validated
and converted here, deterministically (Phase 1 §1): local wall-clock strings become timezone-aware timestamps,
impossible values are rejected, and anything missing or ambiguous becomes a `missing_field` the agent must ask
about. The model never decides; it fills a form that code checks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_VERSION = "record_driver_report/v1"

# Verbatim from phase1_frame/02_variables.md §6c (strict, every field nullable, no extra properties).
RECORD_DRIVER_REPORT = {
    "name": "record_driver_report",
    "description": ("Record what the driver said in this turn. Use null for anything the driver did not state. "
                    "Never infer times the driver did not give."),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "shipment_ref_mentioned", "delay_kind", "declared_eta_local", "declared_eta_range_local",
                     "declared_delay_min", "earliest_start_local", "latest_finish_local", "latest_finish_kind",
                     "exception_type", "vehicle_fixed", "safety_concern", "selected_option_id", "explicit_confirmation",
                     "language"],
        "properties": {
            "intent": {"type": "string", "enum": ["REPORT_DELAY", "ASK_OPTIONS", "BOOK", "CHECK_STATUS", "EARLY_ARRIVAL", "CANCEL", "OTHER"]},
            "shipment_ref_mentioned": {"type": ["string", "null"], "description": "Order or shipment reference exactly as the driver wrote it"},
            "delay_kind": {"type": "string", "enum": ["ARRIVAL_TIME", "DELAY_DURATION", "REPAIR_DURATION", "RANGE", "UNKNOWN"]},
            "declared_eta_local": {"type": ["string", "null"], "description": "HH:MM 24h IST, only if the driver stated a time"},
            "declared_eta_range_local": {"type": ["array", "null"], "items": {"type": "string"}, "description": "[HH:MM, HH:MM] if a range was given"},
            "declared_delay_min": {"type": ["integer", "null"]},
            "earliest_start_local": {"type": ["string", "null"]},
            "latest_finish_local": {"type": ["string", "null"]},
            "latest_finish_kind": {"type": ["string", "null"], "enum": ["GATE_OUT", "UNLOAD_END", "UNLOAD_START", "UNKNOWN", None]},
            "exception_type": {"type": "string", "enum": ["DELAY", "BREAKDOWN", "TRAFFIC", "WEATHER", "EARLY_ARRIVAL", "DOCK_UNAVAILABLE", "UNKNOWN"]},
            "vehicle_fixed": {"type": ["boolean", "null"]},
            "safety_concern": {"type": "boolean", "description": "true for any accident, injury, fire, threat or medical issue"},
            "selected_option_id": {"type": ["string", "null"], "description": "Only an option_id previously shown in this conversation"},
            "explicit_confirmation": {"type": "boolean", "description": "true only for an unambiguous yes to one specific shown option"},
            "language": {"type": "string"},
        },
    },
}


class Extraction(BaseModel):
    """The model's output, parsed. Unknown keys are rejected (schema drift is an error, not something to ignore)."""
    model_config = ConfigDict(extra="forbid")
    intent: Literal["REPORT_DELAY", "ASK_OPTIONS", "BOOK", "CHECK_STATUS", "EARLY_ARRIVAL", "CANCEL", "OTHER"]
    shipment_ref_mentioned: str | None = None
    delay_kind: Literal["ARRIVAL_TIME", "DELAY_DURATION", "REPAIR_DURATION", "RANGE", "UNKNOWN"] = "UNKNOWN"
    declared_eta_local: str | None = None
    declared_eta_range_local: list[str] | None = None
    declared_delay_min: int | None = None
    earliest_start_local: str | None = None
    latest_finish_local: str | None = None
    latest_finish_kind: Literal["GATE_OUT", "UNLOAD_END", "UNLOAD_START", "UNKNOWN"] | None = None
    exception_type: Literal["DELAY", "BREAKDOWN", "TRAFFIC", "WEATHER", "EARLY_ARRIVAL", "DOCK_UNAVAILABLE", "UNKNOWN"] = "UNKNOWN"
    vehicle_fixed: bool | None = None
    safety_concern: bool = False
    selected_option_id: str | None = None
    explicit_confirmation: bool = False
    language: str = "en"

    @field_validator("declared_eta_range_local", mode="before")
    @classmethod
    def _half_range(cls, v):
        """A range with a missing end ([\"19:00\", null]) is no range. Dropping that one field keeps the rest of the
        extraction; rejecting it threw the whole answer away (live G3 replay, BB09)."""
        if isinstance(v, list) and (len(v) != 2 or any(x is None for x in v)):
            return None
        return v


# Code-side safety net: either the model's flag or one of these words → a person takes over (Phase 1 §1 safety_flag).
SAFETY_WORDS = re.compile(
    r"\b(accident|crash(ed)?|collision|injur(ed|y)|hurt|bleeding|blood|fire|smoke|ambulance|police|"
    r"robbe(d|ry)|attack(ed)?|threat(en(ed)?)?|chest pain|faint(ed)?|unconscious|emergency|"
    # 'hospital' and 'medical' alone describe cargo in this fleet ("Medical devices", "Hospital consumables", G3 ADV02);
    # they count only when a person is going to, or needs, care.
    r"(to|in|at|admit(ted)?( to)?)( the| a)? hospital|medical (help|attention|emergency)|need(s)? (a )?doctor|"
    r"durghatna|ghayal|chot|aag|khoon|hamla|loot|"
    # OD-2: being stuck somewhere dangerous is a safety incident, not just a breakdown
    r"danger(ous)?|unsafe|not safe|safe nahi|surakshit nahi|khatar(a|nak)|middle of the (road|highway)|beech (sadak|road))\b|"
    r"दुर्घटना|एक्सीडेंट|घायल|चोट|आग|खून|अस्पताल (ले|में|जाना)|एम्बुलेंस|पुलिस|हमला|लूट|खतरा|खतरनाक|सुरक्षित नहीं",
    re.IGNORECASE)

# OD-2: words that say where a broken-down truck is safely stopped. Danger words above always win.
SAFE_PLACE = re.compile(
    r"\b(safe|surakshit|dhaba|petrol pump|fuel station|parking|service road|lay-?by|toll plaza|side (me|mein|pe|of the road)|"
    r"off the road|garage|workshop)\b|सुरक्षित|ढाबा|पेट्रोल पंप|पार्किंग|किनारे", re.IGNORECASE)

# What the driver must have said for code to accept the model's latest_finish_kind (decision D1: "leave the warehouse
# by 1:30" is ambiguous and is asked, even when the model reads it as gate-out; G3 run 1, THR002).
_UNLOAD = re.compile(r"unload|khali|utar|अनलोड|खाली|उतार", re.I)     # utar: "maal utarna" (S4-R2-08 question)
# Review (step 3 update): mentioning unloading is not enough; start and finish must be told apart, or the agent asks.
_START = re.compile(r"\b(start|starts|started|begin|begins|shuru)\b|शुरू", re.I)
_FINISH = re.compile(r"\b(finish|finished|complete|completed|done|end|over|khatam|poora|pura)\b|ख़त्म|खत्म|पूरा", re.I)
_GATE = re.compile(r"\bgate\b|गेट", re.I)


def kind_stated(kind: str, text: str) -> bool:
    """True only when the driver's own words establish this latest_finish_kind and no other."""
    t = text or ""
    if kind == "GATE_OUT":
        return bool(_GATE.search(t))
    if kind in ("UNLOAD_START", "UNLOAD_END") and _UNLOAD.search(t):
        start, finish = bool(_START.search(t)), bool(_FINISH.search(t))
        return (start and not finish) if kind == "UNLOAD_START" else (finish and not start)
    return False

DELAY_REASON = {"BREAKDOWN": "BREAKDOWN", "TRAFFIC": "TRAFFIC", "WEATHER": "WEATHER", "DELAY": "OTHER",
                "EARLY_ARRIVAL": "OTHER", "DOCK_UNAVAILABLE": "OTHER", "UNKNOWN": "OTHER"}


@dataclass
class Checked:
    """Validated, converted extraction. Only this object flows into tools."""
    raw: Extraction
    intent: str
    declared_eta: datetime | None = None
    eta_precision: str | None = None                 # PRECISE | VAGUE
    earliest_start: datetime | None = None
    latest_finish: datetime | None = None
    delay_reason: str | None = None
    safety: bool = False
    shipment_ref: str | None = None
    selected_option_id: str | None = None
    candidate_eta: datetime | None = None            # computed from a bare delay; used only after the driver confirms
    breakdown: str | None = None                     # OD-2: REPAIRED | UNREPAIRED_SAFE | UNREPAIRED_UNKNOWN
    missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.problems


_HHMM = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def local_to_ts(hhmm: str, now: datetime, ambiguous_12h: bool = True) -> datetime | None:
    """'11:20' → the next sensible occurrence in IST. A 12-hour-looking time ('6:30' at 16:24) resolves to the
    occurrence closest after now − 15 min, so 'ETA 6:30' in the afternoon means 18:30."""
    m = _HHMM.match(hhmm or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    now_l = now.astimezone(IST)
    floor = now_l - timedelta(minutes=15)
    hours = [h] if (h == 0 or h > 12 or not ambiguous_12h) else sorted({h % 12, h % 12 + 12})
    cands = []
    for hh in hours:
        for day in (0, 1):
            c = (now_l + timedelta(days=day)).replace(hour=hh, minute=mi, second=0, microsecond=0)
            if c >= floor:
                cands.append(c)
    return min(cands) if cands else None


def check(ex: Extraction, now: datetime, base_eta: datetime | None, text: str = "",
          shown_option_ids: list[str] | None = None) -> Checked:
    """Phase 1 §1 validation rules, in code."""
    c = Checked(raw=ex, intent=ex.intent, shipment_ref=ex.shipment_ref_mentioned)
    c.safety = bool(ex.safety_concern or SAFETY_WORDS.search(text or ""))
    # Owner decision OD-2 (replaces Phase 1's "breakdown not fixed → safety"): only danger is a safety incident. A truck
    # still broken down is an operational escalation; whether it is somewhere safe is read from the driver's words, and
    # asked once when they do not say.
    if ex.exception_type == "BREAKDOWN" and not c.safety:
        has_eta = bool(ex.declared_eta_local or ex.declared_eta_range_local or
                       (ex.delay_kind == "DELAY_DURATION" and ex.declared_delay_min is not None))
        if ex.vehicle_fixed is True or (ex.vehicle_fixed is None and has_eta and ex.delay_kind != "REPAIR_DURATION"):
            c.breakdown = "REPAIRED"                            # moving again: the normal ETA workflow
        else:
            c.breakdown = "UNREPAIRED_SAFE" if SAFE_PLACE.search(text or "") else "UNREPAIRED_UNKNOWN"
    c.delay_reason = DELAY_REASON.get(ex.exception_type)

    # ETA: an arrival time, a range (plan for the late end), or a duration added to the current ETA
    if ex.delay_kind == "ARRIVAL_TIME" and ex.declared_eta_local:
        c.declared_eta, c.eta_precision = local_to_ts(ex.declared_eta_local, now), "PRECISE"
        if c.declared_eta is None:
            c.problems.append("declared_eta_local")
    elif ex.delay_kind == "RANGE" and ex.declared_eta_range_local and len(ex.declared_eta_range_local) == 2:
        lo, hi = (local_to_ts(x, now) for x in ex.declared_eta_range_local)
        if lo is None or hi is None or hi < lo or hi - lo > timedelta(hours=3):
            c.problems.append("declared_eta_range_local")
        else:
            c.declared_eta, c.eta_precision = hi, "VAGUE"
    elif ex.delay_kind == "DELAY_DURATION" and ex.declared_delay_min is not None:
        if not 0 <= ex.declared_delay_min <= 1440:
            c.problems.append("declared_delay_min")
        elif base_eta is None:
            c.missing.append("declared_eta_local")
        else:
            # Decision register D1 / THR003: "late by one hour" has no reference point (a new arrival time, or another
            # hour from now?). Code computes the likely reading and the driver confirms it in one question.
            c.candidate_eta = base_eta + timedelta(minutes=ex.declared_delay_min)
            c.missing.append("delay_kind")
    elif ex.delay_kind == "REPAIR_DURATION" or (ex.delay_kind == "UNKNOWN" and ex.declared_delay_min is not None):
        c.missing.append("delay_kind")                          # "is 90 minutes the repair or the total delay?"
    if c.declared_eta is not None:
        if not (now - timedelta(minutes=15) <= c.declared_eta <= now + timedelta(hours=24)):
            c.problems.append("declared_eta_out_of_range")
            c.declared_eta = None
    if c.candidate_eta is not None and not (now - timedelta(minutes=15) <= c.candidate_eta <= now + timedelta(hours=24)):
        c.problems.append("declared_eta_out_of_range")
        c.candidate_eta = None
        c.missing.remove("delay_kind")

    if ex.earliest_start_local:
        c.earliest_start = local_to_ts(ex.earliest_start_local, now)
        if c.earliest_start is None:
            c.problems.append("earliest_start_local")
    if ex.latest_finish_local:
        c.latest_finish = local_to_ts(ex.latest_finish_local, now)
        if c.latest_finish is None:
            c.problems.append("latest_finish_local")
        elif ex.latest_finish_kind in (None, "UNKNOWN") or not kind_stated(ex.latest_finish_kind, text):
            c.missing.append("latest_finish_kind")              # MSG004: gate-out or unloading start?
    if c.earliest_start and c.latest_finish and c.earliest_start >= c.latest_finish:
        c.problems.append("earliest_after_latest")

    if ex.selected_option_id:
        if shown_option_ids is None or ex.selected_option_id not in shown_option_ids:
            c.problems.append("selected_option_id")             # only an option shown in this conversation
        else:
            c.selected_option_id = ex.selected_option_id
    # A delay with no time needs one. "Anything after 7 works" already gives what options need (an earliest start), so
    # asking for an ETA as well would be an unnecessary question (D8; live G3 replay, BB09).
    if ex.intent in ("REPORT_DELAY",) and c.declared_eta is None and c.earliest_start is None and not c.missing \
            and not c.safety and "declared_eta_local" not in c.problems:
        c.missing.append("declared_eta_local")
    return c
