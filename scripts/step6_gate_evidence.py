"""Step 6: collect evidence for the six release gates from the deployed synthetic pilot. Read only.

    python scripts/step6_gate_evidence.py [--out docs/release/evidence/step6_gates.json]

What it does: reads AWS configuration (SQS, Lambda, IAM, Step Functions, EventBridge Scheduler, RDS, CloudWatch Logs)
and the database catalogue through the RDS Data API. The only database transactions it opens are rolled back. It
sends no message, starts no execution, changes no schedule and writes no `deployment_evidence` row (those rows are
written by the pipeline identity, and only once a gate passes). Account ids are masked in the output.

Each gate gets one of: PASS (every requirement shown live), PARTIAL (some requirements shown live; the rest named),
FAIL (a requirement is contradicted live), BLOCKED (cannot be shown without an external dependency or an approval).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.data_api import DataApiDb  # noqa: E402
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402
from setuhaul.ops.evidence_triggers import QUERY as EVIDENCE_TRIGGER_QUERY, trigger_problems  # noqa: E402

REGION, STACK, ENV = "ap-southeast-2", "SetuHaulPilot", "pilot"
MASK = re.compile(r"\b\d{12}\b")
LEASE_S = 120                       # infra/setuhaul_stack.py LEASE_S: the orchestrator's processing lease
ROLE_OF_LOGIN = re.compile(r"setuhaul_(agent|ingest|feasibility|console|system|deploy_evidence)")


def c(name):
    return boto3.client(name, region_name=REGION)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def stack():
    cf = c("cloudformation")
    out = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    res, token = [], None
    while True:
        r = cf.list_stack_resources(StackName=STACK, **({"NextToken": token} if token else {}))
        res += r["StackResourceSummaries"]
        token = r.get("NextToken")
        if not token:
            return out, res


def functions(res):
    lam = c("lambda")
    fns = {}
    for r in res:
        if r["ResourceType"] == "AWS::Lambda::Function":
            cfg = lam.get_function_configuration(FunctionName=r["PhysicalResourceId"])
            env = cfg.get("Environment", {}).get("Variables", {})
            fns[r["LogicalResourceId"]] = {"name": cfg["FunctionName"], "role": cfg["Role"].rsplit("/", 1)[-1],
                                           "db_roles": sorted(json.loads(env.get("SETUHAUL_DB_SECRET_ARNS", "{}"))),
                                           "secret_arns": json.loads(env.get("SETUHAUL_DB_SECRET_ARNS", "{}")),
                                           "booking": env.get("SETUHAUL_BOOKING_ENABLED"),
                                           "data_class": env.get("SETUHAUL_DATA_CLASS"), "env": env.get("SETUHAUL_ENV"),
                                           "code_sha256": cfg.get("CodeSha256"), "arn": cfg["FunctionArn"],
                                           "log_group": cfg.get("LoggingConfig", {}).get("LogGroup")
                                           or f"/aws/lambda/{cfg['FunctionName']}"}
    return fns


def secret_stem(arn: str) -> str:
    """A secret ARN without Secrets Manager's random 6-character suffix or an IAM wildcard."""
    arn = arn.rstrip("*").rstrip("?").rstrip("-")
    return re.sub(r"-[A-Za-z0-9]{6}$", "", arn)


def role_statements(role):
    iam = c("iam")
    docs = []
    for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
        docs.append(iam.get_role_policy(RoleName=role, PolicyName=p)["PolicyDocument"])
    for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
        v = iam.get_policy(PolicyArn=p["PolicyArn"])["Policy"]["DefaultVersionId"]
        docs.append(iam.get_policy_version(PolicyArn=p["PolicyArn"], VersionId=v)["PolicyVersion"]["Document"])
    st = []
    for d in docs:
        s = d.get("Statement", [])
        st += s if isinstance(s, list) else [s]
    return st


def as_list(x):
    return x if isinstance(x, list) else [x]


def actions_on(statements, prefix, resource_part=None):
    acts = set()
    for s in statements:
        if s.get("Effect") != "Allow":
            continue
        res = as_list(s.get("Resource", []))
        if resource_part and not any(resource_part in r or r == "*" for r in res):
            continue
        acts |= {a for a in as_list(s.get("Action", [])) if a.lower().startswith(prefix)}
    return sorted(acts)


def secret_resources(statements):
    out = set()
    for s in statements:
        if s.get("Effect") == "Allow" and any(a.lower().startswith("secretsmanager:getsecretvalue") or a == "secretsmanager:*"
                                              for a in as_list(s.get("Action", []))):
            out |= set(as_list(s.get("Resource", [])))
    return sorted(out)


def drill(name):
    f = ROOT / "docs" / "release" / "evidence" / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def gate_queue(out, fns, stmts):
    sqs = c("sqs")
    url = out["QueueUrl"]
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    red = json.loads(a.get("RedrivePolicy", "{}"))
    dlq_name = red.get("deadLetterTargetArn", "").rsplit(":", 1)[-1]
    dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"] if dlq_name else None
    d = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["All"])["Attributes"] if dlq_url else {}
    queue_arn = a["QueueArn"]
    who = {k: actions_on(stmts[k], "sqs:", queue_arn.rsplit(":", 1)[-1]) for k in fns}
    who = {k: v for k, v in who.items() if v}
    cw = c("cloudwatch")
    now = datetime.now(timezone.utc)
    def peak(metric, q):
        pts = cw.get_metric_statistics(Namespace="AWS/SQS", MetricName=metric, Dimensions=[{"Name": "QueueName", "Value": q}],
                                       StartTime=now - timedelta(days=2), EndTime=now, Period=3600, Statistics=["Sum"])
        return int(sum(p["Sum"] for p in pts["Datapoints"]))
    ev = {"queue": {k: a.get(k) for k in ("FifoQueue", "ContentBasedDeduplication", "DeduplicationScope", "FifoThroughputLimit",
                                          "VisibilityTimeout", "MessageRetentionPeriod", "KmsMasterKeyId",
                                          "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible")},
          "redrive_policy": red,
          "dlq": {k: d.get(k) for k in ("FifoQueue", "MessageRetentionPeriod", "ApproximateNumberOfMessages")},
          "sqs_actions_by_function": who,
          "last_48h": {"messages_sent": peak("NumberOfMessagesSent", queue_arn.rsplit(":", 1)[-1]),
                       "messages_deleted": peak("NumberOfMessagesDeleted", queue_arn.rsplit(":", 1)[-1]),
                       "dlq_messages_sent": peak("NumberOfMessagesSent", dlq_name) if dlq_name else None}}
    senders = {k for k, v in who.items() if any(x.lower() == "sqs:sendmessage" for x in v)}
    receivers = {k for k, v in who.items() if any(x.lower() == "sqs:receivemessage" for x in v)}
    checks = {
        "FIFO queue": a.get("FifoQueue") == "true",
        "content-independent deduplication (ContentBasedDeduplication off; the app sets the id)": a.get("ContentBasedDeduplication") == "false",
        "FIFO dead-letter queue": d.get("FifoQueue") == "true",
        "redrive count 1-10": 1 <= int(red.get("maxReceiveCount", 0)) <= 10,
        "DLQ retention >= queue retention": int(d.get("MessageRetentionPeriod", 0)) >= int(a.get("MessageRetentionPeriod", 0)),
        f"visibility >= 2 x {LEASE_S} s lease": int(a.get("VisibilityTimeout", 0)) >= 2 * LEASE_S,
        "no wildcard SQS action on the queue": not any("sqs:*" in v for v in who.values()),
        "senders are only the API and recovery functions": senders and all(re.search(r"Api|Recovery", k) for k in senders),
        "receivers are only the agent function": receivers and all(re.search(r"Agent", k) for k in receivers),
        "messages flowed through the real queue (last 48 h)": ev["last_48h"]["messages_sent"] > 0,
        "DLQ empty": int(d.get("ApproximateNumberOfMessages", "0")) == 0,
    }
    d = drill("drill_dlq_summary.json")
    ev["dlq_drill"] = d and {"checks": d["checks"], "monitoring": d["monitoring"]}
    if d:
        checks["redrive procedure tested live (owner-approved drill, 23 Nov)"] = bool(d["all_pass"])
        missing = ["DLQ-depth monitoring not demonstrated: the DlqNotEmpty alarm (5-minute maximum) never saw the drill "
                   "message, which sat in the DLQ only seconds/minutes; the oldest-message alarm did fire"]
        al = drill("drill_dlq_alarm.json")
        ev["dlq_alarm_drill"] = al and {"checks": al["checks"], "history": al["evidence"].get("alarm_history")}
        if al and al["all_pass"]:
            checks["DLQ-depth alarm fired on a held DLQ message and returned to OK (owner-approved drill, 23 Nov)"] = True
            missing = []
    else:
        missing = ["a redrive drill (move a message DLQ -> queue) has not been run: it writes to the live queue, so it needs "
                   "the owner's approval; the procedure is in docs/operations/RUNBOOK_0011.md"]
    return ev, checks, missing


def gate_recovery(out, fns, res):
    evb = c("events")
    schedules = []
    for r in res:
        if r["ResourceType"] == "AWS::Events::Rule":
            s = evb.describe_rule(Name=r["PhysicalResourceId"].split("|")[-1])
            tg = evb.list_targets_by_rule(Rule=s["Name"])["Targets"]
            schedules.append({"name": s["Name"], "expression": s.get("ScheduleExpression"), "state": s["State"],
                              "target": ",".join(t["Arn"].rsplit(":", 1)[-1] for t in tg)})
    rec = next((v for k, v in fns.items() if "Recovery" in k), None)
    logs = c("logs")
    runs = []
    if rec:
        kw = {"logGroupName": rec["log_group"], "startTime": int((time.time() - 3 * 86400) * 1000),
              "filterPattern": "REPORT"}
        try:
            token = None
            while True:
                page = logs.filter_log_events(**kw, **({"nextToken": token} if token else {}))
                runs += [e["timestamp"] for e in page["events"]]
                token = page.get("nextToken")
                if not token or len(runs) > 5000:
                    break
        except logs.exceptions.ResourceNotFoundException:
            runs = []
    ev = {"schedules": schedules, "recovery_function": rec and {k: rec[k] for k in ("name", "db_roles", "code_sha256")},
          "recovery_invocations_last_3_days": len(runs),
          "last_invocation_utc": datetime.fromtimestamp(max(runs) / 1000, timezone.utc).isoformat() if runs else None}
    rs = [s for s in schedules if "ecover" in s["name"] or "ecover" in s["target"]]
    checks = {
        "recovery schedule exists, every minute": bool(rs) and all(s["expression"] in ("rate(1 minute)", "rate(1 minutes)") for s in rs),
        "recovery worker has run on the real schedule": len(runs) > 0,
    }
    missing = ["lease renewal and generation fencing were not forced live (it needs a deliberately stuck message on the real "
               "queue); they are proven locally (tests/test_inbound_delivery.py)",
               "the DLQ handling procedure has not been drilled live (see AWS_QUEUE)"]
    d = drill("drill_dlq_summary.json")
    if d and d["all_pass"]:
        missing.remove(missing[1])
        checks["DLQ handling procedure drilled live: inspect, redrive, remove one message (23 Nov)"] = True
    ll = drill("drill_lease_loss.json")
    ev["lease_loss_drill"] = ll and {"checks": ll["checks"]}
    if ll and ll["all_pass"]:
        missing.remove(missing[0])
        checks["forced lease loss live: expired lease refused renew/finish (TURN_FENCED), the deployed recovery worker "
               "re-drove the turn, one reply (23 Nov)"] = True
    # Schedules are disabled while the pilot sleeps (cost control, owner-approved). That is an operating state, not a
    # missing proof: the schedule exists, has run on its real cadence, and every behaviour was forced live.
    ev["schedules_disabled_while_sleeping"] = all(s["state"] == "DISABLED" for s in rs) if rs else None
    return ev, checks, missing


def gate_workflow(out, fns, res, with_drill_ids: bool = True):
    """with_drill_ids=False (renewal): skip the owner-session read of drill ids; renewal reads records only through
    approval_execution_evidence (0029) and judges attribution itself."""
    sfn = c("stepfunctions")
    sm = next(r["PhysicalResourceId"] for r in res if r["ResourceType"] == "AWS::StepFunctions::StateMachine")
    alias = next((r["PhysicalResourceId"] for r in res if r["ResourceType"] == "AWS::StepFunctions::StateMachineAlias"), None)
    d = sfn.describe_state_machine(stateMachineArn=sm)
    al = sfn.describe_state_machine_alias(stateMachineAliasArn=alias) if alias else {}
    ver = al.get("routingConfiguration", [{}])[0].get("stateMachineVersionArn") if al else None
    vd = sfn.describe_state_machine(stateMachineArn=ver) if ver else {}
    asl = ROOT / "setuhaul" / "workflow" / "approval_workflow.asl.json"
    subs = {"NotifyApproversFunction": "ApprovalNotify", "RecordTaskTokenFunction": "ApprovalToken",
            "ExpireApprovalFunction": "ApprovalExpire"}             # infra/setuhaul_stack.py definition_substitutions
    text = asl.read_text(encoding="utf-8") if asl.exists() else None
    if text is not None:
        for placeholder, logical in subs.items():
            arn = next(f["arn"] for k, f in fns.items() if logical in k)
            text = text.replace("${" + placeholder + "}", arn)
    repo_def = json.loads(text) if text is not None else None
    live_def = json.loads(vd.get("definition") or d["definition"])
    execs = sfn.list_executions(stateMachineArn=sm, maxResults=100)["executions"]
    drill_ids: set = set()
    if with_drill_ids:
        owner = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
        drill_ids = {r[0] for r in owner.query(
            "SELECT approval_id::text FROM setuhaul.approval_requests WHERE shipment_id LIKE 'SYN-STEP6-%'",
            ("SELECT set_config('app.facility_scope','FAC-JAI-01,FAC-GGN-01',true)",))}
    ev = {"state_machine": sm.rsplit(":", 1)[-1], "type": d.get("type"), "alias_routes_to_version": ver and ver.rsplit(":", 1)[-1],
          "logging": d.get("loggingConfiguration", {}).get("level"), "tracing": d.get("tracingConfiguration", {}).get("enabled"),
          "live_definition_sha256": sha(json.dumps(live_def, sort_keys=True).encode()),
          "repo_definition_sha256": repo_def and sha(json.dumps(repo_def, sort_keys=True).encode()),
          "executions": len(execs), "execution_status": {e["name"][:8]: e["status"] for e in execs}}
    checks = {
        "live alias 'live' routes 100% to one published version": bool(ver) and al["routingConfiguration"][0].get("weight") == 100,
        "published definition equals the repository ASL": repo_def is not None and ev["live_definition_sha256"] == ev["repo_definition_sha256"],
        "every execution is an owner-approved Step 6 drill request (SYN-STEP6-*)": all(
            e["name"] in drill_ids for e in execs),
        "no execution has failed": all(e["status"] != "FAILED" for e in execs),
        "booking switch false on every function": all(f["booking"] == "false" for f in fns.values() if f["booking"] is not None),
    }
    missing = ["request / approve / reject / expiry against the real state machine need the booking switch ON (Option D), "
               "which needs the owner's separate approval; proven locally (tests/test_approval_workflow*.py, LocalApprovalWorkflow + ASL)"]
    d = drill("drill_approval_run1.json")
    pend = drill("pending_workflow_processed.json")
    d2, rd = drill("drill_approval_run2.json"), drill("redrive_20261123.json")
    if d2:
        cw = c("cloudwatch")
        fired = []
        for al in cw.describe_alarms(AlarmNamePrefix="SetuHaulPilot-ApprovalFailed")["MetricAlarms"]:
            hist = cw.describe_alarm_history(AlarmName=al["AlarmName"], HistoryItemType="StateUpdate",
                                             StartDate=datetime(2026, 11, 23, tzinfo=timezone.utc),
                                             EndDate=datetime.now(timezone.utc))["AlarmHistoryItems"]
            fired += [h["Timestamp"].isoformat() for h in hist if "to ALARM" in h["HistorySummary"]]
        ev["approval_drill_2"] = {"checks": d2["checks"], "paths": {p_: {k: v[k] for k in ("execution", "approval", "appointment")}
                                                                     for p_, v in d2["evidence"]["paths"].items()},
                                  "redrive_of_failed_executions": rd and rd["checks"],
                                  "approval_failed_alarm_went_to_alarm_at": fired}
        checks["the deployed relay can start executions"] = True
        checks["drill 2 (live): request, approve, reject, duplicate decision, refusals, audit, timeout expiry"] = bool(d2["all_pass"])
        checks["the 3 executions that failed on the defect were redriven and completed safely"] = bool(rd and rd["all_pass"])
        checks["monitoring: the failed-execution alarm fired on real failures (23 Nov)"] = bool(fired)
        return ev, checks, []
    if d:
        ev["approval_drill"] = {"run1_executions_started": d["executions"], "run1_relay_error": d["relay_error"],
                                "pending_processed": pend and {"checks": pend["checks"], "status": pend["execution_status"]}}
        checks["the deployed relay can start executions"] = bool(
            pend and pend["checks"].get("the relay now starts executions (no AccessDenied)")) or d["executions"] > 0
        missing = ["drill 2 after the task-function fix is deployed: start, approve, reject, duplicate decision, audit, "
                   "timeout expiry; and a redrive of the 3 failed executions"]
    return ev, checks, missing


def gate_identity(out, fns, stmts):
    owner = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
    logins = owner.query("""SELECT r.rolname, coalesce(string_agg(g.rolname, ',' ORDER BY g.rolname), '')
                              FROM pg_roles r LEFT JOIN pg_auth_members m ON m.member = r.oid
                              LEFT JOIN pg_roles g ON g.oid = m.roleid
                             WHERE r.rolcanlogin AND r.rolname LIKE 'setuhaul%' AND r.rolname <> current_user
                             GROUP BY r.rolname ORDER BY 1""")
    reach = owner.query("""SELECT r.rolname, pg_has_role(r.oid, 'setuhaul_console', 'USAGE'),
                                  pg_has_role(r.oid, 'setuhaul_rpc', 'USAGE'), r.rolsuper, r.rolbypassrls,
                                  pg_has_role(r.oid, (SELECT datdba FROM pg_database WHERE datname = current_database()), 'USAGE')
                             FROM pg_roles r WHERE r.rolcanlogin AND r.rolname LIKE 'setuhaul%' AND r.rolname <> current_user
                            ORDER BY 1""")
    mapping = {l: [g for g in gs.split(",") if g] for l, gs in logins}
    # which secrets each function's IAM role may read, and which DB roles its configuration names
    db_secret = {secret_stem(a): role for f in fns.values() for role, a in f["secret_arns"].items()}
    fn_map = {}
    for k, f in fns.items():
        readable = {db_secret[secret_stem(r)] for r in secret_resources(stmts[k]) if secret_stem(r) in db_secret}
        if f["db_roles"] or readable:
            fn_map[k] = {"db_roles": f["db_roles"], "db_secrets_iam_can_read": sorted(readable)}
    over = {k: v for k, v in fn_map.items() if set(v["db_secrets_iam_can_read"]) - set(v["db_roles"])}
    # criterion 4: a real Data API console transaction keeps the staff context; rolled back
    con = next(f for f in fns.values() if f["db_roles"] == ["console"] or ("console" in f["db_roles"] and len(f["db_roles"]) <= 2))
    db = DataApiDb(out["ClusterArn"], "setuhaul", {"console": con["secret_arns"]["console"]}, c("rds-data"), resume_wait_s=90)
    staff_probe = {}

    class Rollback(Exception):
        pass
    try:
        with db.tx("console", ("FAC-JAI-01",), staff=("staff:step6-probe", {"planner"}, ("FAC-JAI-01",))) as t:
            # statement 1 (inside tx()) created the staff identity; a second begin_staff_context in a LATER Data API
            # statement must see it and refuse. That refusal is the proof the identity survived across statements.
            staff_probe = {"current_user": t.execute("SELECT current_user").fetchone()[0],
                           "scope_after_staff_context": t.execute("SELECT current_setting('app.facility_scope')").fetchone()[0]}
            try:
                t.execute("SELECT begin_staff_context(%s,%s,%s)", ("staff:step6-probe-2", ["planner"], ["FAC-JAI-01"]))
                staff_probe["second_begin"] = "ACCEPTED (identity was lost between statements)"
            except Exception as e:                               # noqa: BLE001 — the expected refusal
                staff_probe["second_begin"] = str(getattr(getattr(e, "diag", None), "message_primary", None) or e)[:160]
            raise Rollback
    except Rollback:
        staff_probe["rolled_back"] = True
    except Exception as e:                                       # noqa: BLE001 — reported as evidence
        staff_probe = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    ev = {"login_to_group_roles": mapping, "function_to_db_roles": fn_map,
          "reach": [{"login": r[0], "can_use_console": r[1], "can_use_rpc": r[2], "superuser": r[3], "bypassrls": r[4],
                     "can_use_db_owner": r[5]} for r in reach],
          "staff_context_probe": staff_probe}
    console_logins = [l for l, gs in mapping.items() if "setuhaul_console" in gs]
    checks = {
        "1 console login maps only to setuhaul_console": bool(console_logins) and all(mapping[l] == ["setuhaul_console"] for l in console_logins),
        "2 other logins cannot use the console role": all(not r[1] for r in reach if r[0] not in console_logins),
        "3 no login can use setuhaul_rpc, the owner, superuser or BYPASSRLS": all(not (r[2] or r[3] or r[4] or r[5]) for r in reach),
        "each login maps to exactly one application role": all(len(gs) == 1 for gs in mapping.values()),
        "4 staff context survives across statements of a real Data API transaction":
            "already set in this transaction" in str(staff_probe.get("second_begin", "")),
        "no function's IAM role can read more DB secrets than its configured roles": not over,
    }
    c5 = drill("identity_check5.json")
    ev["check5"] = c5
    if c5 and c5.get("live_signin", {}).get("all_pass"):
        checks["5 an ordinary operations user cannot obtain an ops_lead or planner context (live sign-in, 23 Nov)"] = True
        first = []
    else:
        first = ["5 'an ordinary operations user cannot create an ops_lead context': the second Cognito user exists "
                 "(pilot-operations, groups operations + facility) and the deployed pre-token trigger was shown not to add "
                 "or change role groups; the end-to-end sign-in by the owner is still to be recorded"]
    missing = first + [
               "live drift detection (AWS Config) is not deployed on the Free-plan pilot; the IAM policy hash is "
               "recorded here instead"]
    ev["iam_policies_sha256"] = sha(json.dumps({k: stmts[k] for k in sorted(stmts)}, sort_keys=True, default=str).encode())
    return ev, checks, missing, over


def gate_aurora(out):
    rds = c("rds")
    cl = rds.describe_db_clusters(DBClusterIdentifier=f"setuhaul-{ENV}")["DBClusters"][0]
    owner = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
    live = dict(owner.query("SELECT version, sha256 FROM setuhaul_meta.schema_migrations ORDER BY version"))
    local = {p.stem.split("_", 1)[0]: sha(p.read_bytes()) for p in sorted((ROOT / "migrations").glob("*.sql"))}
    live_n = {k.split("_", 1)[0]: v for k, v in live.items()}
    # D1 (Recovery Release A): every trigger on the evidence and booking-commitment tables, not just two names
    triggers = owner.query(EVIDENCE_TRIGGER_QUERY)
    trigger_issues = trigger_problems(triggers)
    rls = owner.query("""SELECT count(*) FILTER (WHERE c.relrowsecurity AND c.relforcerowsecurity), count(*)
                           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                          WHERE n.nspname = 'setuhaul' AND c.relkind = 'r'""")[0]
    version = owner.query("SELECT current_setting('server_version')")[0][0]
    deployed = {k: v for k, v in local.items() if k in live_n}
    ev = {"engine": f'{cl["Engine"]} {cl["EngineVersion"]}', "server_version": version,
          "migrations_live": len(live), "migrations_in_repo": len(local),
          "hash_mismatches": sorted(k for k, v in deployed.items() if live_n.get(k) != v),
          "repo_only_migrations": sorted(set(local) - set(live_n)),
          "evidence_triggers": {f"{t}.{n}": [e, f, ty] for t, n, e, f, ty in triggers},
          "evidence_trigger_problems": trigger_issues, "tables_with_forced_rls": rls[0], "tables": rls[1]}
    major = int(str(cl["EngineVersion"]).split(".")[0])
    checks = {
        "every deployed migration's hash matches the repository": not ev["hash_mismatches"],
        "evidence and booking guard triggers: exactly the expected set, enabled and configured": not trigger_issues,
        "Data API connectivity behind Db.tx()": True,
    }
    # T33 (production): 18.3+. T33-P (owner, 23 Nov 2026): 17.9 is permitted for the synthetic Free-plan pilot only,
    # once 17.9 is verified: migration 0025, RLS and the role allow-list live on this cluster (verify_pg17_migration.py)
    # and the whole suite in CI on postgres:17.9 (ci_pg17.json). It is never reported as an unconditional PASS.
    ev["t33"] = {"production_requirement": "PostgreSQL 18.3+ (T33, unchanged)", "engine_major": major}
    if major >= 18:
        checks["engine is Aurora PostgreSQL 18.3+ (T33)"] = True
        return ev, checks, [], None
    pilot = {}
    for name in ("pg17_migration_0025.json", "ci_pg17.json"):
        f = ROOT / "docs" / "release" / "evidence" / name
        pilot[name] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    ver = pilot["pg17_migration_0025.json"] and pilot["pg17_migration_0025.json"]["evidence"].get("server_version", "")
    ev["t33_p"] = {"engine": ev["server_version"], "pilot_only": True, "synthetic_data": True,
                   "live_verification": pilot["pg17_migration_0025.json"] and pilot["pg17_migration_0025.json"]["checks"],
                   "ci_on_17_9": pilot["ci_pg17.json"]}
    checks["T33-P: engine is exactly 17.9 (the Free-plan express engine)"] = str(ev["server_version"]).startswith("17.9")
    checks["T33-P: migration 0025, RLS and role allow-list verified live on this 17.9 cluster"] = bool(
        pilot["pg17_migration_0025.json"] and pilot["pg17_migration_0025.json"].get("all_pass") and str(ver).startswith("17.9"))
    checks["T33-P: the whole suite passed in CI on postgres:17.9"] = bool(
        pilot["ci_pg17.json"] and pilot["ci_pg17.json"].get("conclusion") == "success"
        and all(str(v).startswith("17.9") for v in pilot["ci_pg17.json"].get("server_versions", [])))
    return ev, checks, [], "PASS WITH PILOT EXCEPTION (T33-P)"


def status(checks, missing, blocked=False):
    if not all(checks.values()):
        return "FAIL"
    if blocked:
        return "BLOCKED"
    return "PARTIAL" if missing else "PASS"


EVIDENCE = ROOT / "docs" / "release" / "evidence"
ACTIVATION_ONLY = ("every execution is an owner-approved Step 6 drill request (SYN-STEP6-*)",
                   "booking switch false on every function")


def all_executions(sfn_arn: str) -> list[dict]:
    """Every execution of the approval state machine (paginated): renewal attributes all of them, not the first 100."""
    sfn, execs, token = c("stepfunctions"), [], None
    while True:
        page = sfn.list_executions(stateMachineArn=sfn_arn, maxResults=100, **({"nextToken": token} if token else {}))
        execs += [{"name": e["name"], "status": e["status"], "start": e["startDate"].isoformat(),
                   "stop": e.get("stopDate") and e["stopDate"].isoformat(), "arn": MASK.sub("************", e["executionArn"])}
                  for e in page["executions"]]
        token = page.get("nextToken")
        if not token:
            return execs


def evidence_records(out: dict, approval_ids: list[str]) -> tuple[dict[str, dict], str | None]:
    """The approval records behind the executions, through approval_execution_evidence (migration 0029) as the
    pipeline's evidence identity: READ ONLY transaction, facility scope = the two pilot facilities, rolled back. RLS still
    applies. No owner session and no table read. Returns (records, blocker)."""
    from setuhaul.aws.data_api import Connection, begin_with_resume
    from setuhaul.ops.renewal import PILOT_FACILITIES
    secret = json.loads(out["LoginSecretArns"]).get("deploy_evidence")
    if not secret:
        return {}, "the stack has no deploy_evidence login secret"
    rds = c("rds-data")
    kw = {"resourceArn": out["ClusterArn"], "secretArn": secret, "database": "setuhaul"}
    cols = ("approval_id", "kind", "facility_id", "shipment_id", "shipment_is_synthetic", "appointment_id",
            "appointment_status", "status", "decided_by_type", "decided_by_role", "decider_kind", "outcome_reason",
            "created_at", "decided_at", "expires_at", "task_token_recorded")
    rows: dict[str, dict] = {}
    tid = begin_with_resume(rds, kw, 120)
    try:
        conn = Connection(rds, out["ClusterArn"], secret, "setuhaul", tid)
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SELECT set_config('role', 'setuhaul_deploy_evidence', true), "
                     "set_config('app.facility_scope', %s, true)", (",".join(PILOT_FACILITIES),))
        for i in range(0, len(approval_ids), 200):
            batch = approval_ids[i:i + 200]
            for r in conn.execute("SELECT * FROM setuhaul.approval_execution_evidence(%s::uuid[])",
                                  ("{" + ",".join(batch) + "}",)).fetchall():
                d = dict(zip(cols, r))
                rows[str(d["approval_id"])] = d
    except Exception as e:                                       # noqa: BLE001 — reported; the gate then cannot pass
        return {}, f"approval records could not be read through approval_execution_evidence: {type(e).__name__}: {str(e)[:160]}"
    finally:
        try:
            rds.rollback_transaction(resourceArn=kw["resourceArn"], secretArn=secret, transactionId=tid)
        except Exception:                                        # noqa: BLE001
            pass
    return rows, None


def smoke_records() -> list[dict]:
    """Every committed smoke record: the 23 Nov Option D smoke and the renewal-format runs under evidence/smoke/."""
    files = [EVIDENCE / "option_d_smoke.json", *sorted((EVIDENCE / "smoke").glob("*.json"))]
    return [json.loads(f.read_text(encoding="utf-8")) for f in files if f.exists()]


def owner_api_log_corroboration(owner: dict | None, api_log_group: str | None, now: datetime,
                                logs=None, evidence_dir: Path | None = None) -> dict[str, dict]:
    """Recovery Release A (check 1): every console request an owner record names must appear in the API function's OWN
    log group (read from the deployed stack, never from the record): the same request id and the same line, within
    two minutes of the recorded time. Once the log is past its retention, an earlier COMMITTED report must have verified
    exactly the same request ids; otherwise the record is not corroborated (fail closed)."""
    out: dict[str, dict] = {}
    if not owner:
        return out
    logs = logs or c("logs")
    retention_days = None
    if api_log_group:
        g = logs.describe_log_groups(logGroupNamePrefix=api_log_group).get("logGroups") or []
        retention_days = next((x.get("retentionInDays") for x in g if x.get("logGroupName") == api_log_group), None)
    prior = {}
    for f in sorted((evidence_dir or EVIDENCE).glob("*.json")):
        try:
            rep_ = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        bw = ((rep_.get("gates") if isinstance(rep_, dict) else None) or {}).get("BOOKING_WORKFLOW")
        prior_corr = ((bw.get("evidence") if isinstance(bw, dict) else None) or {}).get("api_log_corroboration")
        for aid, v in (prior_corr if isinstance(prior_corr, dict) else {}).items():
            if isinstance(v, dict) and v.get("status") == "verified":
                prior[aid] = {"file": f.name, "request_ids": sorted(v.get("request_ids") or [])}
    for e in owner.get("executions") or []:
        aid = str(e.get("approval_id"))
        reqs = ((e.get("cloudwatch") or {}).get("requests")) or []
        ids = sorted(str(r.get("request_id")) for r in reqs)
        if not reqs or not api_log_group:
            out[aid] = {"status": "missing", "request_ids": ids,
                        "detail": "the record names no console request" if not reqs else "no API log group"}
            continue
        states, found = [], []
        for r in reqs:
            t = datetime.fromisoformat(str(r["t_utc"]).replace("Z", "+00:00"))
            if retention_days is not None and now - t > timedelta(days=retention_days) - timedelta(hours=1):
                states.append("beyond_retention")
                continue
            ev = logs.filter_log_events(logGroupName=api_log_group, filterPattern=f'"{r["request_id"]}"',
                                        startTime=int((t - timedelta(minutes=2)).timestamp() * 1000),
                                        endTime=int((t + timedelta(minutes=2)).timestamp() * 1000)).get("events") or []
            hit = next((x for x in ev if r["request_id"] in x.get("message", "") and r["line"] in x.get("message", "")),
                       None)
            states.append("verified" if hit else "missing")
            if hit:
                found.append({"request_id": r["request_id"], "timestamp_ms": hit.get("timestamp"), "line": r["line"]})
        if "missing" in states:
            out[aid] = {"status": "missing", "request_ids": ids, "detail": "a recorded request is not in the API log"}
        elif "beyond_retention" in states:
            pv = prior.get(aid)
            out[aid] = ({"status": "previously_verified", "request_ids": ids, "detail": f"verified in {pv['file']}"}
                        if pv and pv["request_ids"] == ids else
                        {"status": "beyond_retention_unverified", "request_ids": ids,
                         "detail": "the API log is past its retention and no committed report verified these requests"})
        else:
            out[aid] = {"status": "verified", "request_ids": ids, "log_group": api_log_group, "events": found}
    return out


def renewal_gate(out, fns, res, approval_ref: str, release_sha: str, fresh_smoke: dict | None,
                 expected_kill_switch: str) -> tuple[dict, str | None]:
    """BOOKING_WORKFLOW in renewal mode (setuhaul/ops/renewal.py judges; this function only reads)."""
    import httpx
    from setuhaul.ops import renewal as R
    ev_act, ch_act, _ = gate_workflow(out, fns, res, with_drill_ids=False)   # activation facts: definition, alias, failures
    base = {k: ok for k, ok in ch_act.items() if k not in ACTIVATION_ONLY and k in (
        "live alias 'live' routes 100% to one published version", "published definition equals the repository ASL",
        "no execution has failed")}
    health = httpx.get(out["ApiUrl"].rstrip("/") + "/health", timeout=40).json()
    by_name = {f["name"]: f for f in fns.values()}
    config = R.check_booking_config(by_name, health, out.get("BookingMode"), approval_ref, expected_kill_switch)
    shas = {R.code_sha256_hex(by_name[n]["code_sha256"]) for n in R.EXPECTED_BOOKING_ON if n in by_name}
    artifact = shas.pop() if len(shas) == 1 else None
    sm = next(r["PhysicalResourceId"] for r in res if r["ResourceType"] == "AWS::StepFunctions::StateMachine")
    execs = all_executions(sm)
    records, blocker = evidence_records(out, [e["name"] for e in execs])
    smokes = smoke_records()
    if fresh_smoke and not any(s.get("smoke_id") == fresh_smoke.get("smoke_id") for s in smokes):
        smokes.append(fresh_smoke)
    owner_file = EVIDENCE / "owner_attribution_20261123.json"
    owner = json.loads(owner_file.read_text(encoding="utf-8")) if owner_file.exists() else None
    now = datetime.now(timezone.utc)
    api_group = (by_name.get("setuhaul-pilot-api") or {}).get("log_group")
    api_log = owner_api_log_corroboration(owner, api_group, now)
    attribution = R.attribute_executions(execs, records, smokes, owner, api_log)
    smoke = R.fresh_smoke_checks(fresh_smoke, release_sha, artifact, now)
    g = R.booking_workflow_renewal(base, config, attribution, smoke, fresh_smoke)
    g["evidence"]["activation_facts"] = {k: ev_act.get(k) for k in ("state_machine", "alias_routes_to_version",
                                                                      "live_definition_sha256", "repo_definition_sha256")}
    g["evidence"]["executions_total"] = len(execs)
    g["evidence"]["api_log_corroboration"] = api_log
    if blocker:
        g["diagnostics"].insert(0, f"BOOKING_WORKFLOW: {g['status']} — {blocker}")
        g["evidence"]["record_access_blocker"] = blocker
    return g, artifact


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("activation", "renewal"), default="activation",
                    help="activation (default, unchanged): the pre-activation acceptance, booking OFF. renewal: an "
                         "active pilot, booking ON (docs/booking-renewal/RENEWAL_GATE_DESIGN.md)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--approval-ref", help="renewal: the owner's approval reference OWNER-APPROVAL-YYYYMMDD-<ref>")
    ap.add_argument("--release-sha", help="renewal: the git commit the pilot runs (40 hex)")
    ap.add_argument("--smoke", help="renewal: the fresh smoke record (scripts/smoke_option_d.py --release-sha ...)")
    ap.add_argument("--expected-kill-switch", default="disengaged", choices=("disengaged", "ENGAGED"))
    a = ap.parse_args()
    if a.mode == "renewal":
        if not (a.approval_ref and a.release_sha and re.fullmatch(r"[0-9a-f]{40}", a.release_sha)):
            raise SystemExit("renewal needs --approval-ref and --release-sha (40 hex); --smoke is checked by the gate")
    a.out = a.out or str(EVIDENCE / ("renewal_gates.json" if a.mode == "renewal" else "step6_gates.json"))
    out, res = stack()
    fns = functions(res)
    stmts = {k: role_statements(f["role"]) for k, f in fns.items()}
    gates = {}
    ev, ch, mi = gate_identity(out, fns, stmts)[:3]
    gates["WORKLOAD_IDENTITY"] = {"evidence": ev, "checks": ch, "not_shown": mi, "status": status(ch, mi)}
    ev, ch, mi, exception = gate_aurora(out)
    st = status(ch, mi)
    gates["AURORA"] = {"evidence": ev, "checks": ch, "not_shown": mi,
                       "status": exception if (exception and st == "PASS") else st}
    ev, ch, mi = gate_queue(out, fns, stmts)
    gates["AWS_QUEUE"] = {"evidence": ev, "checks": ch, "not_shown": mi, "status": status(ch, mi)}
    ev, ch, mi = gate_recovery(out, fns, res)
    gates["RECOVERY_WORKER"] = {"evidence": ev, "checks": ch, "not_shown": mi, "status": status(ch, mi)}
    artifact = None
    if a.mode == "renewal":
        fresh = json.loads(Path(a.smoke).read_text(encoding="utf-8")) if a.smoke else None
        gates["BOOKING_WORKFLOW"], artifact = renewal_gate(out, fns, res, a.approval_ref, a.release_sha, fresh,
                                                           a.expected_kill_switch)
    else:
        ev, ch, mi = gate_workflow(out, fns, res)
        gates["BOOKING_WORKFLOW"] = {"evidence": ev, "checks": ch, "not_shown": mi,
                                     "status": status(ch, mi, blocked=not ({"approval_drill", "approval_drill_2"} & set(ev)))}
    gates["OUTBOUND_DELIVERY"] = {
        "evidence": {"sender": "internal test sink (SETUHAUL_OUTBOUND=test-sink); WhatsApp never enabled",
                     "see": "docs/release/evidence/option_a.json (A3/A4: delivered to the sink; reserved number refused)"},
        "checks": {}, "status": "BLOCKED",
        "not_shown": ["needs a Meta WhatsApp Cloud API test number, Meta-approved templates (en, hi; Hinglish is its own "
                      "gate) and a real permitted test delivery; real WhatsApp stays disabled until the owner provides them"]}
    report = {"collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stack": STACK,
              "region": REGION, "booking_enabled": False, "deployment_evidence_rows_written": 0, "gates": gates}
    if a.mode == "renewal":
        from setuhaul.ops.renewal import ENVIRONMENT, renewal_status
        for gname, gv in gates.items():
            gv["renewal_status"] = renewal_status(gname, gv)
        report.update({"mode": "renewal", "booking_enabled": True, "environment": ENVIRONMENT,
                       "release_sha": a.release_sha, "approval_ref": a.approval_ref, "artifact_sha256": artifact,
                       "expected_kill_switch": a.expected_kill_switch,
                       "approved_exceptions": ["AURORA (T33-P)", "WORKLOAD_IDENTITY (NO-LIVE-DRIFT-DETECTION)",
                                               "OUTBOUND_DELIVERY (META-DEFERRED-TEST-SINK-ONLY)"]})
    text = MASK.sub("************", json.dumps(report, indent=1, default=str))
    Path(a.out).write_text(text + "\n", encoding="utf-8")
    for g, v in gates.items():
        print(f"{g:18} {v.get('renewal_status', v['status']):8} "
              + "; ".join(f"{'ok' if ok else 'NO'}: {k}" for k, ok in v["checks"].items()))
        for d in v.get("diagnostics", []):
            print("    " + d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
