"""CDK app for the SetuHaul Sydney pilot (E0). Synthesise only unless the owner has approved a deployment:

    python scripts/build_lambda.py           # build/lambda.zip, the one Lambda artifact
    python infra/app.py                      # writes the Cloud Assembly to cdk.out/ (or $CDK_OUTDIR); no AWS call
    python -m setuhaul.ops.assembly cdk.out  # credential-free check: template, asset manifest, staged zip agree
    cdk deploy --app cdk.out                 # owner, after approval: deploys that assembly, never a fresh synth

The output directory is explicit. Without it, a CDK app run outside the CDK CLI writes to a temporary directory
(review E5-01), so `cdk.out/` would not exist for the pipeline to upload.

Context (cdk.json or -c): lambda_asset (default build/lambda.zip; must be a zip file, so CDK stages it byte for byte),
whatsapp_phone_number_id, alarm_email, llm_daily_budget_usd (default 0.25), whatsapp_approved_templates (JSON list of
"name:language" that Meta has approved; default none).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setuhaul_stack import REGION, SetuHaulPilotStack  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
app = cdk.App(outdir=os.environ.get("CDK_OUTDIR") or str(ROOT / "cdk.out"))
ctx = app.node.try_get_context
SetuHaulPilotStack(
    app, "SetuHaulPilot",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION),
    lambda_asset=ctx("lambda_asset") or str(ROOT / "build" / "lambda.zip"),
    whatsapp_phone_number_id=ctx("whatsapp_phone_number_id") or "",
    alarm_email=ctx("alarm_email") or "",
    llm_daily_budget_usd=ctx("llm_daily_budget_usd") or "0.25",
    approved_templates=ctx("whatsapp_approved_templates") or "[]")
app.synth()
