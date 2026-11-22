"""E0: the CDK stack synthesises, and the template has the properties the deployment gates depend on.

No AWS call: the stack is synthesised in-process (aws-cdk-lib, requirements-infra.txt). Skipped if CDK is not installed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
from setuhaul_stack import AUTO_PAUSE_S, LEASE_S, SCHEDULED, WORKLOAD_ROLES, SetuHaulPilotStack, schedule_rule_name  # noqa: E402

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


def test_the_booking_switch_is_off_and_the_data_is_synthetic_on_every_function(t):
    fns = functions(t)
    assert len(fns) == 8          # api, agent, recovery, outbound, approval relay, and the three state-machine tasks
    for name, p in fns.items():
        env = p["Environment"]["Variables"]
        assert env["SETUHAUL_BOOKING_ENABLED"] == "false", name
        assert env["SETUHAUL_DATA_CLASS"] == "synthetic" and env["SETUHAUL_ENV"] == "pilot", name
        assert not set(PLAINTEXT_SECRETS) & set(env), f"{name} has a plain-text secret in its environment"


def test_aurora_postgresql_18_serverless_with_the_data_api_isolated_and_audited(t):
    t.has_resource_properties("AWS::RDS::DBCluster", {
        "Engine": "aurora-postgresql", "EngineVersion": Match.string_like_regexp(r"^18\."),
        "EnableHttpEndpoint": True, "StorageEncrypted": True, "DeletionProtection": True,
        "ServerlessV2ScalingConfiguration": {"MinCapacity": 0, "MaxCapacity": 2, "SecondsUntilAutoPause": AUTO_PAUSE_S},
        "BackupRetentionPeriod": 1})
    t.has_resource_properties("AWS::RDS::DBClusterParameterGroup", {"Parameters": Match.object_like(
        {"shared_preload_libraries": "pgaudit", "pgaudit.log": "ddl,role", "pgaudit.log_parameter": "0",
         "standard_conforming_strings": "1"})})
    assert resources(t, "AWS::EC2::NatGateway") == {}                     # no NAT: Lambdas use the Data API
    assert resources(t, "AWS::EC2::InternetGateway") == {}


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
        got = {logins[r].removeprefix("setuhaul_login_") for r in readable if r in logins}
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
    assert groups == {"operations", "planner", "ops_lead"}


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
    (inst,) = resources(t, "AWS::RDS::DBInstance").values()
    assert not inst["Properties"].get("EnablePerformanceInsights") and not inst["Properties"].get("MonitoringInterval")
    assert len(resources(t, "AWS::RDS::DBInstance")) == 1
    groups = resources(t, "AWS::Logs::LogGroup").values()
    assert groups and all(g["Properties"]["RetentionInDays"] == 7 for g in groups)
    env = {n: p["Environment"]["Variables"] for n, p in functions(t).items()}
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
    assert summary["functions"] == 8 and summary["staged_asset"].endswith(".zip")
    import hashlib
    assert summary["sha256"] == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert summary["sha256"] != summary["asset_object_key"].removesuffix(".zip")   # CDK's id is not the file hash


def test_every_lambda_runs_the_verified_zip_and_no_cdk_helper_function_exists(t):
    """Review E5-03: Aurora's log retention no longer adds a CDK-provided Lambda whose code nobody hashed."""
    codes = {json.dumps(r["Properties"]["Code"], sort_keys=True) for r in resources(t, "AWS::Lambda::Function").values()}
    assert len(codes) == 1
    assert not [k for k, r in t.to_json()["Resources"].items() if r["Type"].startswith("Custom::")]
    t.has_resource_properties("AWS::Logs::LogGroup", {"LogGroupName": "/aws/rds/cluster/setuhaul-pilot/postgresql",
                                                      "RetentionInDays": 7})


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
    assert summary["functions_checked"] == _lambda_ids(out) and len(summary["functions_checked"]) == 8
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
    assert len([p for p in problems if "its bucket matches no destination" in p]) == 8, problems


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
