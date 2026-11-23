"""Read-only check of the deployed low-cost pilot (docs/release/09_LOW_COST_PILOT_PLAN.md, 10_EXPRESS_AURORA.md).

    python scripts/verify_aws_deploy.py [--out docs/release/evidence/deploy_check.json]

Uses the operator's AWS CLI session. Changes nothing. Account ids are masked in the output.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.aws.migrate_data_api import OwnerSession  # noqa: E402

REGION, STACK, ENV = "ap-southeast-2", "SetuHaulPilot", "pilot"
MASK = re.compile(r"\b\d{12}\b")


def c(name):
    return boto3.client(name, region_name=REGION)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "release" / "evidence" / "deploy_check.json"))
    a = ap.parse_args()
    out = {o["OutputKey"]: o["OutputValue"] for o in c("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    ev, checks = {}, {}

    rds = c("rds")
    cl = rds.describe_db_clusters(DBClusterIdentifier=f"setuhaul-{ENV}")["DBClusters"][0]
    sv2 = cl.get("ServerlessV2ScalingConfiguration", {})
    inst = [rds.describe_db_instances(DBInstanceIdentifier=m["DBInstanceIdentifier"])["DBInstances"][0]
            for m in cl["DBClusterMembers"]]
    ev["aurora"] = {"scaling": sv2, "backup_retention_days": cl["BackupRetentionPeriod"],
                    "data_api": cl.get("HttpEndpointEnabled"), "storage_encrypted_flag": cl["StorageEncrypted"],
                    "kms_key": cl.get("KmsKeyId"), "engine": f'{cl["Engine"]} {cl["EngineVersion"]}',
                    "status": cl["Status"], "vpc_networking": cl.get("VPCNetworkingEnabled"),
                    "internet_access_gateway": cl.get("InternetAccessGatewayEnabled"),
                    "deletion_protection": cl.get("DeletionProtection"), "master_user": cl.get("MasterUsername"),
                    "iam_auth": cl.get("IAMDatabaseAuthenticationEnabled"),
                    "parameter_group": cl.get("DBClusterParameterGroup"),
                    "log_exports": cl.get("EnabledCloudwatchLogsExports"),
                    "security_groups": [g["VpcSecurityGroupId"] for g in cl.get("VpcSecurityGroups", [])],
                    "instances": [{"class": i["DBInstanceClass"], "public": i["PubliclyAccessible"],
                                   "performance_insights": i.get("PerformanceInsightsEnabled"),
                                   "monitoring_interval": i.get("MonitoringInterval")} for i in inst]}
    checks["Aurora 0-2 ACU"] = sv2.get("MinCapacity") == 0 and sv2.get("MaxCapacity") == 2
    checks["auto-pause 300 s"] = sv2.get("SecondsUntilAutoPause") == 300
    checks["backup 1 day"] = cl["BackupRetentionPeriod"] == 1
    checks["database not public"] = all(not i["PubliclyAccessible"] for i in inst)
    checks["Data API on"] = bool(cl.get("HttpEndpointEnabled"))
    checks["VPC networking on, internet access gateway off"] = (cl.get("VPCNetworkingEnabled") is True
                                                               and cl.get("InternetAccessGatewayEnabled") is False)
    checks["deletion protection on"] = cl.get("DeletionProtection") is True
    checks["pgaudit parameter group in sync"] = (cl.get("DBClusterParameterGroup") == f"setuhaul-{ENV}" and all(
        m.get("DBClusterParameterGroupStatus") == "in-sync" for m in cl["DBClusterMembers"]))
    ec2 = c("ec2")
    rules = [p for g in ec2.describe_security_groups(GroupIds=ev["aurora"]["security_groups"])["SecurityGroups"]
             for p in g["IpPermissions"]]
    ev["aurora"]["inbound_rules"] = [{"ports": (p.get("FromPort"), p.get("ToPort")),
                                      "cidrs": [r["CidrIp"] for r in p.get("IpRanges", [])],
                                      "from_groups": [r["GroupId"] for r in p.get("UserIdGroupPairs", [])]} for p in rules]
    checks["database inbound: 5432 from one security group, no CIDR"] = (
        len(rules) == 1 and rules[0].get("FromPort") == 5432 and rules[0].get("ToPort") == 5432
        and not rules[0].get("IpRanges") and not rules[0].get("Ipv6Ranges") and len(rules[0].get("UserIdGroupPairs", [])) == 1)

    acls = c("wafv2").list_web_acls(Scope="REGIONAL")["WebACLs"]
    api_id = out["ApiUrl"].split("//", 1)[1].split(".", 1)[0]
    stage = c("apigateway").get_stage(restApiId=api_id, stageName=ENV)
    thr = stage.get("methodSettings", {}).get("*/*", {})
    ev["api"] = {"web_acls": len(acls), "stage_web_acl": stage.get("webAclArn"),
                 "throttle": {k: thr.get(k) for k in ("throttlingRateLimit", "throttlingBurstLimit")}}
    checks["no WAF"] = not acls and not stage.get("webAclArn")
    checks["throttling 20/40"] = thr.get("throttlingRateLimit") == 20 and thr.get("throttlingBurstLimit") == 40

    pool = c("cognito-idp").describe_user_pool(UserPoolId=out["UserPoolId"])["UserPool"]
    ev["cognito"] = {"mfa": pool.get("MfaConfiguration"), "self_signup": not pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"]}
    checks["Cognito MFA on, no self sign-up"] = pool.get("MfaConfiguration") == "ON" and not ev["cognito"]["self_signup"]

    lam = c("lambda")
    fns = [f for p in lam.get_paginator("list_functions").paginate() for f in p["Functions"]
           if f["FunctionName"].startswith(f"setuhaul-{ENV}-")]
    logs = c("logs")
    fev = {}
    for f in fns:
        cfg = lam.get_function_configuration(FunctionName=f["FunctionName"])
        env = cfg.get("Environment", {}).get("Variables", {})
        lg = cfg["LoggingConfig"]["LogGroup"]
        g = logs.describe_log_groups(logGroupNamePrefix=lg)["logGroups"]
        fev[f["FunctionName"]] = {"booking": env.get("SETUHAUL_BOOKING_ENABLED"),
                                  "whatsapp": env.get("SETUHAUL_WHATSAPP_ENABLED"),
                                  "notification_mode": env.get("SETUHAUL_NOTIFICATION_MODE"),
                                  "environment": env.get("SETUHAUL_ENV"),
                                  "resume_wait_s": env.get("SETUHAUL_DB_RESUME_WAIT_S"),
                                  "vpc": bool(cfg.get("VpcConfig", {}).get("VpcId")),
                                  "log_retention_days": g[0].get("retentionInDays") if g else None}
    ev["lambdas"] = fev
    checks["11 Lambdas (8 workload + 2 database setup + Cognito pre-token)"] = len(fns) == 11
    checks["booking false on every Lambda"] = all(v["booking"] == "false" for v in fev.values())
    checks["WhatsApp never enabled"] = all(v["whatsapp"] in (None, "false") for v in fev.values())
    outbound = fev.get(f"setuhaul-{ENV}-outbound", {})
    checks["outbound uses the test sink"] = outbound.get("notification_mode") == "test_sink" and outbound.get("whatsapp") == "false"
    checks["Lambda logs 7 days"] = all(v["log_retention_days"] == 7 for v in fev.values())
    checks["only db-bootstrap in the VPC"] = [n for n, v in fev.items() if v["vpc"]] == [f"setuhaul-{ENV}-db-bootstrap"]
    dbl = logs.describe_log_groups(logGroupNamePrefix=f"/aws/rds/cluster/setuhaul-{ENV}/")["logGroups"]
    ev["db_log_retention"] = [g.get("retentionInDays") for g in dbl]
    checks["database logs 7 days"] = all(r == 7 for r in ev["db_log_retention"])

    events = c("events")
    rules = {n: events.describe_rule(Name=n)["State"] for n in out["ScheduleRuleNames"].split(",")}
    ev["schedules"] = rules
    checks["3 schedules, all disabled"] = len(rules) == 3 and set(rules.values()) == {"DISABLED"}

    nat = [n for n in ec2.describe_nat_gateways()["NatGateways"] if n["State"] not in ("deleted", "deleting")]
    eips = ec2.describe_addresses()["Addresses"]
    vpce = ec2.describe_vpc_endpoints()["VpcEndpoints"]
    pub = [n["NetworkInterfaceId"] for n in ec2.describe_network_interfaces()["NetworkInterfaces"]
           if n.get("Association", {}).get("PublicIp")]
    igws = ec2.describe_internet_gateways()["InternetGateways"]
    group = rds.describe_db_subnet_groups(DBSubnetGroupName=f"setuhaul-{ENV}")["DBSubnetGroups"][0]
    stack_vpcs = {group["VpcId"]}
    igw_on_db_vpc = [g["InternetGatewayId"] for g in igws if any(a.get("VpcId") in stack_vpcs for a in g.get("Attachments", []))]
    ev["network"] = {"db_vpc": sorted(stack_vpcs), "internet_gateways_on_db_vpc": igw_on_db_vpc}
    ev["network"].update({"nat_gateways": len(nat), "elastic_ips": len(eips), "vpc_endpoints": len(vpce),
                          "public_ipv4_interfaces": len(pub)})
    checks["no NAT, no public IPv4, no endpoints, no internet gateway on the database VPC"] = (
        not nat and not eips and not vpce and not pub and not igw_on_db_vpc)

    db = OwnerSession(c("rds-data"), out["ClusterArn"], out["OwnerSecretArn"])
    versions = [r[0] for r in db.query("SELECT version FROM setuhaul_meta.schema_migrations ORDER BY version")]
    files = sorted(p.stem for p in (ROOT / "migrations").glob("*.sql"))
    roles = [r[0] for r in db.query("SELECT rolname FROM pg_roles WHERE rolname LIKE 'setuhaul%' AND rolcanlogin ORDER BY 1")]
    owner = db.query("""SELECT current_user, rolsuper, rolbypassrls, version() FROM pg_roles WHERE rolname = current_user""")[0]
    ev["database_owner"] = {"user": owner[0], "superuser": owner[1], "bypassrls": owner[2], "server": owner[3]}
    checks["Data API owner is the non-master owner, not a superuser"] = owner[0] == "setuhaul_owner" and not owner[1]
    seed = db.query("SELECT (SELECT count(*) FROM setuhaul.carriers), (SELECT count(*) FROM setuhaul.drivers),"
                    " (SELECT count(*) FROM setuhaul.facilities), (SELECT count(*) FROM setuhaul.appointments)",
                    ("SELECT set_config('app.facility_scope', 'FAC-JAI-01,FAC-GGN-01,CENTRAL', true)",))
    ev["database"] = {"migrations": versions, "login_roles": roles, "carriers_drivers_facilities_appointments": list(seed[0])}
    checks["every repository migration recorded, in order"] = [v.split("_")[0] if "_" in v else v for v in versions] == [f.split("_")[0] for f in files] or versions == files
    # With booking off no conversation can create an appointment. The only ones allowed are the owner-approved Step 6
    # approval-workflow drill's synthetic requests (SYN-STEP6-* shipments, 23 Nov 2026; docs/release/13_STEP6_GATE_EVIDENCE.md).
    other = db.query("SELECT count(*) FROM setuhaul.appointments WHERE shipment_id NOT LIKE 'SYN-STEP6-%'",
                     ("SELECT set_config('app.facility_scope', 'FAC-JAI-01,FAC-GGN-01,CENTRAL', true)",))[0][0]
    ev["database"]["appointments_outside_step6_drills"] = other
    checks["no appointments outside the owner-approved Step 6 drills (booking off)"] = other == 0

    # hosted staff console, served by the API under /app/ (docs/release/11_STAFF_CONSOLE.md)
    if out.get("ConsoleClientId"):
        import urllib.request
        cli = c("cognito-idp").describe_user_pool_client(UserPoolId=out["UserPoolId"], ClientId=out["ConsoleClientId"])["UserPoolClient"]
        trig = pool.get("LambdaConfig", {}).get("PreTokenGenerationConfig", {})
        dom = c("cognito-idp").describe_user_pool_domain(Domain=out["CognitoDomain"].split(".")[0])["DomainDescription"]
        page = urllib.request.urlopen(out["ConsoleUrl"], timeout=30)
        hdr = {k.lower(): v for k, v in page.headers.items()}
        ev["console"] = {"url": out["ConsoleUrl"], "oauth_flows": cli.get("AllowedOAuthFlows"),
                         "callbacks": cli.get("CallbackURLs"), "client_secret": "ClientSecret" in cli,
                         "access_token_minutes": cli.get("AccessTokenValidity"), "trigger_version": trig.get("LambdaVersion"),
                         "cognito_domain_status": dom.get("Status"), "page_status": page.status,
                         "csp": hdr.get("content-security-policy")}
        checks["Cognito: code grant, no client secret, callback = console only"] = (
            cli.get("AllowedOAuthFlows") == ["code"] and "ClientSecret" not in cli
            and cli.get("CallbackURLs") == [out["ConsoleUrl"]] and cli.get("LogoutURLs") == [out["ConsoleUrl"]])
        checks["Cognito hosted domain active"] = dom.get("Status") == "ACTIVE"
        checks["facility-scope trigger V2_0"] = trig.get("LambdaVersion") == "V2_0"
        checks["console page served with a strict CSP"] = (page.status == 200 and "script-src 'self'" in hdr.get(
            "content-security-policy", "") and "unsafe-eval" not in hdr.get("content-security-policy", ""))
        checks["no S3 bucket or CloudFront for the console"] = not [
            r for r in c("cloudformation").list_stack_resources(StackName=STACK)["StackResourceSummaries"]
            if r["ResourceType"] in ("AWS::S3::Bucket", "AWS::CloudFront::Distribution")]

    report = {"stack_outputs": {k: v for k, v in out.items() if not k.endswith("Arns")}, "evidence": ev, "checks": checks,
              "all_pass": all(checks.values())}
    text = MASK.sub("<ACCOUNT>", json.dumps(report, indent=2, default=str))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
