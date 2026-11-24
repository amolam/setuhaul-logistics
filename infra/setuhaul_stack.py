"""SetuHaul Sydney pilot infrastructure (build item E0). AWS CDK v2, Python. Region ap-southeast-2 only.

Not deployed. `python infra/app.py` writes the Cloud Assembly to `cdk.out/`; `tests/test_infra.py` asserts the
properties the six deployment gates depend on, and `setuhaul/ops/assembly.py` checks that every function's code is the
one staged `build/lambda.zip`. Deploying needs the owner's approval and sign-in, and deploys that assembly as built.

Shape (docs/release/04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md §2; low-cost choices in docs/release/09_LOW_COST_PILOT_PLAN.md):
  REST API (throttling; no WAF in this synthetic pilot) → api Lambda → SQS FIFO (+ FIFO DLQ) → agent Lambda
  EventBridge (1 min, DISABLED by default; `python -m setuhaul.ops.pilot_mode wake`) → recovery, outbound,
  approval-relay Lambdas; Aurora Serverless v2 0–2 ACU with auto-pause after 5 idle minutes;  Step Functions approval state machine (published
  version + alias) → notify / record-token / expire Lambdas;  Aurora PostgreSQL 17 with express configuration (Free plan) and the
  Data API, private in an isolated VPC (no NAT, no internet access gateway; only the db-bootstrap function is in the
  VPC, application Lambdas stay outside);  one Secrets Manager login per database role;  Cognito (MFA);
  KMS; CloudWatch alarms.

The booking switch is set to "false" on every function and nothing in this stack can turn it on.
"""
from __future__ import annotations

import hashlib
import re

import json
from pathlib import Path

from aws_cdk import (CfnOutput, CustomResource, Duration, RemovalPolicy, SecretValue, Stack, Tags, aws_apigateway as apigw,
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
BOOKING_APPROVAL = re.compile(r"OWNER-APPROVAL-20\d{6}-[A-Za-z0-9-]{3,40}")
LEASE_S = 120                                      # orchestrator processing lease; queue visibility >= 2x
SYDNEY_AZS = ("ap-southeast-2a", "ap-southeast-2b", "ap-southeast-2c")
DB_PARAMETER_FAMILY = "aurora-postgresql17"         # express configuration creates the Region default (17.9)
DB_SETUP_FUNCTIONS = ("db-express", "db-bootstrap")  # deploy-time only; never handle application traffic
FACILITY_IDS = ("FAC-JAI-01", "FAC-GGN-01")                    # the synthetic facilities this pilot serves
PLATFORM_FUNCTIONS = DB_SETUP_FUNCTIONS + ("cognito-pretoken",)   # not application workloads (no database login)
COGNITO_DOMAIN_PREFIX = "setuhaul-{env}-console"                  # https://<prefix>.auth.<region>.amazoncognito.com
AUTO_PAUSE_S = 300                                 # Aurora pauses after 5 idle minutes (0 ACU)
# The one-minute schedules, by function; they are created DISABLED so an idle pilot never touches the database
# (otherwise Aurora would never reach 0 ACU). `setuhaul.ops.pilot_mode` enables them for a test session.
SCHEDULED = ("recovery", "outbound", "approval-relay")


def schedule_rule_name(environment_name: str, fn_name: str) -> str:
    return f"setuhaul-{environment_name}-{fn_name}-every-minute"
DB_ROLES = ("agent", "console", "ingest", "feasibility", "system")
# M2 (0032): the only roles that make booking commitments, and the only functions holding their logins
RELEASE_DB_ROLES = ("agent", "console")
RELEASE_FUNCTIONS = ("api", "agent")


def release_login(role: str, release: str) -> str:
    """The database login of a role for one release (M2): per release for the booking roles, fixed for the others."""
    return f"setuhaul_login_{role}_{release}" if role in RELEASE_DB_ROLES else f"setuhaul_login_{role}"
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
                 approved_templates: str = "[]", booking_approval: str = "", langsmith_tracing: str = "",
                 langsmith_ui_url: str = "", **kw) -> None:
        super().__init__(scope, cid, **kw)
        # G.1: LangSmith Cloud tracing is opt-in (context langsmith_tracing=on). Off (the default): no LangSmith secret,
        # no permission, no environment variable; the template is exactly what it was before G.1.
        self.langsmith = (langsmith_tracing or "").strip().lower() in ("on", "true", "1")
        if langsmith_ui_url and not langsmith_ui_url.startswith(("https://smith.langchain.com/", "https://eu.smith.langchain.com/")):
            raise ValueError("langsmith_ui_url must be a https://[eu.]smith.langchain.com/ project page")
        # Option D switch. Off unless the owner's written approval reference is passed as context (booking_approval);
        # on, the workload functions still refuse to start until all six Step 6 gates have current evidence for this
        # deployment (setuhaul/api/inbound.py check_booking_preconditions). Setup and Cognito functions never book.
        booking_approval = (booking_approval or "").strip()
        if booking_approval and not BOOKING_APPROVAL.fullmatch(booking_approval):
            raise ValueError("booking_approval must look like OWNER-APPROVAL-YYYYMMDD-<ref> (the owner's written approval)")
        self.booking_enabled = bool(booking_approval)
        # M2 (0032): the release identity = the first 12 hex of the Lambda artifact's SHA-256 (the manifest's
        # artifact_sha256 and the functions' CodeSha256). The two booking roles' logins and the api's and agent's
        # execution roles are named after it, so each release gets a new database identity and a new IAM role.
        asset = Path(lambda_asset)
        self.release = hashlib.sha256(asset.read_bytes()).hexdigest()[:12] if asset.is_file() else "0" * 12
        env_label = f"{environment_name}-{REGION}"
        for k, v in {"Project": "SetuHaul", "Environment": environment_name, "DataClass": "synthetic"}.items():
            Tags.of(self).add(k, v)

        key = kms.Key(self, "Key", enable_key_rotation=True, alias=f"alias/setuhaul-{environment_name}",
                      removal_policy=RemovalPolicy.RETAIN)

        # ------------------------------------------------------------ database (AURORA gate)
        # (the database is created further down, once the verified Lambda code is known: express_aurora)

        # one login per application role, plus the pipeline's evidence writer (WORKLOAD_IDENTITY gate)
        self.login_secrets = {}
        for role in (*DB_ROLES, "deploy_evidence"):
            self.login_secrets[role] = sm.Secret(
                self, f"Login{role.title().replace('_', '')}", secret_name=f"setuhaul/{environment_name}/db/{role}",
                encryption_key=key, generate_secret_string=sm.SecretStringGenerator(
                    secret_string_template=json.dumps({"username": release_login(role, self.release), "engine": "postgres",
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

        # ------------------------------------------------------------ API (created first: the console's URL is on it)
        # One REST API serves the staff console's page under /app/ and its data under /console/: one HTTPS origin, so
        # no CORS (docs/release/11_STAFF_CONSOLE.md). Throttling 20 rps / burst 40 caps abusive volume only; it does
        # not inspect requests the way the removed WAF rule groups did (09_LOW_COST_PILOT_PLAN.md).
        # cloud_watch_role=False: the region's API Gateway account setting already has a logging role.
        api = apigw.RestApi(self, "Api", cloud_watch_role=False, endpoint_types=[apigw.EndpointType.REGIONAL],
                            deploy_options=apigw.StageOptions(stage_name="pilot", throttling_rate_limit=20,
                                                              throttling_burst_limit=40, tracing_enabled=True))
        console_url = f"https://{api.rest_api_id}.execute-api.{REGION}.amazonaws.com/pilot/app/"
        cognito_domain = f"{COGNITO_DOMAIN_PREFIX.format(env=environment_name)}.auth.{REGION}.amazoncognito.com"

        # ------------------------------------------------------------ identity
        pool = cognito.UserPool(self, "Staff", user_pool_name=f"setuhaul-{environment_name}-staff",
                                self_sign_up_enabled=False, mfa=cognito.Mfa.REQUIRED,
                                mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
                                password_policy=cognito.PasswordPolicy(min_length=12, require_symbols=True),
                                account_recovery=cognito.AccountRecovery.EMAIL_ONLY, removal_policy=RemovalPolicy.RETAIN)
        for group in ("operations", "planner", "ops_lead"):
            cognito.CfnUserPoolGroup(self, f"Group{group.title().replace('_', '')}", user_pool_id=pool.user_pool_id,
                                     group_name=group)
        # Staff sign in through Cognito's hosted page (MFA as the pool requires): authorization code with PKCE, a
        # public client (no secret in the browser), redirects to the console's own URL only, 30-minute tokens.
        for fac in FACILITY_IDS:                 # facility scope comes from these admin-assigned groups (pretoken.py)
            cognito.CfnUserPoolGroup(self, f"GroupFacility{fac.replace('-', '')}", user_pool_id=pool.user_pool_id,
                                     group_name=f"facility-{fac}")
        self.cognito_domain = pool.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(
            domain_prefix=COGNITO_DOMAIN_PREFIX.format(env=environment_name)),
            managed_login_version=cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI)
        client = pool.add_client("Console", auth_flows=cognito.AuthFlow(user_srp=True), generate_secret=False,
                                 o_auth=cognito.OAuthSettings(
                                     flows=cognito.OAuthFlows(authorization_code_grant=True),
                                     scopes=[cognito.OAuthScope.OPENID], callback_urls=[console_url],
                                     logout_urls=[console_url]),
                                 supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
                                 prevent_user_existence_errors=True, access_token_validity=Duration.minutes(30),
                                 id_token_validity=Duration.minutes(30), refresh_token_validity=Duration.hours(8))
        self.console_client = client

        # ------------------------------------------------------------ functions
        if not (lambda_asset.endswith(".zip") and Path(lambda_asset).is_file()):
            # A folder would be zipped by CDK itself, and its bytes would not be the artifact the manifest records.
            raise ValueError(f"lambda_asset must be the built zip file (scripts/build_lambda.py), got {lambda_asset!r}")
        code = lambda_.Code.from_asset(lambda_asset)
        cluster_arn = self.express_aurora(environment_name, key, code)
        self.facility_scope_trigger(pool, environment_name, code)
        base_env = {
            "SETUHAUL_ENV": environment_name, "SETUHAUL_ENVIRONMENT": env_label, "SETUHAUL_DATA_CLASS": "synthetic",
            "SETUHAUL_BOOKING_ENABLED": "false", "SETUHAUL_DSN": "rds-data", "SETUHAUL_DB_CLUSTER_ARN": cluster_arn,
            "SETUHAUL_DB_NAME": "setuhaul", "SETUHAUL_QUEUE_URL": queue.queue_url,
            "SETUHAUL_APP_SECRET_ID": app_secret.secret_arn, "SETUHAUL_AWS_REGION": REGION,
            "COGNITO_ISSUER": f"https://cognito-idp.{REGION}.amazonaws.com/{pool.user_pool_id}",
            "COGNITO_CLIENT_IDS": client.user_pool_client_id, "FACILITIES": ",".join(FACILITY_IDS),
            "SETUHAUL_PROCESSING_LEASE_S": str(LEASE_S),
            "SETUHAUL_DB_RESUME_WAIT_S": "40"}           # wait for a paused Aurora to resume (the api overrides it)
        self.functions: dict[str, lambda_.Function] = {}

        def release_role(name: str) -> iam.Role:
            # M2: one execution role per release for the two booking functions. The logical id carries the release,
            # so CloudFormation creates the new role, moves the function to it and then deletes the old role and its
            # policy: an old container's credentials can no longer read the login secret or call the Data API.
            return iam.Role(self, f"Fn{name.title()}Role{self.release}",
                            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                                "service-role/AWSLambdaBasicExecutionRole")])

        def fn(name: str, handler: str, workload: str, timeout_s: int = 30, memory: int = 512, extra=None):
            env = {**base_env, **(extra or {}), "SETUHAUL_DB_SECRET_ARNS": json.dumps(
                {r: self.login_secrets[r].secret_arn for r in WORKLOAD_ROLES[workload]})}
            f = lambda_.Function(self, f"Fn{name.title().replace('-', '')}", function_name=f"setuhaul-{environment_name}-{name}",
                                 runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.X86_64,
                                 handler=f"setuhaul.aws.lambdas.{handler}", code=code, memory_size=memory,
                                 timeout=Duration.seconds(timeout_s), environment=env,
                                 role=release_role(name) if name in RELEASE_FUNCTIONS else None,
                                 tracing=lambda_.Tracing.ACTIVE,
                                 log_group=logs.LogGroup(self, f"Logs{name.title().replace('-', '')}",
                                                         retention=logs.RetentionDays.ONE_WEEK,
                                                         removal_policy=RemovalPolicy.DESTROY))
            f.add_to_role_policy(iam.PolicyStatement(
                actions=["rds-data:BeginTransaction", "rds-data:ExecuteStatement", "rds-data:CommitTransaction",
                         "rds-data:RollbackTransaction"], resources=[cluster_arn]))
            for r in WORKLOAD_ROLES[workload]:
                self.login_secrets[r].grant_read(f)
            app_secret.grant_read(f)
            self.functions[name] = f
            return f

        # Option D (0026): only the two functions that make booking commitments carry the switch: the api (console
        # moves and decisions) and the agent (booking tools). With it on they read the manifest the pipeline recorded
        # in the database and check it against their own code hash; every other function keeps it off, so the
        # recovery sweep, the sender and the approval tasks keep running even if the booking gates lapse.
        booking = ({"SETUHAUL_BOOKING_ENABLED": "true", "SETUHAUL_DEPLOYMENT_MANIFEST_SOURCE": "database"}
                   if self.booking_enabled else {})
        # G.1: the LangSmith key's secret is created with an explicit EMPTY placeholder ({"api_key": ""}), not a
        # generated password: an unset key then reads as EMPTY_SECRET (tracing off, logged), never as a key-shaped
        # string that the provider refuses (the OpenRouter lesson, docs/phase-g1/OPENROUTER_PILOT_DIAGNOSIS.md).
        langsmith_secret, ls_agent, ls_api = None, {}, {}
        if self.langsmith:
            langsmith_secret = sm.Secret(self, "LangSmithKey", secret_name=f"setuhaul/{environment_name}/langsmith",
                                         encryption_key=key, description="LangSmith API key (owner sets the value)",
                                         secret_string_value=SecretValue.unsafe_plain_text('{"api_key": ""}'))
            ls_agent = {"SETUHAUL_LANGSMITH_TRACING": "on", "SETUHAUL_LANGSMITH_SECRET_ID": langsmith_secret.secret_arn,
                        "SETUHAUL_LANGSMITH_PROJECT": f"setuhaul-synthetic-{environment_name}",
                        "SETUHAUL_TRACE_CONTENT": "off", "SETUHAUL_LANGSMITH_SAMPLE_RATE": "1.0",
                        "SETUHAUL_TRACE_FLUSH_S": "0.5"}
            ls_api = {"SETUHAUL_LANGSMITH_TRACING": "on"}           # links only: the api never reads the key
            if langsmith_ui_url:
                ls_agent["SETUHAUL_LANGSMITH_UI_URL"] = ls_api["SETUHAUL_LANGSMITH_UI_URL"] = langsmith_ui_url
        api_fn = fn("api", "api_handler", "api", timeout_s=29, extra={**booking, "SETUHAUL_TOOLS_ROUTE": "off",
                    "SETUHAUL_DB_RESUME_WAIT_S": "20",       # API Gateway ends a request at 29 s
                    "COGNITO_DOMAIN": cognito_domain,         # the console page's sign-in (public values only)
                    "COGNITO_CONSOLE_CLIENT_ID": client.user_pool_client_id, **ls_api})
        agent_fn = fn("agent", "agent_handler", "agent", timeout_s=LEASE_S + 60, memory=1024, extra={**booking,
            "SETUHAUL_MODEL_PROVIDER": "openrouter", "SETUHAUL_OPENROUTER_SECRET_ID": openrouter.secret_arn,
            "SETUHAUL_OR_EXTRACT_MODEL": "anthropic/claude-haiku-4.5", "SETUHAUL_OR_PROVIDER": "amazon-bedrock",
            "SETUHAUL_LLM_DAILY_BUDGET_USD": llm_daily_budget_usd, **ls_agent})
        if langsmith_secret is not None:
            langsmith_secret.grant_read(agent_fn)                   # the agent only (it exports the traces)
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
        if self.booking_enabled:
            # the start-up check (check_booking_preconditions): read-only. Each reads its own code hash (to match the
            # recorded manifest) and the dead-letter queue's settings (to match the AWS_QUEUE fingerprint).
            for name, f in (("api", api_fn), ("agent", agent_fn)):
                # by its fixed name: a reference to the function from its own policy would be a cycle
                own = self.format_arn(service="lambda", resource="function", resource_name=f"setuhaul-{environment_name}-{name}",
                                      arn_format=cdk_arn_format())
                f.add_to_role_policy(iam.PolicyStatement(actions=["lambda:GetFunctionConfiguration"], resources=[own]))
                f.add_to_role_policy(iam.PolicyStatement(actions=["sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
                                                         resources=[dlq.queue_arn, queue.queue_arn]))

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
        # Step 6 drill (23 Nov 2026): starting an execution THROUGH the alias is authorised against the state machine's
        # own ARN as well ("not authorized to perform: states:StartExecution on resource: ...stateMachine:<name>"), so
        # granting only the alias ARN denied every start. The relay still calls the alias, so the published version
        # runs; the machine ARN is the only other resource.
        relay_fn.add_to_role_policy(iam.PolicyStatement(actions=["states:StartExecution"],
                                                        resources=[alias.attr_arn, machine.state_machine_arn]))
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

        # ------------------------------------------------------------ edge: the REST API's routes (no WAF in this pilot)
        # Kept: stage throttling, HMAC-signed webhooks, Cognito with MFA for staff, input validation.
        integration = apigw.LambdaIntegration(api_fn)       # as LambdaRestApi(proxy=True): root and {proxy+}, ANY
        api.root.add_method("ANY", integration)
        api.root.add_proxy(default_integration=integration, any_method=True)

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

        for k, v in {"ApiUrl": api.url, "QueueUrl": queue.queue_url, "ClusterArn": cluster_arn,
                     "OwnerSecretArn": self.owner_secret.secret_arn, "ApprovalAliasArn": alias.attr_arn,
                     "ApprovalVersionArn": version.attr_arn,
                     "UserPoolId": pool.user_pool_id, "OpenRouterSecretArn": openrouter.secret_arn,
                     "AppSecretArn": app_secret.secret_arn, "WhatsAppSecretArn": whatsapp_secret.secret_arn,
                     "ScheduleRuleNames": ",".join(r.rule_name for r in self.schedule_rules.values()),
                     "LoginSecretArns": self.to_json_string({r: s.secret_arn for r, s in self.login_secrets.items()}),
                     "ConsoleUrl": console_url, "CognitoDomain": cognito_domain,
                     "ConsoleClientId": client.user_pool_client_id,
                     "BookingMode": f"ON ({booking_approval})" if self.booking_enabled else "OFF"}.items():
            CfnOutput(self, k, value=v)

    # ------------------------------------------------------------ database (AURORA gate), docs/release/10_EXPRESS_AURORA.md
    def express_aurora(self, environment_name: str, key, code) -> str:
        """Aurora PostgreSQL with express configuration (the only Aurora the AWS Free plan creates), private in an
        isolated VPC: VPC networking on, internet access gateway off, no NAT, no public instance. CloudFormation cannot
        ask for that, so a custom resource (setuhaul/aws/express_cluster.py, from the verified zip) makes the RDS call
        and fails closed unless the live cluster reads back private. Storage uses the AWS/RDS service-owned key
        (express configuration cannot use a customer key; accepted by the owner for this synthetic pilot)."""
        cluster_id = f"setuhaul-{environment_name}"
        # every Availability Zone in the Region: an express cluster's subnet group must cover all of them
        vpc = ec2.Vpc(self, "Vpc", availability_zones=list(SYDNEY_AZS), nat_gateways=0, subnet_configuration=[
            ec2.SubnetConfiguration(name="db", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)])
        isolated = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        db_sg = ec2.SecurityGroup(self, "DbSg", vpc=vpc, allow_all_outbound=False,
                                  description="Aurora: 5432 from the bootstrap function only")
        boot_sg = ec2.SecurityGroup(self, "DbBootstrapSg", vpc=vpc, allow_all_outbound=False,
                                    description="bootstrap function: 5432 to Aurora only")
        db_sg.add_ingress_rule(boot_sg, ec2.Port.tcp(5432), "bootstrap function")
        boot_sg.add_egress_rule(db_sg, ec2.Port.tcp(5432), "Aurora")
        subnets = rds.CfnDBSubnetGroup(self, "DbSubnets", db_subnet_group_name=cluster_id,
                                       db_subnet_group_description="SetuHaul Aurora, isolated subnets in every AZ",
                                       subnet_ids=isolated.subnet_ids)
        params = rds.CfnDBClusterParameterGroup(self, "DbParams", db_cluster_parameter_group_name=cluster_id,
                                                family=DB_PARAMETER_FAMILY, description="SetuHaul Aurora", parameters={
            "shared_preload_libraries": "pg_stat_statements,pgaudit",
            "pgaudit.log": "ddl,role",                   # who changed schema or roles; never statement data
            "pgaudit.log_parameter": "0",
            "rds.force_ssl": "1",
            "standard_conforming_strings": "1"})        # the Data API backend's literal escaping relies on it
        # The database log group is kept by the express resource, before the cluster exports to it: created if
        # missing, 7-day retention set, never deleted (one retained from an earlier deploy is adopted, not replaced);
        # no CDK-provided LogRetention Lambda (review E5-03).
        db_log_group = f"/aws/rds/cluster/{cluster_id}/postgresql"
        # The owner login replaces the master secret: the Data API refuses an express cluster's (IAM-only) master.
        self.owner_secret = sm.Secret(
            self, "DbOwner", secret_name=f"setuhaul/{environment_name}/db/owner", encryption_key=key,
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "setuhaul_owner", "engine": "postgres", "dbname": "setuhaul"}),
                generate_string_key="password", exclude_punctuation=True, password_length=40))
        setup_env = {"SETUHAUL_ENV": environment_name, "SETUHAUL_DATA_CLASS": "synthetic", "SETUHAUL_BOOKING_ENABLED": "false"}

        def setup_fn(name, handler, timeout_s, **kw):
            return lambda_.Function(self, f"Fn{name.title().replace('-', '')}", function_name=f"setuhaul-{environment_name}-{name}",
                                    runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.X86_64,
                                    handler=handler, code=code, memory_size=256, timeout=Duration.seconds(timeout_s),
                                    environment=setup_env,
                                    log_group=logs.LogGroup(self, f"Logs{name.title().replace('-', '')}",
                                                            retention=logs.RetentionDays.ONE_WEEK,
                                                            removal_policy=RemovalPolicy.DESTROY), **kw)
        boot = setup_fn("db-bootstrap", "setuhaul.aws.express_bootstrap.handler", 60, vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                        security_groups=[boot_sg])
        # IAM sign-in as the master only. The cluster's resource id is unknown until it exists; this account holds no
        # other Aurora cluster in the Region.
        boot.add_to_role_policy(iam.PolicyStatement(actions=["rds-db:connect"], resources=[
            self.format_arn(service="rds-db", resource="dbuser", resource_name="*/postgres", arn_format=cdk_arn_format())]))
        express_name = f"setuhaul-{environment_name}-db-express"
        express = setup_fn("db-express", "setuhaul.aws.express_cluster.handler", 900)

        def rds_arn(kind, name):
            return self.format_arn(service="rds", resource=kind, resource_name=name, arn_format=cdk_arn_format())
        express.add_to_role_policy(iam.PolicyStatement(actions=["rds:*"], resources=[
            rds_arn("cluster", cluster_id), rds_arn("db", f"{cluster_id}*"), rds_arn("subgrp", cluster_id),
            rds_arn("cluster-pg", cluster_id), rds_arn("cluster-pg", "default.*"), rds_arn("pg", "default.*")]))
        express.add_to_role_policy(iam.PolicyStatement(actions=[
            "rds:Describe*", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs",
            "ec2:DescribeAvailabilityZones", "logs:DescribeLogGroups"], resources=["*"]))   # list-only; no resource ARNs
        express.add_to_role_policy(iam.PolicyStatement(actions=["iam:CreateServiceLinkedRole"], resources=["*"],
                                                       conditions={"StringEquals": {"iam:AWSServiceName": "rds.amazonaws.com"}}))
        express.add_to_role_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[
            boot.function_arn, self.format_arn(service="lambda", resource="function", resource_name=express_name,
                                               arn_format=cdk_arn_format())]))
        self.owner_secret.grant_read(express)
        express.add_to_role_policy(iam.PolicyStatement(actions=["logs:CreateLogGroup", "logs:PutRetentionPolicy"],
                                                       resources=[
            self.format_arn(service="logs", resource="log-group", resource_name=f"{db_log_group}:*",
                            arn_format=cdk_arn_format()),
            self.format_arn(service="logs", resource="log-group", resource_name=db_log_group, arn_format=cdk_arn_format())]))
        db = CustomResource(self, "Db", service_token=express.function_arn, resource_type="Custom::SetuHaulExpressAurora",
                            properties={
                                "ClusterIdentifier": cluster_id, "SubnetGroupName": subnets.ref,
                                "SecurityGroupId": db_sg.security_group_id, "ParameterGroupName": params.ref,
                                "MinCapacity": "0", "MaxCapacity": "2", "SecondsUntilAutoPause": str(AUTO_PAUSE_S),
                                "BackupRetentionDays": "1",                                   # 1 day: the minimum
                                "OwnerSecretArn": self.owner_secret.secret_arn, "DatabaseName": "setuhaul",
                                "BootstrapFunction": boot.function_name,
                                "LogGroupName": db_log_group, "LogRetentionDays": "7",
                                "Tags": {"Project": "SetuHaul", "Environment": environment_name, "DataClass": "synthetic"}})
        db.node.add_dependency(boot)
        self.db_resource = db
        return db.get_att_string("ClusterArn")

    def facility_scope_trigger(self, pool, environment_name: str, code) -> None:
        """Cognito pre-token generation, event version V2_0 (Essentials tier): adds `facility_scope` to staff access
        tokens from admin-assigned facility-<ID> groups only (setuhaul/aws/pretoken.py). No database login."""
        name = "cognito-pretoken"
        f = lambda_.Function(self, "FnCognitoPretoken", function_name=f"setuhaul-{environment_name}-{name}",
                             runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.X86_64,
                             handler="setuhaul.aws.pretoken.handler", code=code, memory_size=128,
                             timeout=Duration.seconds(5),
                             environment={"SETUHAUL_ENV": environment_name, "SETUHAUL_DATA_CLASS": "synthetic",
                                          "SETUHAUL_BOOKING_ENABLED": "false", "FACILITIES": ",".join(FACILITY_IDS)},
                             log_group=logs.LogGroup(self, "LogsCognitoPretoken", retention=logs.RetentionDays.ONE_WEEK,
                                                     removal_policy=RemovalPolicy.DESTROY))
        pool.add_trigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG, f, cognito.LambdaVersion.V2_0)
