"""Golden dataset G3: the scenario format, loader and extraction grader (08_signoff_and_backlog.md §G3).

The dataset itself is data (`golden/v1/scenarios.jsonl`); this module only says what a valid scenario is and how an
extraction is graded against it. Labels describe the *designed* behaviour (Phase 1 + Phase 2 decisions), not what the
current code happens to do: a scenario the code fails is a finding, not a label to change.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent / "golden"
DATE = "2026-08-04"

TOOLS = {"get_driver_context", "record_driver_report", "find_feasible_options", "select_option", "confirm_selection",
         "hold_option", "submit_booking_request", "release_hold", "get_booking_status", "escalate_to_human"}
OUTCOME = re.compile(r"^(KEEP_CURRENT|OPTIONS_SHOWN|READ_BACK|PENDING_REQUEST|LOST_REOFFER|STATUS|EXPLAIN_POLICY|ACK|"
                     r"UNCLEAR|REFUSED|SILENT|DEDUPED|"
                     r"ASK:(declared_eta_local|delay_kind|latest_finish_kind|shipment_id|confirmation)|"
                     r"ESCALATED:(SAFETY|NO_FEASIBLE_SLOT|CONTRADICTORY_DATA|DRIVER_REQUEST|BREAKDOWN))$")
GATES = {f"D{i}" for i in range(1, 14)}                       # 07_evaluation_plan.md §3
RUBRIC = {"asks_only_missing", "no_overpromise", "refusal_quality", "clarity", "tone", "language_match",
          "context_retention"}                               # 07_evaluation_plan.md §4
NEEDS = {"e2e", "multi_driver", "dock_event", "scheduler", "dedupe", "booking_on"}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
OPTION = re.compile(r"^D\d (\d{2}:\d{2})$")                   # "D2 12:00": dock code and start, as shown to the driver
X_KEYS = {"intent", "eta", "earliest", "latest", "latest_kind", "kind", "dmin", "safety", "pick", "yes", "lang", "ref",
          "exc", "missing"}
GROUPS = {"brief_beat": 13, "delay_ladder": 10, "seed_thread": 12, "audit_trap": 10, "language": 8, "adversarial": 7}
POLICY = 7                       # v3: owner decisions OD-1 / OD-2, outside the signed-off 60


class Ctx(BaseModel):
    """Conversation state before the turn, for extraction-only runs (end-to-end runs read it from the database)."""
    model_config = ConfigDict(extra="forbid")
    base_eta: str | None = None                               # the shipment's ETA before this message (HH:MM)
    options: list[str] = []                                   # options shown earlier in this conversation
    awaiting: int | None = None                               # index of the option read back and awaiting a yes
    ships: list[str] = []                                     # live order references, when more than one

    @field_validator("options")
    @classmethod
    def _opts(cls, v):
        for o in v:
            if not OPTION.match(o):
                raise ValueError(f"option {o!r} must look like 'D2 12:00'")
        return v


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    must: list[str] = []                                      # case-insensitive substrings, any language variant
    must_not: list[str] = []


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str
    at: str | None = None                                     # HH:MM; defaults to the scenario's `now`
    driver: str | None = None                                 # multi-driver scenarios; defaults to the scenario's
    event: str | None = None                                  # a database change applied just before this turn
    duplicate_of: int | None = None                           # provider redelivery of an earlier turn (new message id)
    ctx: Ctx = Field(default_factory=Ctx)
    x: dict | None = None                                     # expected extraction, graded field by field
    outcome: str | list[str]                                  # what code must decide (one, or any of a list)
    ops: list[str] = []                                       # tools that must be called, in this order (a subsequence)
    forbid: list[str] = []                                    # tools that must not be called on this turn
    checks: dict = {}                                         # option-level assertions, see README
    reply: Reply = Field(default_factory=Reply)

    @field_validator("outcome")
    @classmethod
    def _outcome(cls, v):
        for o in ([v] if isinstance(v, str) else v):
            if not OUTCOME.match(o):
                raise ValueError(f"unknown outcome {o!r}")
        return v

    @field_validator("ops", "forbid")
    @classmethod
    def _tools(cls, v):
        bad = set(v) - TOOLS
        if bad:
            raise ValueError(f"unknown tools {bad}")
        return v

    @field_validator("x")
    @classmethod
    def _x(cls, v):
        if v is None:
            return v
        bad = set(v) - X_KEYS
        if bad:
            raise ValueError(f"unknown extraction keys {bad}")
        for k in ("eta", "earliest", "latest"):
            if v.get(k) is not None and not HHMM.match(v[k]):
                raise ValueError(f"{k} must be HH:MM, got {v[k]!r}")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if self.x and self.x.get("pick") is not None and not 0 <= self.x["pick"] < len(self.ctx.options):
            raise ValueError("pick must index ctx.options")
        if self.ctx.awaiting is not None and not 0 <= self.ctx.awaiting < len(self.ctx.options):
            raise ValueError("awaiting must index ctx.options")
        if self.at and not HHMM.match(self.at):
            raise ValueError("at must be HH:MM")
        if set(self.ops) & set(self.forbid):
            raise ValueError("a tool cannot be both required and forbidden")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    group: Literal["brief_beat", "delay_ladder", "seed_thread", "audit_trap", "language", "adversarial", "policy"]
    title: str
    source: str                                               # brief page, thread, audit id or design decision
    fixture: str = "seed-2026-08-04"                          # the seed snapshot; `event` patches apply per turn
    config: dict = {}                                         # e.g. {"booking_enabled": true}
    driver: str
    shipment: str | None = None
    now: str
    turns: list[Turn]
    end: dict = {}                                            # expected database end state
    gates: list[str]
    rubric: list[str] = []
    needs: list[str] = []                                     # capabilities a runner must have to play it end to end
    notes: str | None = None

    @model_validator(mode="after")
    def _labels(self):
        if not HHMM.match(self.now):
            raise ValueError("now must be HH:MM")
        if not self.turns:
            raise ValueError("a scenario needs at least one turn")
        for name, have, allowed in (("gates", self.gates, GATES), ("rubric", self.rubric, RUBRIC),
                                    ("needs", self.needs, NEEDS)):
            if set(have) - allowed:
                raise ValueError(f"unknown {name}: {set(have) - allowed}")
        if not self.gates:
            raise ValueError("every scenario carries at least one hard-gate label")
        for i, t in enumerate(self.turns):
            if t.duplicate_of is not None and not 0 <= t.duplicate_of < i:
                raise ValueError("duplicate_of must point to an earlier turn")
        return self


def path(version: str = "v4") -> Path:
    return ROOT / version / "scenarios.jsonl"


def load(version: str = "v4") -> tuple[list[Scenario], str]:
    """Scenarios plus the dataset hash every run records in its trace (G3 pass condition)."""
    raw = path(version).read_bytes()
    out = []
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(Scenario(**json.loads(line)))
            except Exception as e:
                raise ValueError(f"scenarios.jsonl line {n}: {e}") from e
    return out, f"{version}+sha256:{hashlib.sha256(raw).hexdigest()[:16]}"


def at(s: Scenario, t: Turn) -> datetime:
    return datetime.fromisoformat(f"{DATE}T{t.at or s.now}:00+05:30")


def option_ids(t: Turn) -> list[str]:
    return [f"opt-{i + 1}-{o.replace(' ', '-').replace(':', '')}" for i, o in enumerate(t.ctx.options)]


def user_block(s: Scenario, t: Turn) -> str:
    """Same shape as Orchestrator._user_block. Synthetic ids; no phone numbers or coordinates."""
    lines = [f"Now (India time): {DATE} {t.at or s.now}"]
    refs = t.ctx.ships or ([f"ORD-260804-{s.shipment[-3:]}"] if s.shipment else [])
    for r in refs:
        lines.append(f"Shipment {r}, current slot: see warehouse system")
    ids = option_ids(t)
    for i, (oid, o) in enumerate(zip(ids, t.ctx.options), 1):
        dock, start = o.split()
        h, m = map(int, start.split(":"))
        lines.append(f"Option {i}: option_id={oid} dock {dock} at {(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'} on 4 Aug")
    if t.ctx.awaiting is not None:
        lines.append(f"Read back and awaiting a yes for option_id={ids[t.ctx.awaiting]}")
    lines.append(f"Driver message: {t.say}")
    return "\n".join(lines)


def grade(ex, s: Scenario, t: Turn) -> dict[str, tuple]:
    """Compare one extraction with the turn's labels. Returns {field: (expected, got)} for every mismatch.
    Times are graded after the code checks (extraction.check), i.e. as the orchestrator would use them."""
    from setuhaul.agent.extraction import check

    want = t.x or {}
    base = datetime.fromisoformat(f"{DATE}T{t.ctx.base_eta}:00+05:30") if t.ctx.base_eta else None
    ids = option_ids(t)
    c = check(ex, at(s, t), base, t.say, ids)
    hm = lambda d: d.strftime("%H:%M") if d else None
    got = {"intent": ex.intent, "eta": hm(c.declared_eta or c.candidate_eta),   # the arrival time the extraction implies
"earliest": hm(c.earliest_start), "latest": hm(c.latest_finish),
           "latest_kind": ex.latest_finish_kind, "kind": ex.delay_kind, "dmin": ex.declared_delay_min, "safety": c.safety,
           "pick": ids.index(c.selected_option_id) if c.selected_option_id in ids else None,
           "yes": ex.explicit_confirmation, "lang": ex.language, "ref": (ex.shipment_ref_mentioned or None),
           "exc": ex.exception_type, "missing": c.missing}
    bad = {}
    for k, v in want.items():
        g = got[k]
        if k == "missing":
            ok = set(v) <= set(g)
        elif k == "ref":
            ok = (g or "").upper() == (v or "").upper()
        elif isinstance(v, list):
            ok = g in v
        else:
            ok = g == v
        if not ok:
            bad[k] = (v, g)
    if "safety" not in want and c.safety:                    # a false safety alarm is always graded
        bad["safety"] = (False, True)
    return bad, got


def oracle_raw(x: dict | None, ids: list[str]) -> dict:
    """The record_driver_report a perfect model would return for these labels (no labels: an empty 'OTHER' report).
    `ids` are the option ids shown in the conversation, so a labelled `pick` index becomes a real option id."""
    from datetime import timedelta as _td
    x = x or {}
    first = lambda v, d: (v[0] if isinstance(v, list) else v) if v is not None else d
    kind = x.get("kind") or ("DELAY_DURATION" if x.get("dmin") is not None else "ARRIVAL_TIME" if x.get("eta") else "UNKNOWN")
    timed = any(x.get(k) is not None for k in ("eta", "dmin", "earliest", "latest"))
    default = "BOOK" if x.get("pick") is not None or x.get("yes") else "REPORT_DELAY" if timed else "OTHER"
    raw = dict(intent=first(x.get("intent"), default), delay_kind=kind,
               exception_type=first(x.get("exc"), "UNKNOWN"), language=first(x.get("lang"), "en"),
               shipment_ref_mentioned=x.get("ref"), explicit_confirmation=bool(x.get("yes")),
               safety_concern=bool(x.get("safety")),
               selected_option_id=ids[x["pick"]] if x.get("pick") is not None and x["pick"] < len(ids) else None,
               earliest_start_local=x.get("earliest"), latest_finish_local=x.get("latest"),
               latest_finish_kind=x.get("latest_kind"))
    if kind == "DELAY_DURATION":
        raw["declared_delay_min"] = x.get("dmin")
    elif kind == "ARRIVAL_TIME":
        raw["declared_eta_local"] = x.get("eta")
    elif kind == "RANGE":
        late = datetime.strptime(x["eta"], "%H:%M")
        raw["declared_eta_range_local"] = [(late - _td(hours=1)).strftime("%H:%M"), x["eta"]]
    elif kind == "REPAIR_DURATION":
        raw["declared_delay_min"] = 40
    return raw
