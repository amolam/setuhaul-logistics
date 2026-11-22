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
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
    a = ap.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
