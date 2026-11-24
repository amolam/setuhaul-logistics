"""E0: the CDK stack synthesises, and the template has the properties the deployment gates depend on.

No AWS call: the stack is synthesised in-process (aws-cdk-lib, requirements-infra.txt). Skipped if CDK is not installed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
from setuhaul_stack import (AUTO_PAUSE_S, DB_SETUP_FUNCTIONS, LEASE_S, PLATFORM_FUNCTIONS, SCHEDULED, SYDNEY_AZS, WORKLOAD_ROLES,  # noqa: E402
                            SetuHaulPilotStack, schedule_rule_name)

PLAINTEXT_SECRETS = ("SESSION_SECRET", "META_APP_SECRET", "WHATSAPP_TOKEN", "OPENROUTER_API_KEY")


def _zip(path: Path, text: str = "synth only") -> Path:
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("setuhaul/placeholder.txt", (1980, 1, 1, 0, 0, 0)), text)
    return path


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A synthesised Cloud Assembly in a temporary cdk.out, from a zip standing in for build/lambda.zip."""
    d = tmp_path_factory.mktemp("synth")
    zip_path = _zip(d / "lambda.zip")
    app = cdk.App(outdir=str(d / "cdk.out"))
    stack = SetuHaulPilotStack(app, "SetuHaulPilot", lambda_asset=str(zip_path),
                               env=cdk.Environment(account="000000000000", region="ap-southeast-2"))
    app.synth()
    return stack, d / "cdk.out", zip_path


@pytest.fixture(scope="module")
def stack(built):
    return built[0]


@pytest.fixture(scope="module")
def t(stack):
    return Template.from_stack(stack)


def resources(t, kind):
    return t.find_resources(kind)


def functions(t):
    return {r["Properties"]["FunctionName"]: r["Properties"] for r in resources(t, "AWS::Lambda::Function").values()
            if "FunctionName" in r["Properties"]}


def test_everything_is_in_sydney(stack):
    assert stack.region == "ap-southeast-2"


def workload_functions(t):
    return {n: p for n, p in functions(t).items() if n.removeprefix("setuhaul-pilot-") not in PLATFORM_FUNCTIONS}


def test_option_d_switch_needs_the_owners_approval_reference_and_never_reaches_setup_functions(tmp_path):
    """Option D: absent context = OFF (the pilot, and every CI synth); a malformed reference is refused; the approval
    reference turns it on only for the api and the agent (the functions that make booking commitments), which then
    read the recorded manifest and still need all six gates at start-up; every other function keeps it off."""
    def synth(approval):
        app = cdk.App(outdir=str(tmp_path / f"out{len(approval)}"))
        s = SetuHaulPilotStack(app, "SetuHaulPilot", lambda_asset=str(_zip(tmp_path / f"l{len(approval)}.zip")),
                               booking_approval=approval, env=cdk.Environment(account="000000000000", region="ap-southeast-2"))
        return Template.from_stack(s)
    for bad in ("yes", "true", "OWNER-APPROVAL-2026-x", "on"):
        with pytest.raises(ValueError, match="OWNER-APPROVAL"):
            synth(bad)
    on = synth("OWNER-APPROVAL-20261123-test1")
    for name, p in functions(on).items():
        env = p["Environment"]["Variables"]
        booking = name.removeprefix("setuhaul-pilot-") in ("api", "agent")
        assert env["SETUHAUL_BOOKING_ENABLED"] == ("true" if booking else "false"), name
        assert env.get("SETUHAUL_DEPLOYMENT_MANIFEST_SOURCE") == ("database" if booking else None), name
        assert env["SETUHAUL_DATA_CLASS"] == "synthetic", name
    stmts = [s for pol in on.find_resources("AWS::IAM::Policy").values()
             for s in pol["Properties"]["PolicyDocument"]["Statement"]]
    reads = [s for s in stmts if s["Action"] == "lambda:GetFunctionConfiguration"]
    assert len(reads) == 2 and not any("*" in json.dumps(s["Resource"]) for s in reads)
    assert on.find_outputs("BookingMode")["BookingMode"]["Value"] == "ON (OWNER-APPROVAL-20261123-test1)"


def test_ci_never_passes_the_booking_approval():
    ci = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "booking_approval" not in ci and "BOOKING_ENABLED=true" not in ci


def test_only_the_activation_workflow_turns_booking_on_and_only_for_the_pilot_release_branch():
    """Option D (23 Nov 2026): a separate, manually started workflow; the release branch only, the typed confirmation
    and the owner's written reference; the deploy-evidence login only for the gate rows; ci.yml stays OFF."""
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "activate-booking.yml").read_text(encoding="utf-8")
    trig = wf.split("\non:", 1)[1].split("\npermissions:", 1)[0]
    assert "workflow_dispatch" in trig and "push" not in trig and "pull_request" not in trig and "schedule" not in trig
    # test, activate and (M-1) close-window-on-failure: every job that reaches AWS or gates one is pinned to the branch
    assert wf.count("github.ref == 'refs/heads/release/fde-sydney-pilot'") == 3
    assert wf.count('[ "$DEPLOY_REF" = "refs/heads/release/fde-sydney-pilot" ]') == 2   # both AWS jobs re-check
    assert '[ "$DEPLOY_REF" = "refs/heads/release/fde-sydney-pilot" ]' in wf
    assert '[ "$CONFIRM" = "activate-synthetic-pilot-booking" ]' in wf
    assert "^OWNER-APPROVAL-20[0-9]{6}-[A-Za-z0-9-]{3,40}$" in wf
    assert "{deploy_evidence}" in wf and "--environment pilot-ap-southeast-2" in wf
    assert 'on == {"setuhaul-pilot-api", "setuhaul-pilot-agent"}' in wf
    assert 'SETUHAUL_WHATSAPP_ENABLED"] == "false"' in wf and "test_sink" in wf
    assert "prod" not in wf.replace("production", "")


def test_the_booking_switch_is_off_and_the_data_is_synthetic_on_every_function(t):
    assert t.find_outputs("BookingMode")["BookingMode"]["Value"] == "OFF"
    fns = functions(t)
    assert len(workload_functions(t)) == 8   # api, agent, recovery, outbound, relay, and the three state-machine tasks
    assert set(fns) - set(workload_functions(t)) == {f"setuhaul-pilot-{n}" for n in PLATFORM_FUNCTIONS}
    for name, p in fns.items():
        env = p["Environment"]["Variables"]
        assert env["SETUHAUL_BOOKING_ENABLED"] == "false", name
        assert env["SETUHAUL_DATA_CLASS"] == "synthetic" and env["SETUHAUL_ENV"] == "pilot", name
        assert not set(PLAINTEXT_SECRETS) & set(env), f"{name} has a plain-text secret in its environment"


def test_aurora_express_private_in_the_vpc_with_the_data_api_and_audited(t):
    """The Free plan creates Aurora only with express configuration; CloudFormation cannot make it private, so the
    custom resource does (setuhaul/aws/express_cluster.py, tested in tests/test_express_cluster.py)."""
    assert not resources(t, "AWS::RDS::DBCluster") and not resources(t, "AWS::RDS::DBInstance")
    (db,) = resources(t, "Custom::SetuHaulExpressAurora").values()
    p = db["Properties"]
    assert p["ClusterIdentifier"] == "setuhaul-pilot" and p["DatabaseName"] == "setuhaul"
    assert (p["MinCapacity"], p["MaxCapacity"], p["SecondsUntilAutoPause"], p["BackupRetentionDays"]) == (
        "0", "2", str(AUTO_PAUSE_S), "1")
    assert p["ServiceToken"]["Fn::GetAtt"][0] == next(lid for lid, r in resources(t, "AWS::Lambda::Function").items()
                                                     if r["Properties"]["FunctionName"] == "setuhaul-pilot-db-express")
    (pg,) = resources(t, "AWS::RDS::DBClusterParameterGroup").values()
    assert pg["Properties"]["Family"] == "aurora-postgresql17"            # express creates the Region default, 17.9
    assert pg["Properties"]["Parameters"] == {"shared_preload_libraries": "pg_stat_statements,pgaudit",
                                              "pgaudit.log": "ddl,role", "pgaudit.log_parameter": "0",
                                              "rds.force_ssl": "1", "standard_conforming_strings": "1"}
    (sg,) = resources(t, "AWS::RDS::DBSubnetGroup").values()
    assert len(sg["Properties"]["SubnetIds"]) == len(SYDNEY_AZS) == 3     # a subnet in every AZ (express needs it)
    assert resources(t, "AWS::EC2::NatGateway") == {}                     # no NAT: Lambdas use the Data API
    assert resources(t, "AWS::EC2::InternetGateway") == {}
    assert resources(t, "AWS::EC2::EIP") == {} and resources(t, "AWS::EC2::VPCEndpoint") == {}
    assert all(s["Properties"].get("MapPublicIpOnLaunch") in (None, False) for s in resources(t, "AWS::EC2::Subnet").values())


def test_only_the_bootstrap_function_can_reach_the_database_port(t):
    """Port 5432 is open to the bootstrap function's security group only: no CIDR, no 0.0.0.0/0; the bootstrap
    function's group sends 5432 to the database group only. Application Lambdas stay outside the VPC."""
    sgs = resources(t, "AWS::EC2::SecurityGroup")
    db_lid = next(lid for lid, r in sgs.items() if r["Properties"]["GroupDescription"].startswith("Aurora"))
    boot_lid = next(lid for lid, r in sgs.items() if r["Properties"]["GroupDescription"].startswith("bootstrap"))
    for lid, r in sgs.items():
        for rule in r["Properties"].get("SecurityGroupIngress", []):
            assert "CidrIp" not in rule and "CidrIpv6" not in rule, lid
        for rule in r["Properties"].get("SecurityGroupEgress", []):
            assert rule.get("CidrIp") in (None, "255.255.255.255/32"), (lid, rule)   # CDK's 'no egress' placeholder
    (ing,) = resources(t, "AWS::EC2::SecurityGroupIngress").values()
    assert ing["Properties"]["GroupId"]["Fn::GetAtt"][0] == db_lid
    assert ing["Properties"]["SourceSecurityGroupId"]["Fn::GetAtt"][0] == boot_lid
    assert (ing["Properties"]["FromPort"], ing["Properties"]["ToPort"]) == (5432, 5432)
    (eg,) = resources(t, "AWS::EC2::SecurityGroupEgress").values()
    assert eg["Properties"]["GroupId"]["Fn::GetAtt"][0] == boot_lid
    assert eg["Properties"]["DestinationSecurityGroupId"]["Fn::GetAtt"][0] == db_lid
    in_vpc = [p["FunctionName"] for p in functions(t).values() if p.get("VpcConfig")]
    assert in_vpc == ["setuhaul-pilot-db-bootstrap"]


def test_the_express_resource_can_list_log_groups_but_change_only_the_database_one(t):
    """Regression (deploy run 45014745324): logs:DescribeLogGroups is account-level (resource "*"); creating and
    setting retention stay scoped to /aws/rds/cluster/setuhaul-pilot/postgresql."""
    fn = next(lid for lid, r in resources(t, "AWS::Lambda::Function").items()
              if r["Properties"]["FunctionName"] == "setuhaul-pilot-db-express")
    stmts = _policy_resources(t, fn)
    acts = lambda s: s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
    (lst,) = [s for s in stmts if "logs:DescribeLogGroups" in acts(s)]
    assert lst["Resource"] == "*"
    (chg,) = [s for s in stmts if "logs:PutRetentionPolicy" in acts(s)]
    assert set(acts(chg)) == {"logs:CreateLogGroup", "logs:PutRetentionPolicy"}
    assert "postgresql" in json.dumps(chg["Resource"]) and chg["Resource"] != "*"
    assert not [s for s in stmts if "logs:DeleteLogGroup" in acts(s) or "logs:*" in acts(s)]


def test_the_owner_login_is_not_the_master_and_only_the_express_resource_reads_it(t):
    owner = next(lid for lid, r in resources(t, "AWS::SecretsManager::Secret").items()
                 if r["Properties"].get("Name") == "setuhaul/pilot/db/owner")
    template = json.loads(resources(t, "AWS::SecretsManager::Secret")[owner]["Properties"]["GenerateSecretString"][
        "SecretStringTemplate"])
    assert template["username"] == "setuhaul_owner"
    readers = {r["Properties"]["FunctionName"] for lid, r in resources(t, "AWS::Lambda::Function").items()
               if owner in _readable_secrets(t, lid)}
    assert readers == {"setuhaul-pilot-db-express"}


def test_the_queue_contract_matches_the_aws_queue_gate(t):
    queues = [r["Properties"] for r in resources(t, "AWS::SQS::Queue").values()]
    main = next(q for q in queues if q["QueueName"] == "setuhaul-inbound.fifo")
    dlq = next(q for q in queues if q["QueueName"] == "setuhaul-inbound-dlq.fifo")
    assert main["FifoQueue"] and dlq["FifoQueue"] and main["ContentBasedDeduplication"] is False
    assert main["VisibilityTimeout"] >= 2 * LEASE_S
    assert 1 <= main["RedrivePolicy"]["maxReceiveCount"] <= 10
    assert dlq["MessageRetentionPeriod"] >= main["MessageRetentionPeriod"]


def _policy_resources(t, fn_logical_id):
    role = resources(t, "AWS::Lambda::Function")[fn_logical_id]["Properties"]["Role"]["Fn::GetAtt"][0]
    stmts = []
    for p in resources(t, "AWS::IAM::Policy").values():
        if any(r.get("Ref") == role for r in p["Properties"]["Roles"]):
            stmts += p["Properties"]["PolicyDocument"]["Statement"]
    return stmts


def _login_secret_ids(t):
    return {lid: json.loads(r["Properties"]["GenerateSecretString"]["SecretStringTemplate"])["username"]
            for lid, r in resources(t, "AWS::SecretsManager::Secret").items()
            if "username" in r["Properties"].get("GenerateSecretString", {}).get("SecretStringTemplate", "")}


@pytest.mark.parametrize("workload", list(WORKLOAD_ROLES))
def test_each_function_can_read_only_its_own_database_logins(t, workload):
    logins = _login_secret_ids(t)
    fn_ids = {p["FunctionName"]: lid for lid, r in resources(t, "AWS::Lambda::Function").items()
              if (p := r["Properties"]).get("FunctionName")}
    names = [n for n in fn_ids if n == f"setuhaul-pilot-{workload}" or (workload == "approval" and "approval" in n)]
    assert names
    for name in names:
        readable = set()
        for s in _policy_resources(t, fn_ids[name]):
            if "secretsmanager:GetSecretValue" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]):
                res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
                readable |= {r["Ref"] for r in res if isinstance(r, dict) and "Ref" in r}
        # M2 (0032): the booking roles' logins carry the release (setuhaul_login_<role>_<12 hex>)
        got = {re.sub(r"_[0-9a-f]{12}$", "", logins[r].removeprefix("setuhaul_login_")) for r in readable if r in logins}
        assert got == set(WORKLOAD_ROLES[workload]), (name, got)
        assert "deploy_evidence" not in got and "owner" not in got


def test_the_approval_state_machine_is_the_repository_definition_with_a_published_version(t):
    sm = next(iter(resources(t, "AWS::StepFunctions::StateMachine").values()))["Properties"]
    body = sm["DefinitionString"]
    text = json.dumps(body)
    for state in ("NotifyApprovers", "WaitForDecision", "ExpireApproval", "waitForTaskToken"):
        assert state in text
    assert resources(t, "AWS::StepFunctions::StateMachineVersion")
    t.has_resource_properties("AWS::StepFunctions::StateMachineAlias", {"Name": "live"})


def test_low_cost_pilot_no_waf_but_throttling_and_staff_need_mfa(t):
    """09_LOW_COST_PILOT_PLAN.md: WAF removed for this controlled synthetic pilot; throttling, Cognito MFA and the
    admin-only sign-up remain (signed webhooks and input validation are tested in test_api.py)."""
    assert not resources(t, "AWS::WAFv2::WebACL") and not resources(t, "AWS::WAFv2::WebACLAssociation")
    assert not resources(t, "AWS::ApiGateway::Account")         # the region's existing logging role is left alone
    t.has_resource_properties("AWS::ApiGateway::Stage", {"MethodSettings": Match.array_with([
        Match.object_like({"ThrottlingRateLimit": 20, "ThrottlingBurstLimit": 40})])})
    t.has_resource_properties("AWS::Cognito::UserPool", {"MfaConfiguration": "ON",
                                                         "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}})
    groups = {r["Properties"]["GroupName"] for r in resources(t, "AWS::Cognito::UserPoolGroup").values()}
    assert {g for g in groups if not g.startswith("facility-")} == {"operations", "planner", "ops_lead"}


def test_the_agent_reads_messages_one_at_a_time_and_reports_failures(t):
    t.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "BatchSize": 1, "FunctionResponseTypes": ["ReportBatchItemFailures"]})


def test_every_gate_monitoring_alarm_exists(t):
    alarms = resources(t, "AWS::CloudWatch::Alarm")
    metrics = {a["Properties"].get("MetricName") for a in alarms.values()}
    assert {"ApproximateAgeOfOldestMessage", "ApproximateNumberOfMessagesVisible", "5XXError", "ExecutionsFailed",
            "ExecutionsTimedOut", "Errors"} <= metrics
    assert all(a["Properties"].get("AlarmActions") for a in alarms.values())


def test_aurora_can_pause_nothing_wakes_it_while_idle_and_logs_are_short(t):
    """Acceptance 2 (configuration side): 0 ACU minimum with a 300-s auto-pause, and no schedule runs while the pilot
    sleeps (each would query the database every minute and keep it from pausing). Nothing that prevents pausing:
    no RDS Proxy, no Performance Insights, no enhanced monitoring, no replica."""
    rules = resources(t, "AWS::Events::Rule")
    assert len(rules) == len(SCHEDULED)
    assert {r["Properties"]["Name"] for r in rules.values()} == {schedule_rule_name("pilot", n) for n in SCHEDULED}
    assert all(r["Properties"]["State"] == "DISABLED" for r in rules.values())
    assert not resources(t, "AWS::RDS::DBProxy")
    # one serverless writer, created by express configuration (no Performance Insights, no enhanced monitoring);
    # the probe of 22 Nov 2026 measured 0 ACU five minutes after the last Data API connection closed
    assert not resources(t, "AWS::RDS::DBInstance") and len(resources(t, "Custom::SetuHaulExpressAurora")) == 1
    groups = resources(t, "AWS::Logs::LogGroup").values()
    assert groups and all(g["Properties"]["RetentionInDays"] == 7 for g in groups)
    env = {n: p["Environment"]["Variables"] for n, p in workload_functions(t).items()}
    assert env["setuhaul-pilot-api"]["SETUHAUL_DB_RESUME_WAIT_S"] == "20"           # inside API Gateway's 29 s
    assert all(e["SETUHAUL_DB_RESUME_WAIT_S"] == "40" for n, e in env.items() if n != "setuhaul-pilot-api")


def test_the_4xx_alarm_is_named_for_what_it_counts(t):
    """Review E0-01: API Gateway's 4XXError counts every client error, so the alarm must not claim to be about auth."""
    alarms = resources(t, "AWS::CloudWatch::Alarm")
    four = {lid: a["Properties"] for lid, a in alarms.items() if a["Properties"].get("MetricName") == "4XXError"}
    assert len(four) == 1
    lid, props = next(iter(four.items()))
    assert lid.startswith("Api4xxClientErrors") and "Auth" not in lid
    assert "all client errors" in props["AlarmDescription"]


# ================================================================ E5: the Cloud Assembly is what gets deployed
def test_the_lambda_asset_must_be_the_built_zip(tmp_path):
    folder = tmp_path / "lambda"
    folder.mkdir()
    with pytest.raises(ValueError, match="built zip"):
        SetuHaulPilotStack(cdk.App(outdir=str(tmp_path / "o")), "S", lambda_asset=str(folder))


def test_the_app_writes_its_assembly_to_the_named_directory(tmp_path):
    """Review E5-01: run as the pipeline runs it (no CDK CLI); the assembly lands in CDK_OUTDIR, not a temp dir."""
    import os
    import subprocess
    zip_path = _zip(tmp_path / "lambda.zip")
    out = tmp_path / "cdk.out"
    env = {**os.environ, "CDK_OUTDIR": str(out), "CDK_CONTEXT_JSON": json.dumps({"lambda_asset": str(zip_path)})}
    subprocess.run([sys.executable, str(ROOT / "infra" / "app.py")], env=env, check=True, cwd=tmp_path, timeout=600)
    assert (out / "manifest.json").is_file() and (out / "SetuHaulPilot.template.json").is_file()
    src = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
    assert 'outdir=os.environ.get("CDK_OUTDIR") or str(ROOT / "cdk.out")' in src   # default without the CLI


def test_the_assembly_check_follows_template_to_staged_zip(built):
    from setuhaul.ops.assembly import verify
    _, out, zip_path = built
    summary, problems = verify(out, zip_path=zip_path)
    assert problems == [], problems
    assert summary["functions"] == 11 and summary["staged_asset"].endswith(".zip")
    import hashlib
    assert summary["sha256"] == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert summary["sha256"] != summary["asset_object_key"].removesuffix(".zip")   # CDK's id is not the file hash


def test_every_lambda_runs_the_verified_zip_and_no_cdk_helper_function_exists(t):
    """Review E5-03: Aurora's log retention no longer adds a CDK-provided Lambda whose code nobody hashed. The one
    custom resource (express Aurora) is served by a function of this template, running the verified zip."""
    codes = {json.dumps(r["Properties"]["Code"], sort_keys=True) for r in resources(t, "AWS::Lambda::Function").values()}
    assert len(codes) == 1
    customs = {k: r for k, r in t.to_json()["Resources"].items() if r["Type"].startswith("Custom::")}
    assert list(customs) == ["Db"] and customs["Db"]["Type"] == "Custom::SetuHaulExpressAurora"
    assert customs["Db"]["Properties"]["ServiceToken"]["Fn::GetAtt"][0] in resources(t, "AWS::Lambda::Function")
    (db,) = resources(t, "Custom::SetuHaulExpressAurora").values()      # it keeps the database log group (7 days)
    assert (db["Properties"]["LogGroupName"], db["Properties"]["LogRetentionDays"]) == (
        "/aws/rds/cluster/setuhaul-pilot/postgresql", "7")


def _copy_assembly(out, tmp_path):
    import shutil
    dst = tmp_path / "cdk.out"
    shutil.copytree(out, dst)
    return dst


def test_the_assembly_check_fails_when_the_staged_zip_is_altered(built, tmp_path):
    from setuhaul.ops.assembly import verify
    _, out, zip_path = built
    dst = _copy_assembly(out, tmp_path)
    staged = dst / verify(out)[0]["staged_asset"]
    _zip(staged, "tampered")
    _, problems = verify(dst, zip_path=zip_path)
    assert any("differs from the staged asset" in p for p in problems)


def _lambda_ids(out):
    tpl = json.loads((out / "SetuHaulPilot.template.json").read_text(encoding="utf-8"))
    return sorted(lid for lid, r in tpl["Resources"].items() if r["Type"] == "AWS::Lambda::Function")


def _mutate_code(out, tmp_path, lid, change):
    """A copy of the assembly in which only function `lid` has its Code changed by `change(code)`."""
    dst = _copy_assembly(out, tmp_path)
    tpl_path = dst / "SetuHaulPilot.template.json"
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    change(tpl["Resources"][lid]["Properties"]["Code"])
    tpl_path.write_text(json.dumps(tpl), encoding="utf-8")
    return dst


def test_e5_07_a_valid_assembly_passes_with_every_function_checked(built):
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    summary, problems = verify(out)
    assert problems == []
    assert summary["functions_checked"] == _lambda_ids(out) and len(summary["functions_checked"]) == 11
    # a literal name here (the fixture names an account); the pipeline's account-agnostic synth writes the
    # `${AWS::AccountId}` form. Either way it must equal the asset manifest destination, which verify() checked.
    assert summary["asset_bucket"].startswith("cdk-") and summary["asset_bucket"].endswith("-ap-southeast-2")


def test_the_assembly_check_fails_when_a_function_points_elsewhere(built, tmp_path):
    """E5-07: only one function's S3Key changes; its bucket is still the right one."""
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    lid = _lambda_ids(out)[0]
    dst = _mutate_code(out, tmp_path, lid, lambda c: c.__setitem__("S3Key", "0" * 64 + ".zip"))
    problems = verify(dst)[1]
    assert any(p.startswith(f"{lid}:") and "no asset publishes that key" in p for p in problems), problems


def test_e5_07_a_function_in_another_bucket_with_the_same_key_is_refused(built, tmp_path):
    """Only one function's S3Bucket changes; its S3Key is still the staged asset's key."""
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    lid = _lambda_ids(out)[-1]
    dst = _mutate_code(out, tmp_path, lid, lambda c: c.__setitem__("S3Bucket", "attacker-bucket"))
    problems = verify(dst)[1]
    assert any(p.startswith(f"{lid}:") and "its bucket matches no destination" in p for p in problems), problems


def test_e5_07_every_function_is_checked_not_one_representative(built, tmp_path):
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    for i, lid in enumerate(_lambda_ids(out)):
        dst = _mutate_code(out, tmp_path / str(i), lid,
                           lambda c: c.__setitem__("S3Bucket", {"Fn::Sub": "cdk-other-assets-${AWS::AccountId}-ap-southeast-2"}))
        problems = verify(dst)[1]
        assert [p for p in problems if p.startswith(f"{lid}:")], (lid, problems)
        assert not [p for p in problems if not p.startswith(f"{lid}:")], (lid, problems)   # the others still pass


@pytest.mark.parametrize("change,expected", [
    (lambda c: c.pop("S3Bucket"), "Code must be exactly S3Bucket and S3Key"),
    (lambda c: c.__setitem__("ZipFile", "print(1)"), "Code must be exactly S3Bucket and S3Key"),
    (lambda c: c.__setitem__("S3Bucket", {"Fn::Join": ["", ["a", "b"]]}), "S3Bucket is not a fixed bucket"),
    (lambda c: c.__setitem__("S3Bucket", {"Ref": "SomeBucket"}), "S3Bucket is not a fixed bucket"),
    (lambda c: c.__setitem__("S3Bucket", {"Fn::Sub": ["x-${A}", {"A": "b"}]}), "S3Bucket is not a fixed bucket"),
    (lambda c: c.__setitem__("S3Bucket", ""), "S3Bucket is not a fixed bucket"),
    (lambda c: c.__setitem__("S3Key", {"Fn::Sub": "x.zip"}), "S3Key is not a fixed object key"),
    (lambda c: c.__setitem__("S3Key", ""), "S3Key is not a fixed object key"),
])
def test_e5_07_a_missing_or_malformed_code_reference_is_refused(built, tmp_path, change, expected):
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    lid = _lambda_ids(out)[3]
    problems = verify(_mutate_code(out, tmp_path, lid, change))[1]
    assert any(p.startswith(f"{lid}:") and expected in p for p in problems), problems


def test_e5_07_an_asset_destination_in_another_bucket_is_refused(built, tmp_path):
    """The template is untouched, but the asset manifest would publish the key to a different bucket."""
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    dst = _copy_assembly(out, tmp_path)
    am_path = dst / "SetuHaulPilot.assets.json"
    am = json.loads(am_path.read_text(encoding="utf-8"))
    key = verify(out)[0]["asset_object_key"]
    for a in am["files"].values():
        for d in a["destinations"].values():
            if d["objectKey"] == key:
                d["bucketName"] = "cdk-other-assets-${AWS::AccountId}-ap-southeast-2"
    am_path.write_text(json.dumps(am), encoding="utf-8")
    problems = verify(dst)[1]
    assert len([p for p in problems if "its bucket matches no destination" in p]) == 11, problems


def test_the_assembly_check_fails_when_the_staged_asset_is_missing(built, tmp_path):
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    dst = _copy_assembly(out, tmp_path)
    (dst / verify(out)[0]["staged_asset"]).unlink()
    assert any("missing from the assembly" in p for p in verify(dst)[1])


def test_the_deployment_manifest_records_the_staged_asset_and_the_check_compares_them(built):
    from setuhaul.ops.assembly import verify
    from setuhaul.ops.manifest import build, from_assembly
    _, out, _ = built
    tpl, artifact = from_assembly(out)
    outs = {"ApprovalAliasArn": "arn:aws:states:ap-southeast-2:000000000000:stateMachine:setuhaul-pilot-approval:live",
            "ApprovalVersionArn": "arn:aws:states:ap-southeast-2:000000000000:stateMachine:setuhaul-pilot-approval:1"}
    roles = {w: list(r) for w, r in WORKLOAD_ROLES.items()}
    good = build(tpl, outs, artifact, "pilot-ap-southeast-2", roles)
    assert verify(out, manifest=good)[1] == []
    other = build(tpl, outs, b"a different build", "pilot-ap-southeast-2", roles)
    assert any("deployment manifest records" in p for p in verify(out, manifest=other)[1])


# ================================================================ IAM-01 / IAM-02
def _readable_secrets(t, fn_logical_id):
    readable = set()
    for s in _policy_resources(t, fn_logical_id):
        if "secretsmanager:GetSecretValue" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]):
            res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
            readable |= {r["Ref"] for r in res if isinstance(r, dict) and "Ref" in r}
    return readable


def test_only_the_outbound_function_can_read_the_whatsapp_token(t):
    secrets = resources(t, "AWS::SecretsManager::Secret")
    wa = [lid for lid, r in secrets.items() if r["Properties"].get("Name") == "setuhaul/pilot/whatsapp"]
    app = next(r for r in secrets.values() if r["Properties"].get("Name") == "setuhaul/pilot/app")
    assert len(wa) == 1
    assert "WHATSAPP_TOKEN" not in app["Properties"]["GenerateSecretString"]["SecretStringTemplate"]
    readers = set()
    for lid, r in resources(t, "AWS::Lambda::Function").items():
        name = r["Properties"]["FunctionName"]
        env = r["Properties"]["Environment"]["Variables"]
        if wa[0] in _readable_secrets(t, lid):
            readers.add(name)
        assert ("SETUHAUL_WHATSAPP_SECRET_ID" in env) == (name == "setuhaul-pilot-outbound"), name
    assert readers == {"setuhaul-pilot-outbound"}


def test_the_pilot_sends_notifications_to_the_test_sink_only(t):
    """WhatsApp isolation: the outbound function is configured for the internal sink and WhatsApp is off on every
    function; no function enables WhatsApp."""
    for name, p in functions(t).items():
        env = p["Environment"]["Variables"]
        assert env.get("SETUHAUL_WHATSAPP_ENABLED", "false") == "false", name
        assert env.get("SETUHAUL_NOTIFICATION_MODE", "test_sink") == "test_sink", name
    out = functions(t)["setuhaul-pilot-outbound"]["Environment"]["Variables"]
    assert out["SETUHAUL_NOTIFICATION_MODE"] == "test_sink" and out["SETUHAUL_TEST_SINK_FAIL_PHONES"].startswith("+999")


def test_the_pilot_api_has_no_tools_route_and_no_agent_or_feasibility_login(t):
    api = functions(t)["setuhaul-pilot-api"]
    assert api["Environment"]["Variables"]["SETUHAUL_TOOLS_ROUTE"] == "off"
    assert WORKLOAD_ROLES["api"] == ("ingest", "console")


def test_the_handler_tests_use_the_stack_login_table():
    import test_aws
    assert test_aws.WORKLOAD_ROLES == WORKLOAD_ROLES


# ================================================================ E5: manifest, E6: WhatsApp templates
def test_the_manifest_built_from_this_template_passes_the_start_up_check(t):
    import copy
    from setuhaul.api.inbound import deployment_id_for, manifest_problems
    from setuhaul.ops.manifest import build
    tpl = t.to_json()
    outs = {"ApprovalAliasArn": "arn:aws:states:ap-southeast-2:000000000000:stateMachine:setuhaul-pilot-approval:live",
            "ApprovalVersionArn": "arn:aws:states:ap-southeast-2:000000000000:stateMachine:setuhaul-pilot-approval:1"}
    roles = {w: list(r) for w, r in WORKLOAD_ROLES.items()}
    m = build(tpl, outs, b"artifact-1", "pilot-ap-southeast-2", roles)
    assert manifest_problems(m, "pilot-ap-southeast-2") == []
    assert m["gates"]["RECOVERY_WORKER"]["queue_visibility_timeout_s"] >= 2 * LEASE_S
    assert deployment_id_for(m) == deployment_id_for(build(tpl, outs, b"artifact-1", "pilot-ap-southeast-2", roles))
    assert deployment_id_for(m) != deployment_id_for(build(tpl, outs, b"artifact-2", "pilot-ap-southeast-2", roles))
    changed = copy.deepcopy(tpl)                                   # any IAM change is a new deployment
    pol = next(r for r in changed["Resources"].values() if r["Type"] == "AWS::IAM::Policy")
    pol["Properties"]["PolicyDocument"]["Statement"].append({"Effect": "Allow", "Action": "sqs:*", "Resource": "*"})
    assert build(changed, outs, b"artifact-1", "pilot-ap-southeast-2", roles)["gates"]["WORKLOAD_IDENTITY"] != \
        m["gates"]["WORKLOAD_IDENTITY"]


def test_the_whatsapp_template_file_matches_the_reviewed_texts():
    sys.path.insert(0, str(ROOT / "scripts"))
    import whatsapp_templates as wt
    assert (ROOT / "infra" / "whatsapp_templates.json").read_text(encoding="utf-8") == wt.render()
    defs = wt.definitions()
    assert len(defs) == 21 and len({d["name"] for d in defs if d["language"] != "hi"} | {
        d["name"] + ":hi" for d in defs if d["language"] == "hi"}) == 21
    assert all(d["category"] == "UTILITY" for d in defs)
    hl = [d for d in defs if d["name"].endswith("_hl")]
    # Hinglish: definitions kept, language code NOT assumed; decided with Meta at a separate gate (review decision)
    assert len(hl) == 7 and all(d["language"] is None and d["submission_gate"] == "HINGLISH_TEMPLATES" for d in hl)
    assert all(d["language"] in ("en", "hi") for d in defs if not d["name"].endswith("_hl"))


# ================================================================ hosted staff console (docs/release/11)
def test_the_console_is_served_by_the_api_no_bucket_no_cdn(t):
    """CloudFront needs AWS account verification on this account, so the page is served by the API itself (same
    origin as the data, HTTPS by API Gateway): no S3 bucket, no CloudFront, no CORS setting anywhere."""
    for kind in ("AWS::S3::Bucket", "AWS::CloudFront::Distribution", "AWS::CloudFront::OriginAccessControl"):
        assert not resources(t, kind), kind
    assert len(resources(t, "AWS::ApiGateway::RestApi")) == 1
    assert len(resources(t, "AWS::ApiGateway::Method")) == 2                    # root ANY and {proxy+} ANY only
    assert all("SETUHAUL_CONSOLE_ORIGIN" not in p["Environment"]["Variables"] for p in functions(t).values())
    env = functions(t)["setuhaul-pilot-api"]["Environment"]["Variables"]
    assert env["COGNITO_DOMAIN"] == "setuhaul-pilot-console.auth.ap-southeast-2.amazoncognito.com"
    assert env["COGNITO_CONSOLE_CLIENT_ID"] == env["COGNITO_CLIENT_IDS"]


def test_staff_sign_in_is_code_with_pkce_on_a_public_client_back_to_the_console_only(t):
    (c,) = resources(t, "AWS::Cognito::UserPoolClient").values()
    p = c["Properties"]
    assert p["AllowedOAuthFlows"] == ["code"] and p["AllowedOAuthFlowsUserPoolClient"] is True
    assert p["GenerateSecret"] is False and p["AllowedOAuthScopes"] == ["openid"]
    api = next(iter(resources(t, "AWS::ApiGateway::RestApi")))
    want = {"Fn::Join": ["", ["https://", {"Ref": api}, ".execute-api.ap-southeast-2.amazonaws.com/pilot/app/"]]}
    assert p["CallbackURLs"] == [want] and p["LogoutURLs"] == [want]
    assert (p["AccessTokenValidity"], p["TokenValidityUnits"]["AccessToken"]) == (30, "minutes")
    assert p["PreventUserExistenceErrors"] == "ENABLED" and p["SupportedIdentityProviders"] == ["COGNITO"]
    (pool,) = resources(t, "AWS::Cognito::UserPool").values()
    assert pool["Properties"]["MfaConfiguration"] == "ON"
    assert pool["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    (dom,) = resources(t, "AWS::Cognito::UserPoolDomain").values()
    assert dom["Properties"]["Domain"] == "setuhaul-pilot-console"


def test_facility_scope_comes_from_the_v2_trigger_and_admin_groups(t):
    (pool,) = resources(t, "AWS::Cognito::UserPool").values()
    trig = pool["Properties"]["LambdaConfig"]["PreTokenGenerationConfig"]
    assert trig["LambdaVersion"] == "V2_0"
    fn = resources(t, "AWS::Lambda::Function")[trig["LambdaArn"]["Fn::GetAtt"][0]]["Properties"]
    assert fn["FunctionName"] == "setuhaul-pilot-cognito-pretoken" and fn["Handler"] == "setuhaul.aws.pretoken.handler"
    assert fn["Environment"]["Variables"]["FACILITIES"] == "FAC-JAI-01,FAC-GGN-01" and "VpcConfig" not in fn
    groups = {g["Properties"]["GroupName"] for g in resources(t, "AWS::Cognito::UserPoolGroup").values()}
    assert groups == {"operations", "planner", "ops_lead", "facility-FAC-JAI-01", "facility-FAC-GGN-01"}
    assert not [s for s in _policy_resources(t, trig["LambdaArn"]["Fn::GetAtt"][0])
                if "secretsmanager" in json.dumps(s) or "rds-data" in json.dumps(s)]       # no secret, no database


def test_the_relay_may_start_executions_through_the_alias(t):
    """Step 6 drill, 23 Nov 2026: with StartExecution granted on the alias ARN only, every start was denied on the
    state machine's own ARN. Both must be granted; the relay still calls the alias (published version)."""
    relay = next(p for n, p in functions(t).items() if n.endswith("approval-relay"))
    role_ref = relay["Role"]["Fn::GetAtt"][0]
    stmts = [st for pol in t.find_resources("AWS::IAM::Policy").values()
             if any(r.get("Ref") == role_ref for r in pol["Properties"]["Roles"])
             for st in pol["Properties"]["PolicyDocument"]["Statement"]]
    start = [st for st in stmts if st["Action"] == "states:StartExecution"]
    assert len(start) == 1
    res = json.dumps(start[0]["Resource"])
    alias = next(iter(t.find_resources("AWS::StepFunctions::StateMachineAlias")))
    machine = next(iter(t.find_resources("AWS::StepFunctions::StateMachine")))
    assert alias in res and machine in res and len(start[0]["Resource"]) == 2
    assert alias in json.dumps(relay["Environment"]["Variables"]["SETUHAUL_APPROVAL_STATE_MACHINE_ARN"])


# ================================================================ Phase G.1: OpenRouter secret pinned, LangSmith opt-in
def test_g1_the_openrouter_secret_resource_is_unchanged_so_a_deploy_cannot_overwrite_the_owners_key(t):
    """The OpenRouter secret was created with a CloudFormation-generated placeholder (GenerateSecretString: {}): the
    root cause of the pilot's 401s (OPENROUTER_PILOT_DIAGNOSIS.md). Changing that property now would make
    CloudFormation replace the VALUE, possibly a real key the owner has set, so G.1 leaves the resource exactly as is."""
    (props,) = [r["Properties"] for lid, r in resources(t, "AWS::SecretsManager::Secret").items()
                if lid.startswith("OpenRouterKey")]
    assert props["GenerateSecretString"] == {} and "SecretString" not in props
    assert props["Name"] == "setuhaul/pilot/openrouter"


def test_g1_langsmith_is_off_by_default_no_secret_no_permission_no_environment(t):
    assert not [lid for lid in resources(t, "AWS::SecretsManager::Secret") if lid.startswith("LangSmith")]
    for name, f in functions(t).items():
        assert not [k for k in f.get("Environment", {}).get("Variables", {}) if "LANGSMITH" in k or k == "SETUHAUL_TRACE_CONTENT"], name


def test_g1_langsmith_opt_in_reaches_the_agent_only_with_an_explicit_empty_placeholder(tmp_path):
    zip_path = _zip(tmp_path / "lambda.zip")
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = SetuHaulPilotStack(app, "SetuHaulPilot", lambda_asset=str(zip_path), langsmith_tracing="on",
                               langsmith_ui_url="https://smith.langchain.com/o/x/projects/p/y",
                               env=cdk.Environment(account="000000000000", region="ap-southeast-2"))
    tl = Template.from_stack(stack)
    (lid, sec), = [(k, r) for k, r in resources(tl, "AWS::SecretsManager::Secret").items() if k.startswith("LangSmith")]
    assert sec["Properties"]["SecretString"] == '{"api_key": ""}' and "GenerateSecretString" not in sec["Properties"]
    fns = functions(tl)
    agent, api = fns["setuhaul-pilot-agent"]["Environment"]["Variables"], fns["setuhaul-pilot-api"]["Environment"]["Variables"]
    assert agent["SETUHAUL_LANGSMITH_TRACING"] == "on" and agent["SETUHAUL_TRACE_CONTENT"] == "off"
    assert agent["SETUHAUL_LANGSMITH_PROJECT"] == "setuhaul-synthetic-pilot" and "SETUHAUL_LANGSMITH_SECRET_ID" in agent
    assert "SETUHAUL_LANGSMITH_SECRET_ID" not in api and api["SETUHAUL_LANGSMITH_UI_URL"].startswith("https://smith.")
    readers = [n for n, f in fns.items()
               for lid2 in [next(k for k, r in resources(tl, "AWS::Lambda::Function").items()
                                 if r["Properties"].get("FunctionName") == n)]
               if lid in json.dumps(_policy_resources(tl, lid2))]
    assert readers == ["setuhaul-pilot-agent"]                          # only the agent can read the LangSmith key
    with pytest.raises(ValueError):
        SetuHaulPilotStack(cdk.App(outdir=str(tmp_path / "o2")), "X", lambda_asset=str(zip_path),
                           langsmith_ui_url="https://evil.example.com/")


# ================================================================ M2 (0032): a database identity and an IAM role per release
def test_the_booking_logins_and_execution_roles_are_per_release(t, built):
    import hashlib
    from setuhaul_stack import RELEASE_DB_ROLES, RELEASE_FUNCTIONS
    release = hashlib.sha256(built[2].read_bytes()).hexdigest()[:12]
    logins = set(_login_secret_ids(t).values())
    for role in RELEASE_DB_ROLES:
        assert f"setuhaul_login_{role}_{release}" in logins and f"setuhaul_login_{role}" not in logins
    for role in ("ingest", "feasibility", "system", "deploy_evidence"):
        assert f"setuhaul_login_{role}" in logins                    # unchanged identities
    fns = {p["FunctionName"]: p for r in resources(t, "AWS::Lambda::Function").values()
           if (p := r["Properties"]).get("FunctionName")}
    seen = set()
    for name, p in fns.items():
        role_id = p["Role"]["Fn::GetAtt"][0]
        if name.removeprefix("setuhaul-pilot-") in RELEASE_FUNCTIONS:
            assert release in role_id, (name, role_id)
            seen.add(name)
        else:
            assert release not in role_id, (name, role_id)
    assert seen == {f"setuhaul-pilot-{f}" for f in RELEASE_FUNCTIONS}
