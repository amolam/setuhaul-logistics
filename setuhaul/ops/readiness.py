"""Demo fast track: a READ-ONLY readiness check for a provisional release, run BEFORE anything changes.

    python -m setuhaul.ops.readiness --approval-ref OWNER-APPROVAL-YYYYMMDD-<ref> --release-sha <40-hex> \
        [--report docs/release/evidence/provisional_gates.json] [--deploy-at 2026-11-25T16:05:00Z] [--json]

Why: the activation workflow issues the provisional authorization only AFTER `cdk deploy` has rotated the release's
database logins (M2). A refusal at that point (rate limit, an open window, a used approval, a stale report) leaves the
running release's logins NOLOGIN and the new one booking-OFF. This check asks the same questions first, from the
database's own state, and lists every blocker with its reason and the action that clears it.

It reads, and never writes:
  * Aurora (Data API, owner secret, a READ ONLY transaction that is rolled back):
      - the rolling 24 h issuance count per environment and the limit the DEPLOYED issuing function enforces
        (`provisional_rate_limit(text)` when a later migration provides it, else the literal in
        `issue_provisional_authorization`'s source), so the answer is the database's, not a calculated one;
      - an open (ISSUED, unexpired) window; whether the approval reference was already used;
      - the newest recorded deployment (deployment_manifests) and the kill switch row;
  * the running release's booking state as /health reports it (the API derives it from
    booking_authorization_status, 0033 D4);
  * the committed five-gate report, judged by the SAME function the pipeline uses at issuance
    (gate_evidence.provisional_report_problems): reused when it passes, regenerated only when it does not;
  * CloudFormation (stack status) and the public /health (kill switch, booking state).

Exit 0 = ready (warnings may remain), 1 = blocked, 2 = could not check (treat as blocked).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

APPROVAL_RX = re.compile(r"^OWNER-APPROVAL-20[0-9]{6}-[A-Za-z0-9-]{3,40}$")
WINDOW = timedelta(hours=24)
STACK, REGION = "SetuHaulPilot", "ap-southeast-2"
REPORT = "docs/release/evidence/provisional_gates.json"
MUST_PASS = ("AWS_QUEUE", "RECOVERY_WORKER")      # runbook §3.1: the other three meet their exception conditions


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    action: str = ""
    blocking: bool = True


@dataclass
class State:
    """Everything the assessment needs, gathered by collect() (or built by a test)."""
    environment: str
    now: datetime
    limit: int | None
    limit_source: str
    issued_24h: list[datetime] = field(default_factory=list)
    open_window: dict | None = None
    approval_used: bool = False
    running: dict = field(default_factory=dict)          # deployment_id, artifact_sha256, release_sha
    booking: dict = field(default_factory=dict)          # booking_authorization_status of the running deployment
    stack_status: str | None = None
    health: dict = field(default_factory=dict)
    report: dict | None = None
    report_error: str | None = None
    recovery_target: dict | None = None                  # the named authorization of a recovery run (0033/0045)


# ------------------------------------------------------------------------------------------------ assessment (pure)
def assess(s: State, approval_ref: str, release_sha: str, deploy_at: datetime | None = None,
           recovery: bool = False) -> list[Check]:
    from setuhaul.ops.gate_evidence import provisional_report_problems
    from setuhaul.ops.renewal import REPORT_MAX_AGE, parse_ts
    at = deploy_at or s.now
    out: list[Check] = []

    ok = bool(APPROVAL_RX.match(approval_ref or ""))
    out.append(Check("approval reference format", ok, approval_ref or "(none)",
                     "" if ok else "ask the owner for a written OWNER-APPROVAL-YYYYMMDD-<ref>"))
    out.append(Check("approval reference unused", not s.approval_used,
                     "already used by a provisional authorization" if s.approval_used else "not used yet",
                     "" if not s.approval_used else "single-use: the owner must issue a new reference"))
    ok = bool(re.fullmatch(r"[0-9a-f]{40}", release_sha or ""))
    out.append(Check("release commit", ok, release_sha or "(none)", "" if ok else "pass the full 40-hex commit"))

    # capacity: the database's limit, the database's rows; when the oldest drops out of the rolling window
    if s.limit is None:
        out.append(Check("provisional capacity (rolling 24 h)", False, f"limit unknown ({s.limit_source})",
                         "check that migrations 0032/0033 are applied"))
    else:
        live = sorted(t for t in s.issued_24h if t > at - WINDOW)
        free = s.limit - len(live)
        if free > 0:
            out.append(Check("provisional capacity (rolling 24 h)", True,
                             f"{len(live)} of {s.limit} used in {s.environment}; {free} left at {at:%Y-%m-%dT%H:%M}Z "
                             f"(limit from {s.limit_source})"))
            if free == 1:
                out.append(Check("capacity for a retry", False, "only one issuance left: a failed run cannot be "
                                 "retried inside 24 h", "plan the run carefully; a retry needs the window to clear",
                                 blocking=False))
        else:
            clears = live[len(live) - s.limit] + WINDOW + timedelta(seconds=1)
            out.append(Check("provisional capacity (rolling 24 h)", False,
                             f"{len(live)} of {s.limit} used in {s.environment} (issued "
                             + ", ".join(f"{t:%Y-%m-%dT%H:%M:%S}Z" for t in live) + f"; limit from {s.limit_source})",
                             f"wait until {clears:%Y-%m-%dT%H:%M:%S}Z, or deploy an owner-approved limit change first"))
    out.append(Check("no open provisional window", s.open_window is None,
                     "none open" if s.open_window is None else f"authorization {s.open_window.get('authorization_id')} "
                     f"open until {s.open_window.get('expires_at')}",
                     "" if s.open_window is None else "close it (renewal run) or revoke it (runbook §7.5) first"))

    # the running release: a normal provisional issuance needs it fully evidenced and authorized (no chaining)
    state = (s.booking or {}).get("booking_state")
    if recovery:          # 0033/0045: a recovery follows a REVOKED or owner-approved EXPIRED window (the database judges)
        ok = state in ("off", "refused")
        out.append(Check("running release after a revoked or expired window (recovery)", ok,
                         f"deployment {str(s.running.get('deployment_id'))[:14]} state {state}",
                         "" if ok else "a recovery applies only after a revoked or expired window; use the normal path"))
        if s.recovery_target is not None:
            why = recovery_target_problems(s.recovery_target, s.running, s.environment, s.now)
            t = s.recovery_target
            out.append(Check(f"recovery target authorization {t.get('authorization_id')}", not why,
                             f"{t.get('status') or 'not found'}" + (f", expires {t.get('expires_at')}" if t.get('expires_at') else ""),
                             "; ".join(why)))
    else:
        ok = state == "authorized"
        out.append(Check("running release authorized (no chaining)", ok,
                         f"deployment {str(s.running.get('deployment_id'))[:14]} state {state}",
                         "" if ok else "the previous release lacks complete evidence: renewal or recovery path"))
    ks = (s.health or {}).get("booking_kill_switch")
    row = (s.booking or {}).get("kill_switch_engaged")
    ok = ks == "disengaged" and row is not True
    out.append(Check("kill switch disengaged", ok, f"/health says {ks}; database row engaged={row}",
                     "" if ok else "disengage it (ops lead) or wait: the smoke would refuse"))
    ok = bool(s.stack_status) and s.stack_status.endswith("_COMPLETE") and "ROLLBACK" not in s.stack_status
    out.append(Check("stack ready", ok, f"{STACK} {s.stack_status}",
                     "" if ok else "wait for the stack to settle (no deploy while it is updating or rolled back)"))

    # five-gate evidence: reuse when the pipeline would accept it at the deploy time, else say how to regenerate
    regen = (f"python scripts/step6_gate_evidence.py --mode renewal --approval-ref {approval_ref} --release-sha "
             f"{s.running.get('release_sha') or '<running release commit>'} --out {REPORT}  (then commit it)")
    if s.report is None:
        out.append(Check("five-gate report reusable", False, s.report_error or "missing", regen))
    else:
        p = provisional_report_problems(s.report, s.running.get("artifact_sha256"), approval_ref, now=at,
                                        recovery=recovery)
        gates = s.report.get("gates") or {}
        p += [f"{g} is {gates.get(g, {}).get('status')!r}, must be PASS" for g in MUST_PASS
              if gates.get(g, {}).get("status") != "PASS"]
        t = parse_ts(s.report.get("collected_utc"))
        left = (t + REPORT_MAX_AGE - at) if t else None
        if not p:
            out.append(Check("five-gate report reusable", True, f"collected {s.report.get('collected_utc')}, valid "
                             f"{int(left.total_seconds() // 60)} more minutes at the deploy time; reused, not regenerated"))
        else:
            out.append(Check("five-gate report reusable", False, "; ".join(p), regen))
    return out


def blockers(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking and not c.ok]


RECOVERY_DAYS = timedelta(days=7)


def recovery_target_problems(t: dict, running: dict, environment: str, now: datetime) -> list[str]:
    """Why the named authorization cannot be recovered (the database re-checks every condition at issuance)."""
    if not t.get("found"):
        return ["no such authorization"]
    p = []
    if t.get("environment") != environment or t.get("deployment_id") != running.get("deployment_id"):
        p.append("it is not the running release's authorization")
    if t.get("recovery_of") is not None:
        p.append("it was itself a recovery (no second hop)")
    if t.get("already_recovered"):
        p.append("it has already been recovered once")
    st, exp = t.get("status"), t.get("expires_at")
    lapsed = st == "ISSUED" and exp is not None and _ts(exp) <= now       # swept to EXPIRED by the issuance itself
    if st == "REVOKED":
        if t.get("closed_at") is None or now - _ts(t["closed_at"]) > RECOVERY_DAYS:
            p.append("revoked more than 7 days ago")
        if t.get("events") != ["ISSUED", "REVOKED"]:
            p.append(f"audit trail {t.get('events')}, expected ISSUED, REVOKED")
    elif st == "EXPIRED" or lapsed:
        if not t.get("owner_approved"):
            p.append("an EXPIRED window needs the owner's recorded approval (0045) for this authorization")
        if now - _ts(exp) > RECOVERY_DAYS:
            p.append("expired more than 7 days ago")
        if t.get("events") != (["ISSUED", "EXPIRED"] if st == "EXPIRED" else ["ISSUED"]):
            p.append(f"audit trail {t.get('events')} is not that of an expired window")
    else:
        p.append(f"it is {st}: only a REVOKED or an owner-approved EXPIRED window can be recovered")
    return p


# ------------------------------------------------------------------------------------------------ database reads
LIMIT_RX = re.compile(r"IF\s+n\s*>=\s*(\d+)\s+THEN\s+RAISE\s+EXCEPTION\s+'PROVISIONAL_RATE_LIMIT", re.I)


def read_recovery_target(c, authorization_id: int) -> dict:
    row = c.execute("SELECT authorization_id, environment, deployment_id, status, expires_at, closed_at, recovery_of "
                    "FROM setuhaul.provisional_authorizations WHERE authorization_id = %s", (authorization_id,)).fetchone()
    if not row:
        return {"authorization_id": authorization_id, "found": False}
    has_table = c.execute("SELECT to_regclass('setuhaul.provisional_expired_recovery_approvals') IS NOT NULL").fetchone()[0]
    approved = bool(has_table and c.execute(
        "SELECT EXISTS (SELECT 1 FROM setuhaul.provisional_expired_recovery_approvals WHERE authorization_id = %s "
        "AND environment = %s AND deployment_id = %s)", (authorization_id, row[1], row[2])).fetchone()[0])
    used = bool(c.execute("SELECT EXISTS (SELECT 1 FROM setuhaul.provisional_authorizations WHERE recovery_of = %s)",
                          (authorization_id,)).fetchone()[0])
    ev = [r[0] for r in c.execute("SELECT action FROM setuhaul.provisional_authorization_events "
                                  "WHERE authorization_id = %s ORDER BY event_id", (authorization_id,)).fetchall()]
    return {"authorization_id": row[0], "found": True, "environment": row[1], "deployment_id": row[2],
            "status": row[3], "expires_at": str(row[4]) if row[4] else None,
            "closed_at": str(row[5]) if row[5] else None, "recovery_of": row[6], "owner_approved": approved,
            "already_recovered": used, "events": ev}


def read_database(c, environment: str, approval_ref: str) -> dict:
    """The database's own answers. `c` is a READ ONLY connection with search_path setuhaul."""
    def one(sql, args=()):
        row = c.execute(sql, args).fetchone()
        return row[0] if row else None

    if one("SELECT to_regprocedure('setuhaul.provisional_rate_limit(text)') IS NOT NULL"):
        limit, src = int(one("SELECT setuhaul.provisional_rate_limit(%s)", (environment,))), "provisional_rate_limit()"
    else:
        srcs = [r[0] for r in c.execute("SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                                        "WHERE n.nspname = 'setuhaul' AND p.proname = 'issue_provisional_authorization' "
                                        "AND p.prolang <> (SELECT oid FROM pg_language WHERE lanname = 'sql')").fetchall()]
        found = sorted({int(m.group(1)) for s in srcs for m in LIMIT_RX.finditer(s or "")})
        limit, src = (found[0], "issue_provisional_authorization source") if len(found) == 1 else (None, f"found {found}")
    issued = [r[0] for r in c.execute("SELECT issued_at FROM setuhaul.provisional_authorizations WHERE environment = %s "
                                      "AND issued_at > clock_timestamp() - interval '48 hours' ORDER BY issued_at",
                                      (environment,)).fetchall()]
    op = c.execute("SELECT authorization_id, expires_at FROM setuhaul.provisional_authorizations WHERE environment = %s "
                   "AND status = 'ISSUED' AND clock_timestamp() < expires_at", (environment,)).fetchone()
    used = bool(one("SELECT EXISTS (SELECT 1 FROM setuhaul.provisional_authorizations WHERE approval_ref = %s)",
                    (approval_ref,)))
    # the newest recorded deployment (what booking_gate_snapshot reports as 'manifest'), read from its table
    m = c.execute("SELECT deployment_id, artifact_sha256 FROM setuhaul.deployment_manifests WHERE environment = %s "
                  "ORDER BY manifest_id DESC LIMIT 1", (environment,)).fetchone()
    man = {"deployment_id": m[0], "artifact_sha256": m[1]} if m else {}
    rel = one("SELECT release_sha FROM setuhaul.provisional_authorizations WHERE environment = %s AND deployment_id = %s "
              "ORDER BY authorization_id DESC LIMIT 1", (environment, man.get("deployment_id"))) if man else None
    killed = one("SELECT engaged FROM setuhaul.booking_kill_switch WHERE id = 1")
    now = one("SELECT clock_timestamp()")
    return {"limit": limit, "limit_source": src, "issued": [_ts(t) for t in issued],
            "open": {"authorization_id": op[0], "expires_at": str(op[1])} if op else None, "approval_used": used,
            "running": {"deployment_id": man.get("deployment_id"), "artifact_sha256": man.get("artifact_sha256"),
                        "release_sha": rel}, "kill_switch_engaged": killed, "now": _ts(now)}


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))


def read_only_owner_connection():
    """A Data API transaction on the owner secret, READ ONLY, always rolled back (yields a Connection)."""
    from contextlib import contextmanager
    import boto3
    from setuhaul.aws.data_api import Connection, begin_with_resume

    @contextmanager
    def cm():
        rds = boto3.client("rds-data", region_name=os.environ.get("AWS_REGION") or REGION)
        kw = {"resourceArn": os.environ["SETUHAUL_DB_CLUSTER_ARN"], "secretArn": os.environ["SETUHAUL_DB_OWNER_SECRET_ARN"],
              "database": os.environ.get("SETUHAUL_DB_NAME", "setuhaul")}
        tid = begin_with_resume(rds, kw, 120)
        c = Connection(rds, kw["resourceArn"], kw["secretArn"], kw["database"], tid)
        try:
            c.execute("SET TRANSACTION READ ONLY")
            yield c
        finally:
            rds.rollback_transaction(resourceArn=kw["resourceArn"], secretArn=kw["secretArn"], transactionId=tid)
    return cm()


# ------------------------------------------------------------------------------------------------ collection
def collect(environment: str, approval_ref: str, report_path: str, recovery_of: int | None = None) -> State:
    import urllib.request
    import boto3
    # In the activation workflow the deploy role cannot describe the stack: it passes the read-only CDK lookup role's
    # session (SETUHAUL_LOOKUP_SESSION), as the gate collection does. The database reads stay on the caller's role.
    lookup = os.environ.get("SETUHAUL_LOOKUP_SESSION")
    session = boto3.Session(**json.loads(lookup)) if lookup else boto3.Session()
    cf = session.client("cloudformation", region_name=REGION)
    stack = cf.describe_stacks(StackName=STACK)["Stacks"][0]
    outs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    os.environ.setdefault("SETUHAUL_DB_CLUSTER_ARN", outs["ClusterArn"])
    os.environ.setdefault("SETUHAUL_DB_OWNER_SECRET_ARN", outs["OwnerSecretArn"])
    with read_only_owner_connection() as c:
        db = read_database(c, environment, approval_ref)
        target = read_recovery_target(c, recovery_of) if recovery_of is not None else None
    try:
        with urllib.request.urlopen(outs["ApiUrl"].rstrip("/") + "/health", timeout=10) as r:   # noqa: S310 (stack URL)
            health = json.loads(r.read())
    except Exception as e:                                         # noqa: BLE001 — reported, never ignored
        health = {"error": type(e).__name__}
    report, err = None, None
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError) as e:
        err = f"{report_path}: {type(e).__name__}"
    return State(environment=environment, now=db["now"], limit=db["limit"], limit_source=db["limit_source"],
                 issued_24h=db["issued"], open_window=db["open"], approval_used=db["approval_used"],
                 running=db["running"], booking={"booking_state": health.get("booking_state"),
                                                 "kill_switch_engaged": db["kill_switch_engaged"]},
                 stack_status=stack["StackStatus"], health=health,
                 report=report, report_error=err, recovery_target=target)


def main(argv=None) -> int:
    from setuhaul.ops.renewal import ENVIRONMENT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--approval-ref", required=True)
    ap.add_argument("--release-sha", required=True)
    ap.add_argument("--environment", default=ENVIRONMENT)
    ap.add_argument("--report", default=REPORT)
    ap.add_argument("--deploy-at", help="judge capacity and report age at this UTC time (default: now)")
    ap.add_argument("--recovery", action="store_true", help="a Recovery Release (0033 recovery_of) run")
    ap.add_argument("--recovery-of", type=int, default=None,
                    help="the authorization a recovery run names (0033/0045); implies --recovery and checks it")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        s = collect(a.environment, a.approval_ref, a.report, a.recovery_of)
    except Exception as e:                                         # noqa: BLE001
        print(f"READINESS: could not check ({type(e).__name__}: {str(e)[:200]}) - treat as BLOCKED")
        return 2
    at = _ts(a.deploy_at) if a.deploy_at else None
    checks = assess(s, a.approval_ref, a.release_sha, at, recovery=a.recovery or a.recovery_of is not None)
    b = blockers(checks)
    if a.json:
        print(json.dumps({"ready": not b, "checks": [c.__dict__ for c in checks],
                          "running": s.running, "database_now": s.now.isoformat()}, default=str, indent=1))
    else:
        print(f"readiness for {a.environment} at {(at or s.now):%Y-%m-%dT%H:%M:%S}Z (database clock {s.now:%H:%M:%S}Z)")
        for c in checks:
            mark = "PASS" if c.ok else ("BLOCK" if c.blocking else "WARN")
            print(f"  {mark:5} {c.name}: {c.detail}" + (f"\n        -> {c.action}" if c.action and not c.ok else ""))
        print("READY: no blocker" if not b else f"BLOCKED: {len(b)} blocker(s); nothing was changed")
    return 0 if not b else 1


if __name__ == "__main__":
    sys.exit(main())
