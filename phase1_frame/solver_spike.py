"""Phase 1 spike: is a solver worth it? Dispatch rules vs OR-Tools CP-SAT on the Jaipur snapshot.

Reads structured state only (never chat text). Reuses the derived variables from feasibility_prototype.
Usage:  python phase1_frame/solver_spike.py  -> writes phase1_frame/solver_results.md
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from ortools.sat.python import cp_model

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feasibility_prototype import ACTIVE, CFG, DB, Snapshot, hm, ts  # noqa: E402

OUT = Path(__file__).resolve().parent / "solver_results.md"
FAC = "FAC-JAI-01"
NOW = datetime.fromisoformat("2026-08-04T09:56:00+05:30")  # after the last morning ETA update (ETA012 09:55)
WEIGHT = {"CRITICAL": 8, "HIGH": 4, "NORMAL": 2, "LOW": 1}
ALPHA, BETA, GAMMA, CHANGE_TOL = 2, 30, 2000, 15   # lateness weight, per-change penalty, unscheduled penalty, minutes


def m(d: datetime) -> int:
    return int((d - NOW).total_seconds() // 60)


def build_instance(snap: Snapshot):
    day_close = NOW.replace(hour=22, minute=0)
    last_start = NOW.replace(hour=21, minute=0)
    docks = {d_id: d for d_id, d in snap.docks.items() if d["facility_id"] == FAC}
    blocked = {d_id: [] for d_id in docks}
    for d_id in docks:
        for e0, e1, name in snap.dock_events(d_id):
            blocked[d_id].append((max(0, m(e0)), max(0, m(min(e1, day_close))), name))
    jobs = []
    for sid, s in snap.shipments.items():
        if s["destination_facility_id"] != FAC or s["current_status"] in ("CANCELLED", "COMPLETED"):
            continue
        ck = snap.checkins.get(sid)
        appt = next((a for a in snap.appts if a["shipment_id"] == sid and a["is_current"] and a["appointment_status"] in ACTIVE), None)
        if ck and ck["queue_state"] == "IN_DOCK":          # fixed task: occupies its dock until predicted end
            start = ts(ck["unload_start_ts"])
            end = max(NOW, start + timedelta(minutes=s["expected_unload_min"])) + timedelta(minutes=CFG[FAC]["changeover_min"])
            blocked[ck["actual_dock_id"]].append((0, m(end), f"{sid} in dock (fixed)"))
            continue
        r = snap.find_options(sid, NOW)
        ready = datetime.strptime(r.facts["ready_ts"], "%H:%M").replace(year=NOW.year, month=NOW.month, day=NOW.day, tzinfo=NOW.tzinfo)
        if ready > day_close:
            continue
        compat = [d_id for d_id, d in docks.items() if snap.dock_compatible(s, d) is None]
        prom_dock = prom_start = None
        if appt:
            sl = snap.slot_by_id[appt["slot_id"]]
            prom_dock, prom_start = sl["dock_id"], m(ts(sl["slot_start_ts"]))
        jobs.append(dict(id=sid, r=max(0, m(ready)), p=r.facts["occupancy_min"], w=WEIGHT[s["priority_code"]],
                         prio=s["priority_code"], compat=compat, prom_dock=prom_dock, prom_start=prom_start,
                         flag=r.facts["assignment_conflicts"] != "none"))
    return jobs, docks, blocked, m(last_start), m(day_close)


def evaluate(jobs, plan, docks):
    """plan: {job_id: (dock_id, start) or None}"""
    tot_wait = wtd_wait = late = changes = unsched = 0
    max_wait = 0
    for j in jobs:
        a = plan.get(j["id"])
        if a is None:
            unsched += 1
            continue
        d, s = a
        wait = s - j["r"]
        tot_wait += wait
        wtd_wait += j["w"] * wait
        max_wait = max(max_wait, wait)
        if j["prom_start"] is not None:
            if s > j["prom_start"] + 30:          # beyond no-show grace = missed promise
                late += 1
            if d != j["prom_dock"] or abs(s - j["prom_start"]) > CHANGE_TOL:
                changes += 1
    return dict(weighted_wait=wtd_wait, total_wait=tot_wait, max_wait=max_wait, missed_promises=late,
                changed_promises=changes, unscheduled=unsched)


def dispatch(jobs, docks, blocked, last_start, close, key):
    busy = {d: list(b[:2] for b in blocked[d]) for d in docks}
    plan = {}
    for j in sorted(jobs, key=key):
        best = None
        for d in j["compat"]:
            t = j["r"]
            for b0, b1 in sorted(busy[d]):
                if t + j["p"] <= b0:
                    break
                if t < b1:
                    t = max(t, b1)
            cand = (t, 0 if d == j["prom_dock"] else 1, d)
            if t <= last_start and t + j["p"] <= close and (best is None or cand < best):
                best = cand
        if best:
            plan[j["id"]] = (best[2], best[0])
            busy[best[2]].append((best[0], best[0] + j["p"]))
        else:
            plan[j["id"]] = None
    return plan


def cpsat(jobs, docks, blocked, last_start, close, time_limit=5.0):
    mdl = cp_model.CpModel()
    H = close
    per_dock = {d: [] for d in docks}
    for d in docks:
        for b0, b1, _ in blocked[d]:
            if b1 > b0:
                per_dock[d].append(mdl.NewIntervalVar(b0, b1 - b0, b1, f"blk_{d}_{b0}"))
    obj = []
    pres_all, start_of, on = {}, {}, {}
    for j in jobs:
        s = mdl.NewIntVar(j["r"], last_start, f"s_{j['id']}")
        sched = mdl.NewBoolVar(f"sched_{j['id']}")
        lits = []
        for d in j["compat"]:
            lit = mdl.NewBoolVar(f"x_{j['id']}_{d}")
            iv = mdl.NewOptionalIntervalVar(s, j["p"], s + j["p"], lit, f"iv_{j['id']}_{d}")
            per_dock[d].append(iv)
            lits.append(lit)
            on[(j["id"], d)] = lit
        mdl.Add(sum(lits) == sched) if lits else mdl.Add(sched == 0)
        mdl.Add(s + j["p"] <= H).OnlyEnforceIf(sched)
        wait = mdl.NewIntVar(0, H, f"w_{j['id']}")
        mdl.Add(wait == s - j["r"]).OnlyEnforceIf(sched)
        mdl.Add(wait == 0).OnlyEnforceIf(sched.Not())
        obj.append(j["w"] * wait)
        obj.append(GAMMA * j["w"] * (1 - sched))
        if j["prom_start"] is not None:
            late = mdl.NewIntVar(0, H, f"l_{j['id']}")
            mdl.Add(late >= s - j["prom_start"])
            obj.append(ALPHA * j["w"] * late)
            moved = mdl.NewBoolVar(f"mv_{j['id']}")
            same = on.get((j["id"], j["prom_dock"]))
            far = mdl.NewBoolVar(f"far_{j['id']}")
            mdl.Add(s - j["prom_start"] > CHANGE_TOL).OnlyEnforceIf(far)
            mdl.Add(s - j["prom_start"] <= CHANGE_TOL).OnlyEnforceIf(far.Not())
            if same is not None:
                mdl.AddBoolOr([far, same.Not(), sched.Not()]).OnlyEnforceIf(moved)
                mdl.AddImplication(far, moved)
                mdl.AddImplication(same.Not(), moved)
            else:
                mdl.Add(moved == 1)
            obj.append(BETA * moved)
        pres_all[j["id"]] = sched
        start_of[j["id"]] = s
    for d, ivs in per_dock.items():
        mdl.AddNoOverlap(ivs)
    mdl.Minimize(sum(obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    status = solver.Solve(mdl)
    plan = {}
    for j in jobs:
        if solver.Value(pres_all[j["id"]]):
            d = next(d for d in j["compat"] if solver.Value(on[(j["id"], d)]))
            plan[j["id"]] = (d, solver.Value(start_of[j["id"]]))
        else:
            plan[j["id"]] = None
    return plan, solver.StatusName(status), solver.WallTime()


def main():
    snap = Snapshot(DB)
    jobs, docks, blocked, last_start, close = build_instance(snap)
    t = lambda mins: hm(NOW + timedelta(minutes=mins))
    code = lambda d: snap.docks[d]["dock_code"]
    runs = []
    t0 = time.perf_counter(); p = dispatch(jobs, docks, blocked, last_start, close, key=lambda j: (j["r"], j["id"]))
    runs.append(("FCFS by ready time", p, f"{(time.perf_counter()-t0)*1000:.1f} ms"))
    t0 = time.perf_counter(); p = dispatch(jobs, docks, blocked, last_start, close, key=lambda j: (-j["w"], j["r"], j["id"]))
    runs.append(("Priority-weighted dispatch", p, f"{(time.perf_counter()-t0)*1000:.1f} ms"))
    t0 = time.perf_counter(); p = dispatch(jobs, docks, blocked, last_start, close,
                                           key=lambda j: (j["prom_start"] if j["prom_start"] is not None else 10**6, -j["w"], j["r"]))
    runs.append(("Keep promises first (EDD)", p, f"{(time.perf_counter()-t0)*1000:.1f} ms"))
    p, status, wall = cpsat(jobs, docks, blocked, last_start, close)
    runs.append((f"CP-SAT ({status})", p, f"{wall*1000:.0f} ms"))

    md = ["# Solver spike: dispatch rules vs CP-SAT (generated)", "",
          f"Facility FAC-JAI-01, now = {hm(NOW)}, horizon to 22:00. {len(jobs)} movable trucks; in-dock truck and dock events are fixed. "
          f"Objective = Σ w·wait + {ALPHA}·Σ w·lateness vs promise + {BETA}·changed promises + {GAMMA}·w·unscheduled. "
          "Weights CRITICAL 8 / HIGH 4 / NORMAL 2 / LOW 1. OR-Tools 9.15.", "",
          "## Trucks in the instance", "", "| Shipment | Priority | Ready | Occupancy | Compatible docks | Promised | Data flag |", "|---|---|---|---:|---|---|---|"]
    for j in sorted(jobs, key=lambda j: j["r"]):
        md.append(f"| {j['id']} | {j['prio']} | {t(j['r'])} | {j['p']} | {', '.join(code(d) for d in j['compat'])} | "
                  f"{(code(j['prom_dock']) + ' ' + t(j['prom_start'])) if j['prom_dock'] else '—'} | {'double-assigned' if j['flag'] else ''} |")
    md += ["", "## Results", "", "| Method | Weighted wait (w·min) | Total wait (min) | Max wait | Missed promises | Changed promises | Unscheduled → escalate | Runtime |",
           "|---|---:|---:|---:|---:|---:|---:|---|"]
    for name, plan, rt in runs:
        e = evaluate(jobs, plan, docks)
        md.append(f"| {name} | {e['weighted_wait']} | {e['total_wait']} | {e['max_wait']} | {e['missed_promises']} | {e['changed_promises']} | {e['unscheduled']} | {rt} |")
    md += ["", "## Plans", "", "| Shipment | " + " | ".join(n for n, _, _ in runs) + " |", "|---|" + "---|" * len(runs)]
    for j in sorted(jobs, key=lambda j: j["r"]):
        cells = []
        for _, plan, _ in runs:
            a = plan.get(j["id"])
            cells.append("**unscheduled**" if a is None else f"{code(a[0])} {t(a[1])}")
        md.append(f"| {j['id']} ({j['prio']}) | " + " | ".join(cells) + " |")
    OUT.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[md.index("## Results"):md.index("## Plans")]))


if __name__ == "__main__":
    main()
