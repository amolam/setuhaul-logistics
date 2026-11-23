"""Option D activation path: turn a Step 6 gate report into `deployment_evidence` rows (pipeline identity only).

    python -m setuhaul.ops.gate_evidence --report docs/release/evidence/step6_gates.json \
        --manifest cdk.out/deployment_manifest.json --artifact-uri s3://<bucket>/<run>/step6_gates.json [--write]

The booking switch (`SETUHAUL_BOOKING_ENABLED`) only starts when every one of the six gates has a current row for
THIS deployment whose `config_fingerprint` matches what the service computes live (`check_booking_preconditions`,
setuhaul/api/inbound.py). This module computes the same fingerprints, so a row written here is accepted only while
the configuration it describes is the one running.

Rules (owner boundary, docs/operations/DEPLOYMENT_GATES.md):
- only a gate whose report status is PASS becomes a row; PARTIAL, FAIL and BLOCKED are refused and listed;
- the row is written by the `setuhaul_deploy_evidence` login (append-only table, triggers 0013/0014), never by an
  application role or the owner; this module never chooses the identity, the caller's connection does;
- the default is a dry run that prints the rows; `--write` is needed to insert;
- writing evidence does not turn booking on: that stays a separate deployment change and owner decision.

Option D activation (migration 0026; OWNER-APPROVAL-20261123-OPTION-D) adds, in the same pipeline step:
- the deployment manifest is recorded in `deployment_manifests`, so the functions can find it and check it against
  their own code hash;
- an owner-approved exception is written to `deployment_exceptions` (never to `deployment_evidence`) only for the three
  the owner accepted, and only when its own condition holds at write time:
    AURORA             T33-P                        the report says PASS WITH PILOT EXCEPTION (T33-P), every check
                                                    passes, and the live server is PostgreSQL 17.9+ (not 18);
    WORKLOAD_IDENTITY  NO-LIVE-DRIFT-DETECTION      every check passes and the only item not shown is drift detection;
    OUTBOUND_DELIVERY  META-DEFERRED-TEST-SINK-ONLY the deployed sender is the internal test sink with WhatsApp off;
- never for a production environment (the database refuses it too).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from setuhaul.api.inbound import (REQUIRED_GATES, database_fingerprint, deployment_id_for,
                                  manifest_fingerprints, manifest_problems)

OPERATIONAL = {"AWS_QUEUE", "RECOVERY_WORKER", "OUTBOUND_DELIVERY", "BOOKING_WORKFLOW"}   # 0014: 7-day window
VALIDITY_DAYS = {g: (7 if g in OPERATIONAL else 30) for g in REQUIRED_GATES}


class EvidenceRefused(ValueError):
    pass


def live_fingerprints(manifest: dict, queue, db) -> dict[str, str]:
    """The same values check_booking_preconditions compares against."""
    return {"AWS_QUEUE": queue.fingerprint(), "AURORA": database_fingerprint(db), **manifest_fingerprints(manifest)}


def rows_from_report(report: dict, manifest: dict, environment: str, artifact_uri: str, evidence: bytes,
                     fingerprints: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """Rows for the gates that PASSED, and the reason each other gate was refused."""
    if problems := manifest_problems(manifest, environment):
        raise EvidenceRefused("deployment manifest: " + "; ".join(problems))
    if not artifact_uri.startswith(("s3://", "https://")):
        raise EvidenceRefused("the evidence artifact must be stored at an s3:// or https:// location")
    if report.get("booking_enabled") is not False:
        raise EvidenceRefused("the report must come from a run with the booking switch off")
    dep, digest = deployment_id_for(manifest), hashlib.sha256(evidence).hexdigest()
    rows, refused = [], {}
    for gate in REQUIRED_GATES:
        g = report.get("gates", {}).get(gate)
        if not isinstance(g, dict):
            refused[gate] = "not in the report"
            continue
        failing = [k for k, ok in g.get("checks", {}).items() if not ok]
        if g.get("status") != "PASS" or failing or g.get("not_shown"):
            refused[gate] = f"status {g.get('status')}" + (f"; failing: {', '.join(failing)}" if failing else "") + (
                f"; not shown: {len(g.get('not_shown', []))} item(s)" if g.get("not_shown") else "")
            continue
        rows.append({"environment": environment, "deployment_id": dep, "gate": gate,
                     "validity_days": VALIDITY_DAYS[gate], "artifact_uri": artifact_uri,
                     "evidence_sha256": digest, "config_fingerprint": fingerprints[gate]})
    return rows, refused


EXCEPTION_REASONS = {
    "AURORA": "T33-P: PostgreSQL 17.9 accepted for the synthetic Sydney pilot only; production stays 18.3+ (T33).",
    "WORKLOAD_IDENTITY": "All five acceptance checks pass live; live drift detection (AWS Config) is not on the Free "
                         "plan and is accepted as a pilot exception; IAM permission fingerprinting continues.",
    "OUTBOUND_DELIVERY": "Meta integration deferred: notices go to the internal test inbox only; real WhatsApp delivery "
                         "stays disabled.",
}
EXCEPTION_DAYS = 7          # as short as the operational evidence: the owner re-confirms with each weekly re-run


def exception_rows(report: dict, manifest: dict, environment: str, approval_ref: str, fingerprints: dict[str, str],
                   server_version_num: int, outbound_env: dict) -> tuple[list[dict], dict[str, str]]:
    """Rows for the owner-approved exceptions whose condition holds now, and why any other could not be written."""
    from setuhaul.api.inbound import EXCEPTION_CODES
    if "prod" in environment or not environment.startswith("pilot"):
        raise EvidenceRefused("owner-approved exceptions apply to the pilot only, never to production")
    if not re.fullmatch(r"OWNER-APPROVAL-20\d{6}-[A-Za-z0-9-]{3,40}", approval_ref or ""):
        raise EvidenceRefused("the owner's written approval reference is required (OWNER-APPROVAL-YYYYMMDD-<ref>)")
    dep, gates = deployment_id_for(manifest), report.get("gates", {})
    rows, refused = [], {}

    def ok_checks(g):
        v = gates.get(g) or {}
        return v, bool(v.get("checks")) and all(v["checks"].values())

    v, ok = ok_checks("AURORA")
    if v.get("status") == "PASS WITH PILOT EXCEPTION (T33-P)" and ok and 170009 <= server_version_num < 180000:
        rows.append("AURORA")
    else:
        refused["AURORA"] = f"T33-P does not hold (status {v.get('status')}, checks {ok}, server {server_version_num})"
    v, ok = ok_checks("WORKLOAD_IDENTITY")
    ns = v.get("not_shown") or []
    if v.get("status") in ("PASS", "PARTIAL") and ok and len(ns) == 1 and "drift detection" in ns[0]:
        rows.append("WORKLOAD_IDENTITY")
    else:
        refused["WORKLOAD_IDENTITY"] = f"only drift detection may be missing (status {v.get('status')}, checks {ok}, " \
                                       f"not shown {len(ns)})"
    if outbound_env.get("SETUHAUL_NOTIFICATION_MODE") == "test_sink" and outbound_env.get("SETUHAUL_WHATSAPP_ENABLED") == "false":
        rows.append("OUTBOUND_DELIVERY")
    else:
        refused["OUTBOUND_DELIVERY"] = "the deployed sender is not the test sink with WhatsApp off"
    return [{"environment": environment, "deployment_id": dep, "gate": g, "exception_code": EXCEPTION_CODES[g],
             "approval_ref": approval_ref, "reason": EXCEPTION_REASONS[g], "validity_days": EXCEPTION_DAYS,
             "config_fingerprint": fingerprints[g]} for g in rows], refused


def record_manifest(conn, manifest: dict, environment: str) -> str:
    dep = deployment_id_for(manifest)
    conn.execute("""INSERT INTO setuhaul.deployment_manifests (environment, deployment_id, artifact_sha256, manifest)
                    VALUES (%s, %s, %s, %s::jsonb)""",
                 (environment, dep, manifest["gates"]["RECOVERY_WORKER"]["artifact_sha256"],
                  json.dumps(manifest, sort_keys=True)))
    return dep


def write_exceptions(conn, rows: list[dict]) -> int:
    for r in rows:
        conn.execute("""INSERT INTO setuhaul.deployment_exceptions (environment, deployment_id, gate, exception_code,
                               approval_ref, reason, config_fingerprint, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s))""",
                     (r["environment"], r["deployment_id"], r["gate"], r["exception_code"], r["approval_ref"],
                      r["reason"], r["config_fingerprint"], r["validity_days"]))
    return len(rows)


def activate(conn, report: dict, raw_report: bytes, manifest: dict, environment: str, artifact_uri: str,
             approval_ref: str, fingerprints: dict[str, str], server_version_num: int, outbound_env: dict) -> dict:
    """One transaction on the deploy-evidence login: the manifest, the PASS evidence and the owner-approved exceptions.
    Every gate must be covered by one or the other, or nothing is written."""
    rows, refused = rows_from_report(report, manifest, environment, artifact_uri, raw_report, fingerprints)
    exc, exc_refused = exception_rows(report, manifest, environment, approval_ref, fingerprints, server_version_num,
                                      outbound_env)
    covered = {r["gate"] for r in rows} | {r["gate"] for r in exc}
    missing = {g: exc_refused.get(g) or refused.get(g) for g in REQUIRED_GATES if g not in covered}
    if missing:
        raise EvidenceRefused("not every gate is covered by PASS evidence or an owner-approved exception: "
                              + "; ".join(f"{g}: {w}" for g, w in missing.items()))
    dep = record_manifest(conn, manifest, environment)
    write(conn, rows)
    write_exceptions(conn, exc)
    return {"deployment_id": dep, "evidence": sorted(r["gate"] for r in rows),
            "exceptions": {r["gate"]: r["exception_code"] for r in exc}, "approval_ref": approval_ref}


def write(conn, rows: list[dict]) -> int:
    """Insert through the caller's connection (the deploy-evidence login). The table's triggers refuse anything stale,
    future-dated or longer than the gate's window."""
    for r in rows:
        conn.execute("""INSERT INTO setuhaul.deployment_evidence (environment, deployment_id, gate, expires_at,
                               artifact_uri, evidence_sha256, config_fingerprint)
                        VALUES (%s, %s, %s, now() + make_interval(days => %s), %s, %s, %s)""",
                     (r["environment"], r["deployment_id"], r["gate"], r["validity_days"], r["artifact_uri"],
                      r["evidence_sha256"], r["config_fingerprint"]))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--environment", default="pilot")
    ap.add_argument("--artifact-uri", required=True)
    ap.add_argument("--write", action="store_true", help="insert the rows (needs the deploy-evidence login)")
    ap.add_argument("--activate", metavar="OWNER-APPROVAL-YYYYMMDD-REF",
                    help="Option D: record the manifest, PASS evidence and the owner-approved exceptions (pipeline)")
    ap.add_argument("--template", help="the deployed CloudFormation template (for the sender's configuration)")
    a = ap.parse_args(argv)
    if a.activate:
        return activate_from_pipeline(a)
    raw = Path(a.report).read_bytes()
    report, manifest = json.loads(raw), json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    eligible = [g for g, v in report.get("gates", {}).items() if v.get("status") == "PASS"]
    if not eligible:
        print(json.dumps({"rows": [], "refused": {g: v.get("status") for g, v in report.get("gates", {}).items()},
                          "note": "no gate passed: nothing to write"}, indent=1))
        return 0
    from setuhaul.api.inbound import SqsFifoQueue          # live fingerprints need the real queue and database
    import boto3
    from setuhaul.aws.data_api import DataApiDb
    db = DataApiDb.from_env()
    queue = SqsFifoQueue(__import__("os").environ["SETUHAUL_QUEUE_URL"], boto3.client("sqs"))
    rows, refused = rows_from_report(report, manifest, a.environment, a.artifact_uri, raw,
                                     live_fingerprints(manifest, queue, db))
    print(json.dumps({"rows": rows, "refused": refused, "write": a.write}, indent=1))
    if a.write and rows:
        raise SystemExit("--write runs only in the deployment pipeline with the deploy-evidence login "
                         "(SETUHAUL_DB_SECRET_ARNS must hold only 'deploy_evidence'); not available from this CLI yet")
    return 0


def activate_from_pipeline(a) -> int:
    """The pipeline step (.github/workflows/activate-booking.yml): runs only with the deploy-evidence login, which is
    the one login SETUHAUL_DB_SECRET_ARNS may hold here; the queue is read through the read-only lookup session."""
    import os
    import boto3
    from setuhaul.api.inbound import SqsFifoQueue, database_fingerprint
    from setuhaul.aws.data_api import Connection, begin_with_resume
    secrets = json.loads(os.environ["SETUHAUL_DB_SECRET_ARNS"])
    if set(secrets) != {"deploy_evidence"}:
        raise SystemExit("activation runs only with the deploy-evidence login (SETUHAUL_DB_SECRET_ARNS)")
    raw = Path(a.report).read_bytes()
    report, manifest = json.loads(raw), json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    template = json.loads(Path(a.template).read_text(encoding="utf-8"))
    outbound_env = next(r["Properties"]["Environment"]["Variables"] for r in template["Resources"].values()
                        if r["Type"] == "AWS::Lambda::Function"
                        and str(r["Properties"].get("FunctionName", "")).endswith("-outbound"))
    rds = boto3.client("rds-data")
    cluster, secret = os.environ["SETUHAUL_DB_CLUSTER_ARN"], secrets["deploy_evidence"]
    kw = {"resourceArn": cluster, "secretArn": secret, "database": os.environ.get("SETUHAUL_DB_NAME", "setuhaul")}

    class _Tx:                                              # database_fingerprint() reads with gate_role -> "system"
        def tx(self, _role, readonly=False):
            return _tx(readonly)

    from contextlib import contextmanager

    @contextmanager
    def _tx(readonly=False):
        tid = begin_with_resume(rds, kw, 120)
        c = Connection(rds, cluster, secret, kw["database"], tid)
        try:
            c.execute("SET LOCAL standard_conforming_strings = on")
            c.execute("SELECT set_config('role', 'setuhaul_deploy_evidence', true), set_config('search_path', 'setuhaul', true)")
            yield c
        except BaseException:
            rds.rollback_transaction(resourceArn=cluster, secretArn=secret, transactionId=tid)
            raise
        rds.commit_transaction(resourceArn=cluster, secretArn=secret, transactionId=tid)

    lookup = boto3.Session(**json.loads(os.environ["SETUHAUL_LOOKUP_SESSION"])) if os.environ.get(
        "SETUHAUL_LOOKUP_SESSION") else boto3.Session()
    queue = SqsFifoQueue(os.environ["SETUHAUL_QUEUE_URL"], lookup.client("sqs"))
    db = _Tx()
    fps = live_fingerprints(manifest, queue, db)
    with _tx() as c:
        ver = int(c.execute("SELECT current_setting('server_version_num')").fetchone()[0])
        out = activate(c, report, raw, manifest, a.environment, a.artifact_uri, a.activate, fps, ver, outbound_env)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
