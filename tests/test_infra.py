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


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    asset = tmp_path_factory.mktemp("lambda")
    (asset / "placeholder.txt").write_text("synth only")
    app = cdk.App()
    return SetuHaulPilotStack(app, "SetuHaulPilot", lambda_asset=str(asset),
                              env=cdk.Environment(account="000000000000", region="ap-southeast-2"))


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
    assert len(defs) == 21 and len({(d["name"], d["language"]) for d in defs}) == 21
    assert all(d["category"] == "UTILITY" for d in defs)
