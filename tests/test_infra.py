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
from setuhaul_stack import LEASE_S, WORKLOAD_ROLES, SetuHaulPilotStack  # noqa: E402

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
        "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 4}})
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


def test_the_api_has_waf_and_throttling_and_staff_need_mfa(t):
    assert resources(t, "AWS::WAFv2::WebACLAssociation")
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
                                                      "RetentionInDays": 30})


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


def test_the_assembly_check_fails_when_a_function_points_elsewhere(built, tmp_path):
    from setuhaul.ops.assembly import verify
    _, out, _ = built
    dst = _copy_assembly(out, tmp_path)
    tpl_path = dst / "SetuHaulPilot.template.json"
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    fn = next(r for r in tpl["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    fn["Properties"]["Code"]["S3Key"] = "0" * 64 + ".zip"
    tpl_path.write_text(json.dumps(tpl), encoding="utf-8")
    assert any("different code objects" in p for p in verify(dst)[1])


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
