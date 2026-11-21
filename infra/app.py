"""CDK app for the SetuHaul Sydney pilot (E0). Synthesise only unless the owner has approved a deployment:

    python infra/app.py                      # writes cdk.out/ (CloudFormation), no AWS call
    cdk diff / cdk deploy                    # owner, signed in to the Sydney account, after approval

Context (cdk.json or -c): lambda_asset (default build/lambda, made by scripts/build_lambda.py),
whatsapp_phone_number_id, alarm_email, llm_daily_budget_usd (default 0.25).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setuhaul_stack import REGION, SetuHaulPilotStack  # noqa: E402

app = cdk.App()
ctx = app.node.try_get_context
SetuHaulPilotStack(
    app, "SetuHaulPilot",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
    lambda_asset=ctx("lambda_asset") or str(Path(__file__).resolve().parents[1] / "build" / "lambda"),
    whatsapp_phone_number_id=ctx("whatsapp_phone_number_id") or "",
    alarm_email=ctx("alarm_email") or "",
    llm_daily_budget_usd=ctx("llm_daily_budget_usd") or "0.25")
app.synth()
