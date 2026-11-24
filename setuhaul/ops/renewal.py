"""Booking evidence renewal: validate an ALREADY ACTIVE synthetic pilot (booking ON) without weakening activation.

Two modes of the Step 6 gate collector (scripts/step6_gate_evidence.py):

  activation  unchanged: the pilot must be booking OFF; BOOKING_WORKFLOW requires every approval execution to be a
              Step 6 drill and the booking switch false everywhere (the pre-activation acceptance).
  renewal     the pilot is booking ON under a recorded owner approval. BOOKING_WORKFLOW is judged by its own acceptance
              criteria (this module): the exact booking configuration, the owner approval, attribution of EVERY approval
              execution to auditable evidence, and a fresh smoke test bound to the deployed release.

Nothing here calls AWS or the database: the collector gathers the facts, this module judges them, so every rule is
unit-tested (tests/test_renewal_gate.py). Nothing here changes any configuration; renewal never turns booking on or off.

Attribution categories (an execution must be in exactly one, or the gate does not pass):
  A  Step 6 drill        the approval's shipment is SYN-STEP6-* (owner-approved drills, 23 Nov 2026).
  B  smoke test          a smoke-test record (scripts/smoke_option_d.py) names the execution, passed, is for this
                         environment, and its shipment/facility match the database record. A record in the renewal
                         format also carries the release SHA and the deployed code hash it ran against.
  C  owner-confirmed     ONLY the two executions the owner confirmed on 23 Nov 2026 (OWNER_CONFIRMED_IDS), from
                         docs/release/evidence/owner_attribution_20261123.json, and only when the database record
                         matches what the owner's record says happened. Never equivalent to B: it never satisfies the
                         fresh-smoke requirement and cannot be extended to another execution without a code change.
Otherwise: UNRESOLVED (no visible record, or no evidence) or FAIL (evidence contradicts the record, a non-synthetic or
out-of-pilot record, a failed execution, or the same execution claimed twice).
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone

MODES = ("activation", "renewal")
ENVIRONMENT = "pilot-ap-southeast-2"
EXPECTED_BOOKING_ON = frozenset({"setuhaul-pilot-api", "setuhaul-pilot-agent"})
PILOT_FACILITIES = ("FAC-JAI-01", "FAC-GGN-01")
APPROVAL_REF = re.compile(r"OWNER-APPROVAL-20\d{6}-[A-Za-z0-9-]{3,40}")
# The owner's confirmation (23 Nov 2026) covers exactly these two executions. Adding one is a code change and a review.
OWNER_CONFIRMED_IDS = frozenset({"87915a37-918e-46b5-8872-8ecaa4fc7a24", "29ffac03-d8bd-4dac-b35c-66b0f8ebf24b"})
REPORT_MAX_AGE = timedelta(hours=24)          # a renewal report older than this is refused when evidence is written
SMOKE_MAX_AGE = timedelta(hours=24)           # the fresh smoke must be at most this old when the report is collected

# Gate statuses in a renewal report. The activation statuses ("PASS", "PARTIAL", "BLOCKED", "PASS WITH PILOT EXCEPTION
# (T33-P)") stay in each gate's `status` exactly as before; `renewal_status` adds the explicit wording.
PASS, FAIL, UNRESOLVED = "PASS", "FAIL", "UNRESOLVED"
PASS_EXC, PARTIAL_EXC, BLOCKED_EXC = ("PASS WITH APPROVED EXCEPTION", "PARTIAL WITH APPROVED EXCEPTION",
                                      "BLOCKED WITH APPROVED EXCEPTION")
STATUSES = (PASS, PASS_EXC, PARTIAL_EXC, BLOCKED_EXC, FAIL, UNRESOLVED)
# The three owner-approved pilot exceptions, with the only activation status each may map from (unchanged scope).
APPROVED_EXCEPTIONS = {"AURORA": ("PASS WITH PILOT EXCEPTION (T33-P)", PASS_EXC),
                       "WORKLOAD_IDENTITY": ("PARTIAL", PARTIAL_EXC),
                       "OUTBOUND_DELIVERY": ("BLOCKED", BLOCKED_EXC)}
# The smoke test's own checks that must be present and true (scripts/smoke_option_d.py, numbered as there).
SMOKE_REQUIRED = ("1 ", "2a ", "2b ", "2c ", "2d ", "2e ", "3 ", "4 ", "5 ", "6 ", "7 ")


def short(approval_id: str) -> str:
    return str(approval_id)[:8]


def code_sha256_hex(lambda_code_sha256: str) -> str:
    """Lambda reports CodeSha256 as base64 of the SHA-256 of the zip it received; the manifest stores the same hash in hex."""
    return base64.b64decode(lambda_code_sha256).hex()


def parse_ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    s = str(v).replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ booking configuration
def check_booking_config(functions: dict[str, dict], health: dict, booking_mode: str | None, approval_ref: str,
                         expected_kill_switch: str = "disengaged") -> tuple[dict[str, bool], list[str], dict]:
    """functions: FunctionName -> {"booking", "data_class", "env"} read from the DEPLOYED configuration (not inferred from
    names). Returns (checks, discrepancies, evidence). Read-only: judges, never changes."""
    on = {n for n, f in functions.items() if str(f.get("booking")).lower() == "true"}
    odd = {n: f.get("booking") for n, f in functions.items()
           if n not in EXPECTED_BOOKING_ON and f.get("booking") not in (None, "false")}
    disc = []
    for n in sorted(EXPECTED_BOOKING_ON - on):
        disc.append(f"{n}: booking is {functions.get(n, {}).get('booking')!r}, expected 'true'"
                    if n in functions else f"{n}: function not found in the deployed stack")
    for n in sorted(on - EXPECTED_BOOKING_ON):
        disc.append(f"{n}: booking is 'true', but only the api and agent may book")
    for n, v in sorted(odd.items()):
        if n not in on:
            disc.append(f"{n}: booking switch has an unexpected value {v!r}")
    env_bad = {n: (f.get("env"), f.get("data_class")) for n, f in functions.items()
               if n in EXPECTED_BOOKING_ON and (f.get("env") != "pilot" or f.get("data_class") != "synthetic")}
    disc += [f"{n}: environment {e!r} / data class {d!r}, expected 'pilot' / 'synthetic'" for n, (e, d) in env_bad.items()]
    synth_bad = sorted(n for n, f in functions.items() if f.get("data_class") not in (None, "synthetic"))
    disc += [f"{n}: data class {functions[n].get('data_class')!r}, expected 'synthetic'" for n in synth_bad]
    ks = health.get("booking_kill_switch")
    m = re.fullmatch(r"ON \((.+)\)", booking_mode or "")
    deployed_ref = m.group(1) if m else None
    checks = {
        "booking ON on exactly the api and agent functions": on == EXPECTED_BOOKING_ON,
        "every other function has booking OFF": not (on - EXPECTED_BOOKING_ON) and not odd,
        "api and agent run in the pilot environment with synthetic data": not env_bad,
        "every function is synthetic-data only": not synth_bad,
        "/health answers: status ok, database ok, booking ON": (health.get("status") == "ok"
                                                              and health.get("database") == "ok"
                                                              and health.get("booking_enabled") is True),
        "/health environment is the synthetic pilot": health.get("environment") == ENVIRONMENT,
        f"kill switch is in its expected state ({expected_kill_switch})": ks == expected_kill_switch,
        "the renewal names a well-formed owner approval reference": bool(APPROVAL_REF.fullmatch(approval_ref or "")),
        "the deployed stack's BookingMode names an owner approval": bool(deployed_ref and APPROVAL_REF.fullmatch(deployed_ref)),
    }
    if ks != expected_kill_switch:
        disc.append(f"kill switch is {ks!r}, expected {expected_kill_switch!r}")
    if not checks["/health answers: status ok, database ok, booking ON"]:
        disc.append(f"/health: status {health.get('status')!r}, database {health.get('database')!r}, "
                    f"booking_enabled {health.get('booking_enabled')!r}")
    if not checks["/health environment is the synthetic pilot"]:
        disc.append(f"/health environment {health.get('environment')!r}, expected {ENVIRONMENT!r}")
    if not checks["the renewal names a well-formed owner approval reference"]:
        disc.append("the owner approval reference is missing or malformed (OWNER-APPROVAL-YYYYMMDD-<ref>)")
    if not checks["the deployed stack's BookingMode names an owner approval"]:
        disc.append(f"deployed BookingMode is {booking_mode!r}, expected 'ON (OWNER-APPROVAL-…)'")
    ev = {"booking_on": sorted(on), "functions": {n: {k: f.get(k) for k in ("booking", "env", "data_class")}
                                                   for n, f in sorted(functions.items())},
          "kill_switch": ks, "health": {k: health.get(k) for k in ("status", "database", "booking_enabled",
                                                                      "environment", "booking_kill_switch",
                                                                      "gate_exceptions", "version")},
          "deployed_booking_mode": booking_mode, "deployed_approval_ref": deployed_ref, "renewal_approval_ref": approval_ref}
    return checks, disc, ev


# ------------------------------------------------------------------------------------------------ owner attribution
def load_owner_attributions(record: dict | None) -> tuple[dict[str, dict], list[str]]:
    """The Category C record, validated: owner confirmation present, category C, every named execution inside
    OWNER_CONFIRMED_IDS, none twice. Returns ({approval_id: entry}, problems). Any problem rejects the whole record."""
    if not record:
        return {}, []
    problems = []
    if record.get("category") != "C":
        problems.append("owner attribution record is not category C")
    conf = record.get("owner_confirmation") or {}
    if not (conf.get("confirmed_by") == "owner" and conf.get("statement")):
        problems.append("owner attribution record has no owner confirmation")
    if record.get("environment") != ENVIRONMENT:
        problems.append(f"owner attribution record is for {record.get('environment')!r}, not {ENVIRONMENT!r}")
    out: dict[str, dict] = {}
    for e in record.get("executions") or []:
        aid = str(e.get("approval_id"))
        if aid not in OWNER_CONFIRMED_IDS:
            problems.append(f"owner attribution names {short(aid)}, which is outside the two executions the owner "
                            "confirmed")
        elif aid in out:
            problems.append(f"owner attribution names {short(aid)} twice")
        else:
            out[aid] = e
    return ({} if problems else out), problems


# ------------------------------------------------------------------------------------------------ smoke records
def smoke_claims(smoke_records: list[dict]) -> dict[str, list[dict]]:
    """approval_id -> the smoke records that name it. Renewal format: record["executions"]; the 23 Nov format: the one
    request in evidence.booking.request with its execution in evidence.workflow.execution_arn."""
    claims: dict[str, list[dict]] = {}
    for s in smoke_records:
        named = []
        for x in s.get("executions") or []:
            named.append({"approval_id": str(x.get("approval_id")), "shipment_id": x.get("shipment_id"),
                          "facility_id": x.get("facility_id"), "execution_arn": x.get("execution_arn")})
        ev = s.get("evidence") or {}
        req, arn = (ev.get("booking") or {}).get("request") or [], (ev.get("workflow") or {}).get("execution_arn")
        if not s.get("executions") and req and arn:
            aid = str(req[0][2])
            if str(arn).endswith(":" + aid):
                named.append({"approval_id": aid, "shipment_id": ((ev.get("booking") or {}).get("driver") or {})
                              .get("shipment_id"), "facility_id": s.get("facility_id", "FAC-JAI-01"),
                              "execution_arn": arn})
        for n in named:
            claims.setdefault(n["approval_id"], []).append({**n, "record": s})
    return claims


def fresh_smoke_checks(smoke: dict | None, release_sha: str, artifact_sha256: str | None,
                       collected_at: datetime) -> tuple[dict[str, bool], list[str]]:
    """The mandatory fresh smoke run for THIS renewal: renewal format, passed, this environment, the target release SHA,
    run against the deployed code hash, at most SMOKE_MAX_AGE old."""
    if not smoke:
        return {"a fresh smoke test record is supplied": False}, ["no fresh smoke test record (scripts/smoke_option_d.py "
                                                                  "--release-sha <sha>) was supplied"]
    t = parse_ts(smoke.get("collected_utc"))
    names = list((smoke.get("checks") or {}).keys())
    missing = [p.strip() for p in SMOKE_REQUIRED if not any(n.startswith(p) for n in names)]
    checks = {
        "the smoke record is in the renewal format (smoke id, release, environment, executions)": bool(
            smoke.get("smoke_id") and smoke.get("release_sha") and smoke.get("environment") and smoke.get("executions")),
        "every smoke check passed": bool(smoke.get("all_pass")) and bool(smoke.get("checks"))
                                    and all((smoke.get("checks") or {}).values()),
        "every required smoke check is present (1-7)": not missing,
        "the smoke ran on this environment": smoke.get("environment") == ENVIRONMENT,
        "the smoke ran on the target release SHA": bool(release_sha) and smoke.get("release_sha") == release_sha,
        "the smoke ran against the deployed code (same artifact hash)": bool(artifact_sha256)
                                                                       and smoke.get("deployed_code_sha256") == artifact_sha256,
        f"the smoke is fresh (at most {int(SMOKE_MAX_AGE.total_seconds() // 3600)} h old)": bool(
            t and timedelta(0) <= collected_at - t <= SMOKE_MAX_AGE),
    }
    diag = [f"fresh smoke: {k}" for k, ok in checks.items() if not ok]
    if missing:
        diag.append("fresh smoke: missing checks " + ", ".join(missing))
    if smoke.get("release_sha") and release_sha and smoke.get("release_sha") != release_sha:
        diag.append(f"fresh smoke ran on {str(smoke.get('release_sha'))[:12]}, not the target {release_sha[:12]}")
    return checks, diag


# ------------------------------------------------------------------------------------------------ attribution
def _transitions(sfn_status: str, row: dict) -> list[str]:
    """The workflow's outcome and the database record must agree."""
    bad = []
    st, appt = row.get("status"), row.get("appointment_status")
    if sfn_status == "SUCCEEDED" and st == "OPEN":
        bad.append("the execution finished but the approval is still OPEN")
    if sfn_status == "RUNNING" and st not in ("OPEN",):
        bad.append(f"the execution is still running but the approval is {st}")
    if st == "APPROVED" and appt in (None, "PENDING_CONFIRMATION"):
        bad.append(f"the approval is APPROVED but the appointment is {appt}")
    if st in ("EXPIRED", "REJECTED") and appt == "CONFIRMED":
        bad.append(f"the approval is {st} but the appointment is CONFIRMED")
    if st in ("APPROVED", "REJECTED") and row.get("decided_by_type") != "HUMAN":
        bad.append(f"the approval was {st} by {row.get('decided_by_type')!r}, not a person")
    return bad


def _owner_expectations(entry: dict, row: dict, execution: dict) -> list[str]:
    exp, sf, bad = entry.get("expected_record") or {}, entry.get("step_functions") or {}, []
    if exp.get("appointment_id") and row.get("appointment_id") != exp["appointment_id"]:
        bad.append(f"appointment {row.get('appointment_id')} is not the one in the owner's record "
                   f"({exp['appointment_id']})")
    if exp.get("approval_status") and row.get("status") not in exp["approval_status"]:
        bad.append(f"approval status {row.get('status')} is not one of {exp['approval_status']}")
    if "decided_by_type" in exp and row.get("decided_by_type") not in exp["decided_by_type"]:
        bad.append(f"decided by {row.get('decided_by_type')!r}, the record says {exp['decided_by_type']}")
    if exp.get("decider_kind") and row.get("decider_kind") != exp["decider_kind"]:
        bad.append(f"decider kind {row.get('decider_kind')!r}, the record says {exp['decider_kind']!r}")
    win = exp.get("decided_between_utc")
    if win:
        d, lo, hi = parse_ts(row.get("decided_at")), parse_ts(win[0]), parse_ts(win[1])
        if not (d and lo <= d <= hi):
            bad.append(f"decided at {row.get('decided_at')}, outside the recorded window {win[0]}..{win[1]}")
    if exp.get("appointment_not_in") and row.get("appointment_status") in exp["appointment_not_in"]:
        bad.append(f"appointment is {row.get('appointment_status')}, which the record rules out")
    if sf.get("execution_status") and execution.get("status") != sf["execution_status"]:
        bad.append(f"execution status {execution.get('status')}, the record says {sf['execution_status']}")
    s1, s2 = parse_ts(execution.get("start")), parse_ts(sf.get("started_utc"))
    if s2 and not (s1 and abs((s1 - s2).total_seconds()) <= 2):
        bad.append(f"execution started {execution.get('start')}, the record says {sf.get('started_utc')}")
    return bad


API_LOG_ACCEPTED = ("verified", "previously_verified")


def attribute_executions(executions: list[dict], records: dict[str, dict], smoke_records: list[dict],
                         owner_record: dict | None, api_log: dict[str, dict] | None = None
                         ) -> tuple[list[dict], list[str]]:
    """executions: [{"name" (= approval id), "status", "start", "arn"}] from Step Functions.
    records: approval_id -> the row from approval_execution_evidence (migration 0029), facility-scoped.
    api_log: approval_id -> the API-log corroboration of the owner record's console requests (Recovery Release A,
    check 1): {"status": verified | previously_verified | missing | beyond_retention_unverified, ...}. Category C needs
    verified (the live API log has every request line the record names) or previously_verified (the log is past its
    retention and an earlier committed report verified the same lines). Anything else, or no corroboration at all,
    fails closed: an owner record alone can never attribute an execution.
    Returns (one verdict per execution, record-level problems)."""
    owner, owner_problems = load_owner_attributions(owner_record)
    claims = smoke_claims(smoke_records)
    out = []
    for e in executions:
        aid = str(e["name"])
        row = records.get(aid)
        v = {"approval_id": aid, "execution": short(aid), "execution_status": e.get("status"),
             "started": str(e.get("start")), "category": None, "verdict": UNRESOLVED, "reasons": [], "record": None}
        if row is None:
            v["reasons"].append("no approval record is visible for this execution under the pilot facility scope")
            out.append(v)
            continue
        v["record"] = {k: (str(row[k]) if row.get(k) is not None else None)
                       for k in ("kind", "facility_id", "shipment_id", "appointment_id", "appointment_status", "status",
                                 "decided_by_type", "decider_kind", "created_at", "decided_at")}
        hard = []
        if not (row.get("shipment_is_synthetic") and str(row.get("shipment_id") or "").startswith("SYN-")):
            hard.append(f"shipment {row.get('shipment_id')!r} is not synthetic")
        if row.get("facility_id") not in PILOT_FACILITIES:
            hard.append(f"facility {row.get('facility_id')!r} is not a pilot facility")
        if e.get("status") in ("FAILED", "TIMED_OUT", "ABORTED"):
            hard.append(f"the execution ended {e.get('status')}")
        hard += _transitions(e.get("status"), row)
        cats = []
        if str(row.get("shipment_id") or "").startswith("SYN-STEP6-"):
            cats.append(("A", "Step 6 drill request (SYN-STEP6-*)"))
        for c in claims.get(aid, []):
            s = c["record"]
            sb = []
            if not s.get("all_pass"):
                sb.append("the smoke record that names it did not pass")
            if s.get("environment", ENVIRONMENT) != ENVIRONMENT:
                sb.append(f"the smoke record is for {s.get('environment')!r}")
            if c.get("shipment_id") and c["shipment_id"] != row.get("shipment_id"):
                sb.append(f"the smoke record's shipment {c['shipment_id']} is not the record's {row.get('shipment_id')}")
            if c.get("facility_id") and c["facility_id"] != row.get("facility_id"):
                sb.append(f"the smoke record's facility {c['facility_id']} is not the record's {row.get('facility_id')}")
            if sb:
                hard += sb
            else:
                cats.append(("B", "smoke test " + (s.get("smoke_id") or f"of {s.get('collected_utc')}")
                             + (f" on release {str(s['release_sha'])[:12]}" if s.get("release_sha")
                                else " (23 Nov format: release SHA not recorded)")))
        if aid in owner:
            ob = _owner_expectations(owner[aid], row, e)
            corr = (api_log or {}).get(aid) or {"status": "not checked"}
            if corr.get("status") not in API_LOG_ACCEPTED:
                ob.append(f"the API log does not corroborate the recorded console requests ({corr.get('status')}"
                          + (f": {corr['detail']}" if corr.get("detail") else "") + ")")
            if ob:
                hard += [f"owner-confirmed record does not match the database: {b}" for b in ob]
            else:
                cats.append(("C", "owner-confirmed manual console test: " + owner[aid].get("label", "")))
        if len(cats) > 1:
            hard.append("attributed more than once: " + ", ".join(c for c, _ in cats))
        v["reasons"] += hard
        if hard:
            v["verdict"] = FAIL
        elif cats:
            v["category"], v["verdict"] = cats[0][0], "ATTRIBUTED"
            v["reasons"].append(cats[0][1])
        else:
            v["reasons"].append("no attributable evidence (not a Step 6 drill, no smoke record names it, not "
                                "owner-confirmed)")
        out.append(v)
    return out, owner_problems


# ------------------------------------------------------------------------------------------------ the gate
def booking_workflow_renewal(base_checks: dict[str, bool], config: tuple[dict, list, dict],
                             attribution: tuple[list[dict], list[str]], smoke: tuple[dict, list],
                             fresh_smoke_record: dict | None) -> dict:
    """The renewal-mode BOOKING_WORKFLOW gate. base_checks: the activation checks that hold in both modes (the live
    alias, the published definition, no failed execution). Status: FAIL if anything contradicts; UNRESOLVED if nothing
    contradicts but some execution has no evidence; PASS only when every check holds and every execution is attributed."""
    cfg_checks, cfg_disc, cfg_ev = config
    verdicts, owner_problems = attribution
    smk_checks, smk_diag = smoke
    fresh_ids = {str(x.get("approval_id")) for x in (fresh_smoke_record or {}).get("executions") or []}
    fresh_attributed = bool(fresh_ids) and all(
        any(v["approval_id"] == i and v["verdict"] == "ATTRIBUTED" and v["category"] == "B" for v in verdicts)
        for i in fresh_ids)
    checks = {**base_checks, **{f"config: {k}": ok for k, ok in cfg_checks.items()},
              **{f"smoke: {k}": ok for k, ok in smk_checks.items()},
              "smoke: the fresh smoke's own executions are attributed to it (category B)": fresh_attributed,
              "owner attribution record is valid and limited to the two confirmed executions": not owner_problems,
              "no approval execution contradicts its evidence": not any(v["verdict"] == FAIL for v in verdicts),
              "every approval execution is attributed (A drill, B smoke, C owner-confirmed)":
                  all(v["verdict"] == "ATTRIBUTED" for v in verdicts)}
    diagnostics = list(cfg_disc) + list(smk_diag) + list(owner_problems)
    for v in verdicts:
        if v["verdict"] == FAIL:
            diagnostics.append(f"approval execution {v['execution']}: " + "; ".join(v["reasons"]))
        elif v["verdict"] == UNRESOLVED:
            diagnostics.append(f"approval execution {v['execution']} has no attributable evidence: "
                               + "; ".join(v["reasons"]))
    contradicted = [k for k, ok in checks.items()
                    if not ok and k != "every approval execution is attributed (A drill, B smoke, C owner-confirmed)"]
    status = FAIL if contradicted else (UNRESOLVED if not all(checks.values()) else PASS)
    by_cat = {c: sorted(v["execution"] for v in verdicts if v["category"] == c) for c in ("A", "B", "C")}
    return {"mode": "renewal", "status": status, "renewal_status": status, "checks": checks,
            "not_shown": [] if status == PASS else [f"BOOKING_WORKFLOW: {status} — {d}" for d in diagnostics],
            "diagnostics": [f"BOOKING_WORKFLOW: {status} — {d}" for d in diagnostics],
            "evidence": {"booking_configuration": cfg_ev, "approval_executions": verdicts,
                         "attribution_summary": {**by_cat,
                                                 "unresolved": sorted(v["execution"] for v in verdicts
                                                                      if v["verdict"] == UNRESOLVED),
                                                 "failed": sorted(v["execution"] for v in verdicts if v["verdict"] == FAIL)},
                         "fresh_smoke": fresh_smoke_record and {k: fresh_smoke_record.get(k) for k in
                                                                ("smoke_id", "release_sha", "environment",
                                                                 "collected_utc", "deployed_code_sha256", "all_pass",
                                                                 "executions")}}}


def renewal_status(gate: str, g: dict) -> str:
    """The explicit renewal wording for a gate collected by the activation rules. An approved exception keeps its scope:
    only its own gate, only from its own activation status, and only with every check passing."""
    st = g.get("status")
    checks_ok = all((g.get("checks") or {}).values())
    if gate == "BOOKING_WORKFLOW":
        return g.get("renewal_status", st if st in (PASS, FAIL, UNRESOLVED) else FAIL)
    if st == PASS and checks_ok and not g.get("not_shown"):
        return PASS
    exc = APPROVED_EXCEPTIONS.get(gate)
    if exc and st == exc[0] and checks_ok:
        return exc[1]
    return FAIL


# ------------------------------------------------------------------------------------------------ observed booking state
# M-2 (Recovery Release A hardening): a report records the booking state it OBSERVED on the deployed pilot's /health,
# never a value the collector assumes. "unavailable" means it could not be verified, and no validation accepts it.
BOOKING_STATES = {"off": "booking disabled (the booking switch is off)",
                  "refused": "booking enabled but not authorized by the database",
                  "authorized": "booking enabled and authorized by the database"}


def observe_booking_state(api_url: str | None, get=None, now: datetime | None = None) -> dict:
    """GET <ApiUrl>/health once and reduce it to one of BOOKING_STATES, or 'unavailable' with the reason. Since D4
    (0033) /health reports the database's verdict (booking_state). The older format (before D4) has only
    booking_enabled, which is the start-up switch: false is 'off'; true cannot say whether the database authorizes
    booking now, so it is 'unavailable'."""
    now = now or datetime.now(timezone.utc)
    src = api_url.rstrip("/") + "/health" if api_url else None
    obs = {"source": src, "observed_utc": now.isoformat(timespec="seconds"), "http_status": None,
           "booking_state": "unavailable", "booking_enabled": None}
    if not src:
        return {**obs, "error": "no ApiUrl: the deployed pilot's /health could not be read"}
    try:
        if get is None:
            import httpx
            get = lambda u: httpx.get(u, timeout=40)                  # noqa: E731
        r = get(src)
        obs["http_status"] = r.status_code
        body = r.json()
    except Exception as e:                                          # noqa: BLE001 — unavailable, never assumed
        return {**obs, "error": f"the health endpoint is unavailable ({type(e).__name__})"}
    if r.status_code != 200 or not isinstance(body, dict):
        return {**obs, "error": f"the health endpoint answered HTTP {r.status_code} without a JSON object"}
    enabled = body.get("booking_enabled")
    if "booking_state" in body:
        state, fmt = body.get("booking_state"), "D4 (the database's verdict)"
    elif enabled is False:
        state, fmt = "off", "before D4 (booking_enabled is the start-up switch)"
    else:
        return {**obs, "error": f"a health response before D4 with booking_enabled {enabled!r} cannot show whether "
                                "the database authorizes booking"}
    if state not in BOOKING_STATES or not isinstance(enabled, bool) or enabled != (state == "authorized"):
        return {**obs, "error": f"malformed or inconsistent health response (booking_state {state!r}, "
                                f"booking_enabled {enabled!r})"}
    obs.update(booking_state=state, booking_enabled=enabled, meaning=BOOKING_STATES[state], format=fmt)
    for k in ("booking_switch", "booking_refusal", "booking_kill_switch", "booking_provisional_until", "version"):
        if k in body:
            obs[k] = body[k]
    return obs


def booking_observation_problems(report: dict, allowed: tuple[str, ...], context: str) -> list[str]:
    """Why the report's observed booking state is not acceptable here (empty = it is)."""
    obs = report.get("booking_observed")
    if not isinstance(obs, dict) or obs.get("booking_state") not in BOOKING_STATES:
        why = (obs or {}).get("error") if isinstance(obs, dict) else None
        return [f"the report records no verified booking state of the running pilot ({why or 'not observed'})"]
    p = []
    if not (obs.get("source") and parse_ts(obs.get("observed_utc"))):
        p.append("the observed booking state has no source or time")
    if report.get("booking_enabled") is not obs.get("booking_enabled"):
        p.append(f"the report's booking_enabled {report.get('booking_enabled')!r} is not what was observed on the "
                 f"running pilot ({obs.get('booking_enabled')!r})")
    if obs["booking_state"] not in allowed:
        p.append(f"{context}: the running pilot was observed {obs['booking_state']!r} "
                 f"({BOOKING_STATES[obs['booking_state']]}); expected {' or '.join(allowed)}")
    return p


# ------------------------------------------------------------------------------------------------ writing evidence
def renewal_report_problems(report: dict, manifest: dict, approval_ref: str | None = None,
                            now: datetime | None = None) -> list[str]:
    """Why a renewal report may NOT become evidence for this deployment (empty list = it may). Used by
    setuhaul.ops.gate_evidence before any row is written."""
    now = now or datetime.now(timezone.utc)
    p = []
    if report.get("mode") != "renewal":
        p.append("the report is not a renewal report")
    p += booking_observation_problems(report, ("authorized",),
                                      "a renewal report must come from the active pilot (booking ON and authorized)")
    if report.get("environment") != ENVIRONMENT:
        p.append(f"the renewal report is for {report.get('environment')!r}, not {ENVIRONMENT!r}")
    t = parse_ts(report.get("collected_utc"))
    if not (t and timedelta(0) <= now - t <= REPORT_MAX_AGE):
        p.append(f"the renewal report is not fresh (collected {report.get('collected_utc')}; at most "
                 f"{int(REPORT_MAX_AGE.total_seconds() // 3600)} h)")
    art = ((manifest.get("gates") or {}).get("RECOVERY_WORKER") or {}).get("artifact_sha256")
    if not art or report.get("artifact_sha256") != art:
        p.append("the renewal report was collected on different code than this deployment "
                 f"(report {str(report.get('artifact_sha256'))[:12]}, deployment {str(art)[:12]})")
    if approval_ref is not None and report.get("approval_ref") != approval_ref:
        p.append(f"the renewal report names approval {report.get('approval_ref')!r}, not {approval_ref!r}")
    bw = (report.get("gates") or {}).get("BOOKING_WORKFLOW") or {}
    if bw.get("mode") != "renewal" or bw.get("status") != PASS:
        p.append(f"BOOKING_WORKFLOW is {bw.get('status')!r} in renewal mode, not PASS")
    smoke = (bw.get("evidence") or {}).get("fresh_smoke") or {}
    if smoke.get("release_sha") != report.get("release_sha") or not report.get("release_sha"):
        p.append("the fresh smoke is not bound to the report's release SHA")
    return p
