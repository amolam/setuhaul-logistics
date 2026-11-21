"""Build the deployment manifest from what the pipeline deploys (build item E5; DEPLOYMENT_GATES.md, S4-R3-02).

Inputs: the synthesised CloudFormation template, the stack outputs after `cdk deploy`, and the Lambda artifact. The
manifest names an immutable version for everything the service cannot inspect itself, so a new worker build, state
machine version, IAM policy or sender configuration is a new deployment id, and old evidence stops counting.

    python -m setuhaul.ops.manifest cdk.out outputs.json > manifest.json

The pipeline passes the deployed Cloud Assembly: the template and the Lambda artifact are read from it, and the
artifact is the staged asset the template's functions reference (setuhaul/ops/assembly.py), so the recorded
artifact_sha256 is the hash of the bytes AWS receives (review E5-03). The older three-argument form
(template, outputs, zip) is kept for local use.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from ..api.inbound import deployment_id_for, manifest_problems

ASL = Path(__file__).resolve().parents[1] / "workflow" / "approval_workflow.asl.json"
DELIVERY_POLICY_VERSION = "B-WF-3/0017"
GRAPH_API = "https://graph.facebook.com/v21.0"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def build(template: dict, outputs: dict, artifact: bytes, environment: str, role_mapping: dict) -> dict:
    res = template["Resources"]
    queue = next(r["Properties"] for r in res.values()
                 if r["Type"] == "AWS::SQS::Queue" and r["Properties"].get("QueueName") == "setuhaul-inbound.fifo")
    iam = {k: r for k, r in sorted(res.items()) if r["Type"] in ("AWS::IAM::Role", "AWS::IAM::Policy")}
    fns = {r["Properties"]["FunctionName"]: r["Properties"] for r in res.values()
           if r["Type"] == "AWS::Lambda::Function" and "FunctionName" in r["Properties"]}
    recovery = next(p for n, p in fns.items() if n.endswith("-recovery"))
    outbound = next(p for n, p in fns.items() if n.endswith("-outbound"))
    alias_arn = outputs["ApprovalAliasArn"]
    art = sha256_bytes(artifact)
    m = {
        "environment": environment,
        "gates": {
            "RECOVERY_WORKER": {
                "artifact_sha256": art, "schedule": "rate(1 minute)",
                "config_version": sha256_bytes(canonical({k: v for k, v in recovery.items() if k != "Code"}))[:16],
                "queue_visibility_timeout_s": queue["VisibilityTimeout"]},
            "BOOKING_WORKFLOW": {
                "state_machine_arn": alias_arn, "state_machine_version": outputs.get("ApprovalVersionArn", alias_arn),
                "definition_sha256": sha256_bytes(ASL.read_bytes())},
            "WORKLOAD_IDENTITY": {"iam_policies_sha256": sha256_bytes(canonical(iam)), "role_mapping": role_mapping},
            "OUTBOUND_DELIVERY": {
                "artifact_sha256": art,
                "provider_config_sha256": sha256_bytes(canonical({
                    "api": GRAPH_API, "phone_number_id": outbound["Environment"]["Variables"].get(
                        "SETUHAUL_WHATSAPP_PHONE_NUMBER_ID", ""),
                    # which Meta-approved templates the sender may use outside the 24-hour window (E6-01)
                    "approved_templates": outbound["Environment"]["Variables"].get("SETUHAUL_WA_APPROVED_TEMPLATES",
                                                                                   "[]")})),
                "delivery_policy_version": DELIVERY_POLICY_VERSION},
        },
    }
    problems = manifest_problems(m, environment)
    if problems:
        raise ValueError("manifest incomplete: " + "; ".join(problems))
    return m


def from_assembly(cdk_out: str | Path, stack: str = "SetuHaulPilot") -> tuple[dict, bytes]:
    """(template, artifact bytes) from a Cloud Assembly, after the assembly check passes."""
    from .assembly import verify
    summary, problems = verify(cdk_out, stack)
    if problems:
        raise ValueError("assembly check failed: " + "; ".join(problems))
    out = Path(cdk_out)
    tpl_file = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["artifacts"][stack]["properties"]
    template = json.loads((out / tpl_file.get("templateFile", f"{stack}.template.json")).read_text(encoding="utf-8"))
    return template, (out / summary["staged_asset"]).read_bytes()


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    if len(sys.argv) == 3:
        tpl, artifact = from_assembly(sys.argv[1])
    else:
        tpl, artifact = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), Path(sys.argv[3]).read_bytes()
    outs = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    outs = next(iter(outs.values())) if len(outs) == 1 and isinstance(next(iter(outs.values())), dict) else outs
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))
    from setuhaul_stack import WORKLOAD_ROLES  # noqa: E402
    manifest = build(tpl, outs, artifact, "pilot-ap-southeast-2", {w: list(r) for w, r in WORKLOAD_ROLES.items()})
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print("DEPLOYMENT_ID", deployment_id_for(manifest), file=sys.stderr)
