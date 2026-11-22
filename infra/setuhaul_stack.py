"""SetuHaul Sydney pilot infrastructure (build item E0). AWS CDK v2, Python. Region ap-southeast-2 only.

Not deployed. `python infra/app.py` writes the Cloud Assembly to `cdk.out/`; `tests/test_infra.py` asserts the
properties the six deployment gates depend on, and `setuhaul/ops/assembly.py` checks that every function's code is the
one staged `build/lambda.zip`. Deploying needs the owner's approval and sign-in, and deploys that assembly as built.

Shape (docs/release/04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md §2; low-cost choices in docs/release/09_LOW_COST_PILOT_PLAN.md):
  REST API (throttling; no WAF in this synthetic pilot) → api Lambda → SQS FIFO (+ FIFO DLQ) → agent Lambda
  EventBridge (1 min, DISABLED by default; `python -m setuhaul.ops.pilot_mode wake`) → recovery, outbound,
  approval-relay Lambdas; Aurora Serverless v2 0–2 ACU with auto-pause after 5 idle minutes;  Step Functions approval state machine (published
  version + alias) → notify / record-token / expire Lambdas;  Aurora PostgreSQL 18 Serverless v2 with the Data API in an
  isolated VPC (no NAT; Lambdas stay outside the VPC);  one Secrets Manager login per database role;  Cognito (MFA);
  KMS; CloudWatch alarms.

The booking switch is set to "false" on every function and nothing in this stack can turn it on.
"""
from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (CfnOutput, Duration, RemovalPolicy, Stack, Tags, aws_apigateway as apigw,
                     aws_cloudwatch as cw, aws_cloudwatch_actions as cw_actions, aws_cognito as cognito, aws_ec2 as ec2,
                     aws_events as events, aws_events_targets as targets, aws_iam as iam, aws_kms as kms,
                     aws_lambda as lambda_, aws_lambda_event_sources as sources, aws_logs as logs, aws_rds as rds,
                     aws_secretsmanager as sm, aws_sns as sns, aws_sns_subscriptions as subs, aws_sqs as sqs,
                     aws_stepfunctions as sfn)
from constructs import Construct

def cdk_arn_format():
    from aws_cdk import ArnFormat
    return ArnFormat.COLON_RESOURCE_NAME


ROOT = Path(__file__).resolve().parents[1]
ASL = ROOT / "setuhaul" / "workflow" / "approval_workflow.asl.json"
REGION = "ap-southeast-2"
LEASE_S = 120                                      # orchestrator processing lease; queue visibility >= 2x
AUTO_PAUSE_S = 300                                 # Aurora pauses after 5 idle minutes (0 ACU)
# The one-minute schedules, by function; they are created DISABLED so an idle pilot never touches the database
# (otherwise Aurora would never reach 0 ACU). `setuhaul.ops.pilot_mode` enables them for a test session.
SCHEDULED = ("recovery", "outbound", "approval-relay")


def schedule_rule_name(environment_name: str, fn_name: str) -> str:
    return f"setuhaul-{environment_name}-{fn_name}-every-minute"
DB_ROLES = ("agent", "console", "ingest", "feasibility", "system")
# Database logins per workload: proven minimal by tests/test_aws.py (each handler runs with only these).
WORKLOAD_ROLES = {
    "api": ("ingest", "console"),                            # /tools is off in staging and pilot (review IAM-02)
    "agent": ("agent", "feasibility", "ingest"),
    "recovery": ("system", "ingest"),
    "outbound": ("system",),
    "approval": ("system",),                                 # relay, reconciler and the three state-machine tasks
}


class SetuHaulPilotStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, lambda_asset: str, environment_name: str = "pilot",
                 whatsapp_phone_number_id: str = "", alarm_email: str = "", llm_daily_budget_usd: str = "0.25",
                 approved_templates: str = "[]", **kw) -> None:
        super().__init__(scope, cid, **kw)
        env_label = f"{environment_name}-{REGION}"
        for k, v in {"Project": "SetuHaul", "Environment": environment_name, "DataClass": "synthetic"}.items():
            Tags.of(self).add(k, v)

        key = kms.Key(self, "Key", enable_key_rotation=True, alias=f"alias/setuhaul-{environment_name}",
                      removal_policy=RemovalPolicy.RETAIN)

        # ------------------------------------------------------------ database (AURORA gate)
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=0, subnet_configuration=[
            ec2.SubnetConfiguration(name="db", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)])
        engine = rds.DatabaseClusterEngine.aurora_postgres(version=rds.AuroraPostgresEngineVersion.VER_18_3)
        params = rds.ParameterGroup(self, "DbParams", engine=engine, parameters={
            "shared_preload_libraries": "pgaudit",
            "pgaudit.log": "ddl,role",                   # who changed schema or roles; never statement data
            "pgaudit.log_parameter": "0",
            "rds.force_ssl": "1",
            "standard_conforming_strings": "1"})        # the Data API backend's literal escaping relies on it
        self.cluster = cluster = rds.DatabaseCluster(
            self, "Db", engine=engine, vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            writer=rds.ClusterInstance.serverless_v2("writer"), serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=2, serverless_v2_auto_pause_duration=Duration.seconds(AUTO_PAUSE_S),
            enable_data_api=True, default_database_name="setuhaul",
            credentials=rds.Credentials.from_generated_secret("setuhaul_owner", encryption_key=key,
                                                              secret_name=f"setuhaul/{environment_name}/db/owner"),
            storage_encrypted=True, storage_encryption_key=key, parameter_group=params,
            backup=rds.BackupProps(retention=Duration.days(1)), deletion_protection=True,    # 1 day: the minimum
            removal_policy=RemovalPolicy.SNAPSHOT, cloudwatch_logs_exports=["postgresql"],
            cluster_identifier=f"setuhaul-{environment_name}")
        # The database log group is created here with its retention, instead of `cloudwatch_logs_retention`, which adds
        # a CDK-provided Lambda (LogRetention) whose code is not the verified artifact (review E5-03). It must exist
        # before the cluster starts exporting, or RDS creates it first and this resource fails.
        db_logs = logs.LogGroup(self, "DbLogs", log_group_name=f"/aws/rds/cluster/setuhaul-{environment_name}/postgresql",
                                retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.RETAIN)
        cluster.node.add_dependency(db_logs)

        # one login per application role, plus the pipeline's evidence writer (WORKLOAD_IDENTITY gate)
        self.login_secrets = {}
        for role in (*DB_ROLES, "deploy_evidence"):
            self.login_secrets[role] = sm.Secret(
                self, f"Login{role.title().replace('_', '')}", secret_name=f"setuhaul/{environment_name}/db/{role}",
                encryption_key=key, generate_secret_string=sm.SecretStringGenerator(
                    secret_string_template=json.dumps({"username": f"setuhaul_login_{role}", "engine": "postgres",
                                                       "dbname": "setuhaul"}),
                    generate_string_key="password", exclude_punctuation=True, password_length=40))
        app_secret = sm.Secret(self, "AppSecret", secret_name=f"setuhaul/{environment_name}/app", encryption_key=key,
                               description="SESSION_SECRET, META_APP_SECRET (owner fills the second)",
                               generate_secret_string=sm.SecretStringGenerator(
                                   secret_string_template=json.dumps({"META_APP_SECRET": ""}),
                                   generate_string_key="SESSION_SECRET", exclude_punctuation=True, password_length=48))
        # The WhatsApp sending token has its own secret, readable by the outbound function only (review IAM-01).
        whatsapp_secret = sm.Secret(self, "WhatsAppToken", secret_name=f"setuhaul/{environment_name}/whatsapp",
                                    encryption_key=key, description="WHATSAPP_TOKEN (owner sets the value)")
        openrouter = sm.Secret(self, "OpenRouterKey", secret_name=f"setuhaul/{environment_name}/openrouter",
                               encryption_key=key, description="OpenRouter API key (owner sets the value)")

        # ------------------------------------------------------------ queue (AWS_QUEUE gate)
        dlq = sqs.Queue(self, "InboundDlq", queue_name="setuhaul-inbound-dlq.fifo", fifo=True,
                        retention_period=Duration.days(14), encryption=sqs.QueueEncryption.KMS,
                        encryption_master_key=key, enforce_ssl=True)
        queue = sqs.Queue(self, "Inbound", queue_name="setuhaul-inbound.fifo", fifo=True,
                          content_based_deduplication=False,      # the sender gives a content-independent id
                          visibility_timeout=Duration.seconds(LEASE_S * 2 + 60), retention_period=Duration.days(4),
                          dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
                          encryption=sqs.QueueEncryption.KMS, encryption_master_key=key, enforce_ssl=True)

        # ------------------------------------------------------------ identity
        pool = cognito.UserPool(self, "Staff", user_pool_name=f"setuhaul-{environment_name}-staff",
                                self_sign_up_enabled=False, mfa=cognito.Mfa.REQUIRED,
                                mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
                                password_policy=cognito.PasswordPolicy(min_length=12, require_symbols=True),
                                account_recovery=cognito.AccountRecovery.EMAIL_ONLY, removal_policy=RemovalPolicy.RETAIN)
        for group in ("operations", "planner", "ops_lead"):
            cognito.CfnUserPoolGroup(self, f"Group{group.title().replace('_', '')}", user_pool_id=pool.user_pool_id,
                                     group_name=group)
        client = pool.add_client("Console", auth_flows=cognito.AuthFlow(user_srp=True), generate_secret=False)

        # ------------------------------------------------------------ functions
        if not (lambda_asset.endswith(".zip") and Path(lambda_asset).is_file()):
            # A folder would be zipped by CDK itself, and its bytes would not be the artifact the manifest records.
            raise ValueError(f"lambda_asset must be the built zip file (scripts/build_lambda.py), got {lambda_asset!r}")
        code = lambda_.Code.from_asset(lambda_asset)
        base_env = {
            "SETUHAUL_ENV": environment_name, "SETUHAUL_ENVIRONMENT": env_label, "SETUHAUL_DATA_CLASS": "synthetic",
            "SETUHAUL_BOOKING_ENABLED": "false", "SETUHAUL_DSN": "rds-data", "SETUHAUL_DB_CLUSTER_ARN": cluster.cluster_arn,
            "SETUHAUL_DB_NAME": "setuhaul", "SETUHAUL_QUEUE_URL": queue.queue_url,
            "SETUHAUL_APP_SECRET_ID": app_secret.secret_arn, "SETUHAUL_AWS_REGION": REGION,
            "COGNITO_ISSUER": f"https://cognito-idp.{REGION}.amazonaws.com/{pool.user_pool_id}",
            "COGNITO_CLIENT_IDS": client.user_pool_client_id, "FACILITIES": "FAC-JAI-01,FAC-GGN-01",
            "SETUHAUL_PROCESSING_LEASE_S": str(LEASE_S),
            "SETUHAUL_DB_RESUME_WAIT_S": "40"}           # wait for a paused Aurora to resume (the api overrides it)
        self.functions: dict[str, lambda_.Function] = {}

        def fn(name: str, handler: str, workload: str, timeout_s: int = 30, memory: int = 512, extra=None):
            env = {**base_env, **(extra or {}), "SETUHAUL_DB_SECRET_ARNS": json.dumps(
                {r: self.login_secrets[r].secret_arn for r in WORKLOAD_ROLES[workload]})}
            f = lambda_.Function(self, f"Fn{name.title().replace('-', '')}", function_name=f"setuhaul-{environment_name}-{name}",
                                 runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.X86_64,
                                 handler=f"setuhaul.aws.lambdas.{handler}", code=code, memory_size=memory,
                                 timeout=Duration.seconds(timeout_s), environment=env,
                                 tracing=lambda_.Tracing.ACTIVE,
                                 log_group=logs.LogGroup(self, f"Logs{name.title().replace('-', '')}",
                                                         retention=logs.RetentionDays.ONE_WEEK,
                                                         removal_policy=RemovalPolicy.DESTROY))
            f.add_to_role_policy(iam.PolicyStatement(
                actions=["rds-data:BeginTransaction", "rds-data:ExecuteStatement", "rds-data:CommitTransaction",
                         "rds-data:RollbackTransaction"], resources=[cluster.cluster_arn]))
            for r in WORKLOAD_ROLES[workload]:
                self.login_secrets[r].grant_read(f)
            app_secret.grant_read(f)
            self.functions[name] = f
            return f

        api_fn = fn("api", "api_handler", "api", timeout_s=29, extra={"SETUHAUL_TOOLS_ROUTE": "off",
                    "SETUHAUL_DB_RESUME_WAIT_S": "20"})     # API Gateway ends a request at 29 s
        agent_fn = fn("agent", "agent_handler", "agent", timeout_s=LEASE_S + 60, memory=1024, extra={
            "SETUHAUL_MODEL_PROVIDER": "openrouter", "SETUHAUL_OPENROUTER_SECRET_ID": openrouter.secret_arn,
            "SETUHAUL_OR_EXTRACT_MODEL": "anthropic/claude-haiku-4.5", "SETUHAUL_OR_PROVIDER": "amazon-bedrock",
            "SETUHAUL_LLM_DAILY_BUDGET_USD": llm_daily_budget_usd})
        recovery_fn = fn("recovery", "recovery_handler", "recovery", timeout_s=60)
        outbound_fn = fn("outbound", "outbound_handler", "outbound", timeout_s=60,
                         extra={# synthetic pilot: the internal test sink; WhatsApp stays off (no Meta call)
                                "SETUHAUL_NOTIFICATION_MODE": "test_sink", "SETUHAUL_WHATSAPP_ENABLED": "false",
                                "SETUHAUL_TEST_SINK_FAIL_PHONES": "+99900000009999",
                                "SETUHAUL_WHATSAPP_PHONE_NUMBER_ID": whatsapp_phone_number_id,
                                "SETUHAUL_WHATSAPP_SECRET_ID": whatsapp_secret.secret_arn,
                                # names of templates Meta has approved, as "name:language"; none until E6 submission
                                "SETUHAUL_WA_APPROVED_TEMPLATES": approved_templates})
        notify_fn = fn("approval-notify", "notify_approvers_handler", "approval")
        token_fn = fn("approval-token", "record_task_token_handler", "approval")
        expire_fn = fn("approval-expire", "expire_approval_handler", "approval")

        openrouter.grant_read(agent_fn)
        whatsapp_secret.grant_read(outbound_fn)
        queue.grant_send_messages(api_fn)
        queue.grant_send_messages(recovery_fn)
        agent_fn.add_event_source(sources.SqsEventSource(queue, batch_size=1, report_batch_item_failures=True,
                                                         max_concurrency=5))
        agent_fn.add_to_role_policy(iam.PolicyStatement(actions=["sqs:ChangeMessageVisibility"],
                                                        resources=[queue.queue_arn]))

        # ------------------------------------------------------------ approval workflow (BOOKING_WORKFLOW gate)
        machine = sfn.StateMachine(
            self, "Approval", state_machine_name=f"setuhaul-{environment_name}-approval",
            definition_body=sfn.DefinitionBody.from_string(ASL.read_text(encoding="utf-8")),
            definition_substitutions={"NotifyApproversFunction": notify_fn.function_arn,
                                      "RecordTaskTokenFunction": token_fn.function_arn,
                                      "ExpireApprovalFunction": expire_fn.function_arn},
            tracing_enabled=True)
        machine.add_to_role_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[
            notify_fn.function_arn, token_fn.function_arn, expire_fn.function_arn]))
        version = sfn.CfnStateMachineVersion(self, "ApprovalVersion", state_machine_arn=machine.state_machine_arn,
                                             state_machine_revision_id=machine.state_machine_revision_id)
        alias = sfn.CfnStateMachineAlias(self, "ApprovalLive", name="live", routing_configuration=[
            sfn.CfnStateMachineAlias.RoutingConfigurationVersionProperty(state_machine_version_arn=version.attr_arn,
                                                                         weight=100)])
        relay_fn = fn("approval-relay", "approval_relay_handler", "approval", timeout_s=60,
                      extra={"SETUHAUL_APPROVAL_STATE_MACHINE_ARN": alias.attr_arn})
        relay_fn.add_to_role_policy(iam.PolicyStatement(actions=["states:StartExecution"], resources=[alias.attr_arn]))
        # The token task answers its own token; built from the fixed name, because a reference to the machine here
        # would be a cycle (the machine references this function).
        machine_arn = self.format_arn(service="states", resource="stateMachine",
                                      resource_name=f"setuhaul-{environment_name}-approval",
                                      arn_format=cdk_arn_format())
        for f in (relay_fn, token_fn):
            f.add_to_role_policy(iam.PolicyStatement(
                actions=["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"],
                resources=[machine_arn]))

        self.schedule_rules = {}
        for name in SCHEDULED:
            f = self.functions[name]
            self.schedule_rules[name] = events.Rule(
                self, f"Every{f.node.id}", rule_name=schedule_rule_name(environment_name, name),
                schedule=events.Schedule.rate(Duration.minutes(1)), targets=[targets.LambdaFunction(f)],
                enabled=False)

        # ------------------------------------------------------------ edge: REST API (no WAF in this synthetic pilot)
        # Kept: stage throttling, HMAC-signed webhooks, Cognito with MFA for staff, input validation. Throttling limits
        # volume only; it does not inspect requests the way the removed WAF rule groups did (09_LOW_COST_PILOT_PLAN.md).
        # cloud_watch_role=False: the region's API Gateway account setting already has a logging role.
        api = apigw.LambdaRestApi(self, "Api", handler=api_fn, proxy=True, cloud_watch_role=False,
                                  endpoint_types=[apigw.EndpointType.REGIONAL],
                                  deploy_options=apigw.StageOptions(stage_name="pilot", throttling_rate_limit=20,
                                                                    throttling_burst_limit=40, tracing_enabled=True))

        # ------------------------------------------------------------ alarms (operations guide §2)
        topic = sns.Topic(self, "Alarms", master_key=key)
        if alarm_email:
            topic.add_subscription(subs.EmailSubscription(alarm_email))
        action = cw_actions.SnsAction(topic)

        def alarm(name, metric, threshold, periods=1, description=None):
            a = cw.Alarm(self, name, metric=metric, threshold=threshold, evaluation_periods=periods,
                         alarm_description=description,
                         comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
            a.add_alarm_action(action)

        alarm("QueueOldestMessage", queue.metric_approximate_age_of_oldest_message(period=Duration.minutes(5)), 300)
        alarm("DlqNotEmpty", dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(5)), 0)
        alarm("Api5xx", api.metric_server_error(period=Duration.minutes(5)), 5, periods=2)
        # API Gateway publishes 4xx as one metric, so this alarm is named for what it counts (review E0-01): every
        # client error, of which refused authentication (401/403) is one kind.
        alarm("Api4xxClientErrors", api.metric_client_error(period=Duration.minutes(5)), 50, periods=2,
              description="More than 50 API 4xx responses in each of two 5-minute periods (all client errors, "
                          "including but not limited to 401/403)")
        alarm("ApprovalFailed", machine.metric_failed(period=Duration.minutes(5)), 0)
        alarm("ApprovalTimedOut", machine.metric_timed_out(period=Duration.minutes(5)), 0)
        for name, f in self.functions.items():
            alarm(f"Errors{f.node.id}", f.metric_errors(period=Duration.minutes(5)), 0)

        for k, v in {"ApiUrl": api.url, "QueueUrl": queue.queue_url, "ClusterArn": cluster.cluster_arn,
                     "OwnerSecretArn": cluster.secret.secret_arn, "ApprovalAliasArn": alias.attr_arn,
                     "ApprovalVersionArn": version.attr_arn,
                     "UserPoolId": pool.user_pool_id, "OpenRouterSecretArn": openrouter.secret_arn,
                     "AppSecretArn": app_secret.secret_arn, "WhatsAppSecretArn": whatsapp_secret.secret_arn,
                     "ScheduleRuleNames": ",".join(r.rule_name for r in self.schedule_rules.values()),
                     "LoginSecretArns": self.to_json_string({r: s.secret_arn for r, s in self.login_secrets.items()})}.items():
            CfnOutput(self, k, value=v)

