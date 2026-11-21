"""Phase 1 prototype of the deterministic feasibility check (no LLM).

It proves that the variables in 02_variables.md can be computed from the snapshot and
produces worked examples for the Problem Definition Sheet. In the Build phase this logic
becomes the booking service; here it is read-only.

Usage:  python scripts/frame/feasibility_prototype.py   -> writes docs/1-frame/worked_examples.md
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DB = ROOT / "data" / "setuhaul_freight_operations.db"
OUT = ROOT / "docs" / "1-frame" / "worked_examples.md"

# ---- CFG (per facility; values from 02_variables.md §6) ---------------------------------
CFG = {
    "FAC-JAI-01": dict(gate_to_dock_min=10, changeover_min=10, eta_band=dict(LOW=30, MEDIUM=15, HIGH=0)),
    "FAC-GGN-01": dict(gate_to_dock_min=15, changeover_min=10, eta_band=dict(LOW=30, MEDIUM=15, HIGH=0)),
}
SOURCE_RANK = {"OPERATIONS_OVERRIDE": 4, "WAREHOUSE_ESTIMATE": 3, "DRIVER_DECLARED": 2, "ORIGINAL_PLAN": 1}
ACTIVE = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS")


def ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def hm(d: datetime | None) -> str:
    return d.strftime("%H:%M") if d else "—"


@dataclass
class Option:
    dock: str
    start: datetime
    end: datetime
    slots: list[str]
    needs_approval: bool


@dataclass
class Result:
    shipment_id: str
    now: datetime
    facts: dict
    options: list[Option] = field(default_factory=list)
    rejections: dict[str, list[str]] = field(default_factory=dict)
    outcome: str = ""


class Snapshot:
    def __init__(self, path: Path):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        q = lambda sql, *a: [dict(r) for r in con.execute(sql, a)]
        self.shipments = {r["shipment_id"]: r for r in q("SELECT * FROM shipments")}
        self.docks = {r["dock_id"]: r for r in q("SELECT * FROM docks")}
        self.facilities = {r["facility_id"]: r for r in q("SELECT * FROM facilities")}
        self.slots = q("SELECT * FROM appointment_slots ORDER BY dock_id, slot_start_ts")
        self.slot_by_id = {s["slot_id"]: s for s in self.slots}
        self.appts = q("SELECT * FROM appointments")
        self.events = q("SELECT * FROM dock_status_events")
        self.checkins = {r["shipment_id"]: r for r in q("SELECT * FROM facility_checkins")}
        self.etas = q("SELECT * FROM eta_updates")
        self.rules = {}
        for r in q("SELECT * FROM facility_rules WHERE active_flag = 1"):
            self.rules.setdefault(r["facility_id"], {})[r["rule_type"]] = r["rule_value"]

    # ---- derived variables -----------------------------------------------------------
    def effective_eta(self, sid: str, now: datetime):
        """Source precedence, then latest created_at, ignoring updates created after `now`."""
        ups = [e for e in self.etas if e["shipment_id"] == sid and ts(e["created_at"]) <= now]
        if not ups:
            s = self.shipments[sid]
            return ts(s["original_eta_ts"]), "ORIGINAL_PLAN", "HIGH"
        best = max(ups, key=lambda e: (SOURCE_RANK[e["source_type"]], e["created_at"]))
        return ts(best["declared_eta_ts"]), best["source_type"], best["confidence_code"]

    def required_capability(self, s) -> str:
        heavy_kg = int(self.rules.get(s["destination_facility_id"], {}).get("HEAVY_DOCK_REQUIRED_KG", 10**9))
        if s["temperature_control_required"]:
            return "REEFER"
        if s["required_dock_type"] == "HEAVY" or s["load_weight_kg"] > heavy_kg:
            return "HEAVY"
        return "STANDARD"

    def dock_compatible(self, s, d) -> str | None:
        """None when compatible, else a plain-language reason."""
        if d["facility_id"] != s["destination_facility_id"]:
            return "other facility"
        cap = self.required_capability(s)
        if d["dock_type"] != cap:
            return f"dock type {d['dock_type']} ≠ required {cap}"
        if s["load_weight_kg"] > d["max_vehicle_weight_kg"]:
            return f"load {s['load_weight_kg']} kg > dock limit {d['max_vehicle_weight_kg']} kg"
        if s["temperature_control_required"] and not d["supports_refrigerated"]:
            return "no refrigeration"
        return None

    def busy_window(self, sid: str):
        s = self.shipments[sid]
        start = ts(s["actual_departure_ts"] or s["planned_departure_ts"])
        ck = self.checkins.get(sid)
        if ck and ck["gate_out_ts"]:
            return start, ts(ck["gate_out_ts"])
        eta = ts(s["latest_eta_ts"] or s["original_eta_ts"])
        return start, eta + timedelta(minutes=s["expected_unload_min"])

    def assignment_conflicts(self, sid: str) -> list[str]:
        s = self.shipments[sid]
        if s["current_status"] in ("CANCELLED", "COMPLETED"):
            return []
        a0, a1 = self.busy_window(sid)
        out = []
        for oid, o in self.shipments.items():
            if oid == sid or o["current_status"] in ("CANCELLED", "COMPLETED"):  # history is audited separately (A01)
                continue
            if o["driver_id"] != s["driver_id"] and o["vehicle_id"] != s["vehicle_id"]:
                continue
            b0, b1 = self.busy_window(oid)
            if a0 < b1 and b0 < a1:
                who = "driver" if o["driver_id"] == s["driver_id"] else "vehicle"
                out.append(f"{who} also on {oid} ({hm(b0)}–{hm(b1)})")
        return out

    def committed_intervals(self, dock_id: str, now: datetime, exclude_shipment: str):
        """Occupied intervals on a dock from active appointments (unload may exceed the slot)."""
        out = []
        for a in self.appts:
            if a["appointment_status"] not in ACTIVE or a["shipment_id"] == exclude_shipment:
                continue
            sl = self.slot_by_id[a["slot_id"]]
            if sl["dock_id"] != dock_id:
                continue
            s = self.shipments[a["shipment_id"]]
            co = CFG[sl["facility_id"]]["changeover_min"]
            ck = self.checkins.get(a["shipment_id"])
            if a["appointment_status"] == "IN_PROGRESS" and ck and ck["unload_start_ts"]:
                start = ts(ck["unload_start_ts"])
                end = max(now, start + timedelta(minutes=s["expected_unload_min"])) + timedelta(minutes=co)
            else:
                start = ts(sl["slot_start_ts"])
                end = max(ts(sl["slot_end_ts"]), start + timedelta(minutes=s["expected_unload_min"] + co))
            out.append((start, end, a["appointment_id"]))
        return out

    def dock_events(self, dock_id: str):
        return [(ts(e["event_start_ts"]), ts(e["event_end_ts"]) or datetime.max.replace(tzinfo=ts(e["event_start_ts"]).tzinfo),
                 f"{e['dock_event_id']} {e['event_type']}")
                for e in self.events if e["dock_id"] == dock_id and e["event_type"] in ("BREAKDOWN", "MAINTENANCE", "MANUAL_BLOCK")]

    # ---- the single feasibility predicate ----------------------------------------------
    def find_options(self, sid: str, now: datetime, earliest_start: datetime | None = None,
                     latest_finish: datetime | None = None, max_options: int = 3) -> Result:
        s = self.shipments[sid]
        fac = s["destination_facility_id"]
        cfg = CFG[fac]
        eta, src, conf = self.effective_eta(sid, now)
        ck = self.checkins.get(sid)
        arrived = bool(ck and ck["gate_in_ts"])
        eta_hi = eta + timedelta(minutes=cfg["eta_band"][conf])
        ready = (max(now, ts(ck["gate_in_ts"])) if arrived else max(now, eta_hi)) + timedelta(minutes=cfg["gate_to_dock_min"])
        occ = s["expected_unload_min"] + cfg["changeover_min"]
        f = self.facilities[fac]
        day = ready.date()
        close = datetime.combine(day, datetime.strptime(f["close_time"], "%H:%M").time(), ready.tzinfo)
        last_new = self.rules.get(fac, {}).get("LAST_NEW_START_TIME")
        last_new_dt = datetime.combine(day, datetime.strptime(last_new, "%H:%M").time(), ready.tzinfo) if last_new else close
        conflicts = self.assignment_conflicts(sid)

        cur = next((a for a in self.appts if a["shipment_id"] == sid and a["is_current"] and a["appointment_status"] in ACTIVE), None)
        cur_txt, at_risk = "none", None
        if cur:
            sl = self.slot_by_id[cur["slot_id"]]
            d = self.docks[sl["dock_id"]]
            grace = int(self.rules.get(fac, {}).get("NO_SHOW_GRACE_MIN", f["checkin_grace_min"]))
            at_risk = ready > ts(sl["slot_start_ts"]) + timedelta(minutes=grace) or \
                (ready + timedelta(minutes=occ) > ts(sl["slot_end_ts"]) + timedelta(minutes=grace)) or \
                bool(self.dock_compatible(s, d))
            cur_txt = f"{cur['appointment_id']} {d['dock_code']} {hm(ts(sl['slot_start_ts']))}–{hm(ts(sl['slot_end_ts']))} ({cur['appointment_status']})"

        res = Result(sid, now, dict(
            status=s["current_status"], priority=s["priority_code"], weight_kg=s["load_weight_kg"],
            eta=hm(eta), eta_source=src, eta_confidence=conf, eta_window=f"{hm(eta)}–{hm(eta_hi)}",
            arrived=arrived, ready_ts=hm(ready), capability=self.required_capability(s),
            unload_min=s["expected_unload_min"], occupancy_min=occ,
            current_appointment=cur_txt, appointment_at_risk=at_risk,
            assignment_conflicts="; ".join(conflicts) or "none",
            earliest_start=hm(earliest_start), latest_finish=hm(latest_finish)))

        if s["current_status"] in ("CANCELLED", "COMPLETED"):
            res.outcome = f"NOT ELIGIBLE: shipment is {s['current_status']}"
            return res
        if ck and ck["queue_state"] in ("IN_DOCK", "COMPLETED"):
            res.outcome = "FIXED TASK: truck is already in a dock; never re-slotted"
            return res
        for dock_id, d in sorted(self.docks.items(), key=lambda kv: kv[1]["dock_code"]):
            if d["facility_id"] != fac:
                continue
            reason = self.dock_compatible(s, d)
            if reason:
                res.rejections[d["dock_code"]] = [reason]
                continue
            dslots = [x for x in self.slots if x["dock_id"] == dock_id]
            slot_len = (ts(dslots[0]["slot_end_ts"]) - ts(dslots[0]["slot_start_ts"])).seconds // 60
            k = math.ceil(occ / slot_len)
            busy = self.committed_intervals(dock_id, now, sid)
            evts = self.dock_events(dock_id)
            reasons: list[str] = []
            for i, first in enumerate(dslots):
                run = dslots[i:i + k]
                start = ts(first["slot_start_ts"])
                end = start + timedelta(minutes=occ)
                why = None
                if len(run) < k or any(ts(run[j]["slot_end_ts"]) != ts(run[j + 1]["slot_start_ts"]) for j in range(len(run) - 1)):
                    why = "not enough contiguous slots"
                elif start < ready:
                    why = None if start + timedelta(minutes=slot_len) <= ready else "starts before truck is ready"
                    if why is None:
                        continue  # silently skip slots fully in the past/before ready
                elif earliest_start and start < earliest_start:
                    why = "before driver's earliest start"
                elif any(x["slot_status"] != "OPEN" for x in run):
                    why = f"slot {next(x for x in run if x['slot_status'] != 'OPEN')['slot_status'].lower()} ({next(x for x in run if x['slot_status'] != 'OPEN')['block_reason']})"
                elif any(b0 < end and start < b1 for b0, b1, _ in busy):
                    why = "occupied by " + next(a for b0, b1, a in busy if b0 < end and start < b1)
                elif any(e0 < end and start < e1 for e0, e1, _ in evts):
                    why = next(n for e0, e1, n in evts if e0 < end and start < e1)
                elif end > close:
                    why = "ends after facility close"
                elif latest_finish and end > latest_finish:
                    why = "finishes after driver's latest finish"
                if why:
                    reasons.append(f"{hm(start)} {why}")
                    continue
                res.options.append(Option(d["dock_code"], start, end, [x["slot_id"] for x in run], start >= last_new_dt))
            res.rejections[d["dock_code"]] = reasons[:4] + ([f"… {len(reasons) - 4} more"] if len(reasons) > 4 else [])

        # ranking: no-approval first, earliest finish, dock code  (priority is applied by the allocation policy)
        res.options.sort(key=lambda o: (o.needs_approval, o.end, o.dock))
        res.options = res.options[:max_options]
        if conflicts:
            res.outcome = "ESCALATE (CONTRADICTORY_DATA): driver/vehicle double-assigned; options shown to ops only"
        elif not res.options:
            res.outcome = "ESCALATE (NO_FEASIBLE_SLOT)"
        elif all(o.needs_approval for o in res.options):
            res.outcome = "OPTIONS NEED APPROVAL (after last new start time)"
        else:
            res.outcome = f"{len(res.options)} feasible option(s)"
        return res


def render(res: Result, title: str, note: str) -> str:
    f = res.facts
    lines = [f"### {title}", "", note, "", f"Evaluated at `now = {hm(res.now)}`.", "",
             "| Variable | Value |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in f.items()]
    lines += ["", "**Docks considered**", "", "| Dock | Rejected starts (first reasons) |", "|---|---|"]
    lines += [f"| {d} | {'<br>'.join(r) if r else '—'} |" for d, r in res.rejections.items()]
    lines += ["", "**Feasible options (ranked)**", ""]
    if res.options:
        lines += ["| # | Dock | Start | Occupied until | Slots reserved | Needs approval |", "|---|---|---|---|---|---|"]
        lines += [f"| {i} | {o.dock} | {hm(o.start)} | {hm(o.end)} | {', '.join(o.slots)} | {'yes' if o.needs_approval else 'no'} |"
                  for i, o in enumerate(res.options, 1)]
    else:
        lines.append("_None._")
    lines += ["", f"**Outcome:** {res.outcome}", ""]
    return "\n".join(lines)


def main():
    snap = Snapshot(DB)
    T = lambda h: datetime.fromisoformat(f"2026-08-04T{h}:00+05:30")
    md = ["# Worked feasibility checks (generated)", "",
          "Generated by `feasibility_prototype.py` from the snapshot. Deterministic: no LLM. "
          "Config: gate-to-dock 10 min, changeover 10 min, ETA band LOW ±30 / MEDIUM ±15 / HIGH 0.", ""]

    md.append(render(snap.find_options("SHP1006", T("09:35"), earliest_start=T("12:00")),
                     "1. SHP1006: delayed HIGH-priority truck (THR001)",
                     "Driver: \"Reaching around 11:20. Any slot after 12?\" The current 10:00 slot is stale (audit A06)."))
    md.append(render(snap.find_options("SHP1014", T("09:52")),
                     "2. SHP1014: CRITICAL truck whose pending request is wrong (THR004 / APT1014A)",
                     "Shows why the pending D1 11:00 request (audit A03, A05) must not be confirmed: 21 t exceeds D1's limit and 75 min does not fit."))
    md.append(render(snap.find_options("SHP1015", T("16:26")),
                     "3. SHP1015: reefer truck, no feasible slot (THR005)",
                     "The only reefer dock is under maintenance from 18:00 (DEVT002). The correct outcome is escalation, never an invented slot."))
    md.append(render(snap.find_options("SHP1012", T("09:30"), latest_finish=T("13:30")),
                     "4. SHP1012: driver with a latest-finish constraint (THR002)",
                     "Driver: \"I need to leave the warehouse before 1:30.\" `latest_finish` is treated as a hard constraint once its meaning is confirmed (MSG004)."))
    md.append(render(snap.find_options("SHP1006", T("09:35")),
                     "5. SHP1006 at the same moment without the 12:00 preference",
                     "Shows how one driver-stated constraint changes the option set."))

    # sample values for every live shipment at a common clock
    now = T("10:00")
    md += ["## Sample derived values for every inbound shipment (now = 10:00)", "",
           "| Shipment | Status | Priority | Capability | kg | ETA (src/conf) | Ready | Occupancy | Current appointment | At risk | Assignment conflict | Outcome at 10:00 |",
           "|---|---|---|---|---:|---|---|---:|---|---|---|---|"]
    for sid in sorted(snap.shipments):
        r = snap.find_options(sid, now)
        f = r.facts
        md.append(f"| {sid} | {f['status']} | {f['priority']} | {f['capability']} | {f['weight_kg']} | "
                  f"{f['eta']} ({f['eta_source'].split('_')[0].lower()}/{f['eta_confidence']}) | {f['ready_ts']} | {f['occupancy_min']} | "
                  f"{f['current_appointment']} | {f['appointment_at_risk'] if f['appointment_at_risk'] is not None else '—'} | "
                  f"{'yes' if f['assignment_conflicts'] != 'none' else 'no'} | {r.outcome} |")
    OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
