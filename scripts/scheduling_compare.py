"""Compare the three facility scheduling rules (FCFS, EARLIEST_ETA, FACILITY_SCORE) on the same input.

Two inputs:
  1. A designed scenario at one facility: an early truck, a late truck and a truck already unloading share three
     docks with a few ordinary trucks (brief p.10: "show what happens when an early truck, a late truck and a truck
     in progress share one facility").
  2. The challenge data snapshot, facility FAC-JAI-01 at 09:56 on 4 Aug 2026 (same instant as the Frame solver spike),
     read through the sqlite adapter and the feasibility engine.

Pure and offline: no database, no network, no model. Usage:  python scripts/scheduling_compare.py [--markdown]
"""
from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setuhaul.domain.feasibility import hm  # noqa: E402
from setuhaul.domain.scheduling import (RULES, Dock, FacilitySnapshot, FixedTask, Hours, Job, Promise,  # noqa: E402
                                        propose, snapshot_from_data)

IST = datetime.fromisoformat("2026-08-04T00:00:00+05:30").tzinfo
METRICS = ("weighted_wait", "total_wait", "max_wait", "missed_promises", "changed_promises", "kept_promises",
           "unscheduled", "moves", "moves_in_a_cycle", "score")


def at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 8, 4, h, m, tzinfo=IST)


def scenario() -> FacilitySnapshot:
    """Facility FAC-DEMO, now 09:00, docks D1-D3 (all standard), open 06:00-22:00, last new start 21:00, grace 30 min.
    Processing = unload + 10 min changeover. Release = when the truck can be at a dock."""
    D = ("DK1", "DK2", "DK3")
    promise = lambda appt, dock, start, mins: Promise(appt, dock, at(start), at(start) + timedelta(minutes=mins))
    jobs = (
        # (a) EARLY: through the gate at 08:45, ready now; promised D2 at 10:30
        Job("SHP-EARLY", "NORMAL", at("09:00"), 50, D, arrived_at=at("08:45"), planned_eta=at("10:15"),
            promise=promise("APT-EARLY", "DK2", "10:30", 50)),
        # (b) LATE: promised D1 09:45, the latest ETA now says the truck can be at a dock at 10:35
        Job("SHP-LATE", "HIGH", at("10:35"), 60, D, planned_eta=at("09:30"),
            promise=promise("APT-LATE", "DK1", "09:45", 60)),
        # ordinary trucks
        Job("SHP-A", "NORMAL", at("09:10"), 60, D, planned_eta=at("09:00"), promise=promise("APT-A", "DK2", "09:15", 60)),
        Job("SHP-B", "CRITICAL", at("10:40"), 70, D, planned_eta=at("10:30"), promise=promise("APT-B", "DK1", "10:50", 70)),
        Job("SHP-C", "LOW", at("09:05"), 40, D, planned_eta=at("09:00")),                       # no booking yet
        Job("SHP-D", "NORMAL", at("09:20"), 60, D, planned_eta=at("09:15"), promise=promise("APT-D", "DK3", "09:30", 60)),
    )
    fixed = (
        # (c) IN PROGRESS: unloading on D1 since 08:20, predicted to free the dock (with changeover) at 09:40
        FixedTask("DK1", at("08:20"), at("09:40"), "APT-UNLOADING", "IN_DOCK"),
    )
    return FacilitySnapshot("FAC-DEMO", at("09:00"), tuple(Dock(d, d.replace("DK", "D")) for d in D), jobs, fixed,
                            Hours(time(6), time(21), time(22)), grace_min=30)


def snapshot_jai() -> FacilitySnapshot:
    from setuhaul.adapters.sqlite_snapshot import load
    return snapshot_from_data(load(), "FAC-JAI-01", datetime.fromisoformat("2026-08-04T09:56:00+05:30"))


def table(head, rows) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def report(title: str, snap: FacilitySnapshot) -> list[str]:
    code = {d.dock_id: d.dock_code for d in snap.docks}
    lines = [f"## {title}", "", f"Facility {snap.facility_id}, now {hm(snap.now)}, window to {hm(snap.horizon_end)}, "
             f"input fingerprint `{snap.fingerprint()[:16]}…`", "", "### Input state", ""]
    lines += table(["Shipment", "Priority (w)", "Ready", "Processing min", "Compatible docks", "Promised", "Arrived", "Flag"],
                   [(j.shipment_id, f"{j.priority} ({j.weight})", hm(j.release), j.processing_min,
                     ", ".join(code[d] for d in j.compatible_docks) or "none",
                     f"{code.get(j.promise.dock_id, j.promise.dock_id)} {hm(j.promise.start)}" if j.promise else "—",
                     hm(j.arrived_at) if j.arrived_at else "—", ", ".join(j.flags))
                    for j in sorted(snap.jobs, key=lambda x: (x.release, x.shipment_id))])
    lines += ["", "Fixed (never moved):", ""]
    lines += table(["Dock", "From", "To", "Kind", "Ref"],
                   [(code[f.dock_id], hm(f.start), hm(f.end), f.kind, f.ref) for f in sorted(snap.fixed, key=lambda f: (f.dock_id, f.start))])
    props = {r: propose(snap, r) for r in RULES}
    lines += ["", "### Metrics", ""]
    lines += table(["Rule"] + list(METRICS), [(r, *(p.metrics[m] for m in METRICS)) for r, p in props.items()])
    lines += ["", "### Orderings", ""]
    for r, p in props.items():
        lines += [f"**{r}**", ""]
        def apply(a):
            if a.action != "MOVE":
                return "—"
            if a.apply_order is None:
                return "swap cycle: a person"
            return str(a.apply_order) + (f" (after {', '.join(a.depends_on)})" if a.depends_on else "")
        lines += table(["#", "Shipment", "Dock", "Start", "End", "Action", "Apply", "Wait", "w·wait", "late term", "change",
                        "Score", "Reason"],
                       [(i, a.shipment_id, a.dock_code, hm(a.start), hm(a.end), a.action, apply(a), a.score.wait_min,
                         a.score.wait_term, a.score.lateness_term, a.score.change_term, a.score.total, a.reason)
                        for i, a in enumerate(p.assignments, 1)])
        if p.unscheduled:
            lines += ["", "Unscheduled (for a person): " + "; ".join(f"{u.shipment_id} {u.reason_code}" for u in p.unscheduled)]
        lines += [""]
    return lines


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")            # reasons use arrows and the w·min unit
    md = report("Scenario: early, late and in-progress trucks at one facility", scenario())
    md += report("Challenge snapshot: FAC-JAI-01 at 09:56", snapshot_jai())
    print("\n".join(md))


if __name__ == "__main__":
    main()
