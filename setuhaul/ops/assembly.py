"""Credential-free check of a synthesised Cloud Assembly (review E5-01, E5-02, E5-03). No AWS call.

    python -m setuhaul.ops.assembly cdk.out --zip build/lambda.zip [--manifest manifest.json]

It follows the chain the deployment will use, from the files CDK wrote, and hashes the bytes CDK will upload:

  cdk.out/manifest.json        the stack artifact and its asset-manifest artifact
  <stack>.template.json        every AWS::Lambda::Function: its Code.S3Key
  <stack>.assets.json          that object key -> one file asset, packaging "file" (uploaded as is, not re-zipped)
  cdk.out/asset.<id>.zip       the staged file: SHA-256 of its bytes

and requires: the template exists; every Lambda function in the stack uses the one staged asset (so no function runs
code the check did not hash, such as a CDK-provided helper); that staged file exists and is a zip; its SHA-256 equals
`build/lambda.zip` when given, and every `artifact_sha256` in the deployment manifest when given. The asset id in the
file name is CDK's fingerprint, not the file's SHA-256, so it is never trusted in place of the bytes.

Exit status 0 and a JSON summary on stdout when everything agrees; otherwise 1 and the problems on stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

STACK = "SetuHaulPilot"
FUNCTIONS = 8                                  # api, agent, recovery, outbound, relay and the three approval tasks


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_sha256s(manifest: dict) -> list[str]:
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "artifact_sha256":
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(manifest)
    return found


def verify(cdk_out: str | Path, stack: str = STACK, zip_path: str | Path | None = None,
           manifest: dict | None = None, functions: int | None = FUNCTIONS) -> tuple[dict, list[str]]:
    out, problems = Path(cdk_out), []
    summary: dict = {"stack": stack}
    try:
        cloud = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return summary, [f"no readable Cloud Assembly manifest in {out} ({type(e).__name__})"]
    arts = cloud.get("artifacts", {})
    st = arts.get(stack)
    if not st or st.get("type") != "aws:cloudformation:stack":
        return summary, [f"stack {stack} is not in the assembly"]
    tpl_file = out / st.get("properties", {}).get("templateFile", f"{stack}.template.json")
    asset_arts = [arts[d] for d in st.get("dependencies", []) if arts.get(d, {}).get("type") == "cdk:asset-manifest"]
    if len(asset_arts) != 1:
        return summary, [f"stack {stack} has {len(asset_arts)} asset manifests, expected 1"]
    try:
        template = json.loads(tpl_file.read_text(encoding="utf-8"))
        assets = json.loads((out / asset_arts[0]["properties"]["file"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as e:
        return summary, [f"template or asset manifest unreadable ({type(e).__name__})"]
    summary["template_sha256"] = _sha256(tpl_file)

    fns = {lid: r["Properties"].get("Code", {}) for lid, r in template.get("Resources", {}).items()
           if r.get("Type") == "AWS::Lambda::Function"}
    summary["functions"] = len(fns)
    if functions is not None and len(fns) != functions:
        problems.append(f"{len(fns)} Lambda functions in the template, expected {functions}")
    if any(r.get("Type", "").startswith("Custom::") or r.get("Type") == "AWS::CloudFormation::CustomResource"
           for r in template.get("Resources", {}).values()):
        problems.append("the template has a custom resource (a Lambda whose code this check does not cover)")
    keys = {json.dumps(c.get("S3Key"), sort_keys=True) for c in fns.values()}
    if len(keys) != 1:
        problems.append(f"Lambda functions use {len(keys)} different code objects, expected exactly 1")
        return summary, problems
    key = json.loads(keys.pop())
    if not isinstance(key, str):
        problems.append(f"Lambda code key is not a fixed object key: {key!r}")
        return summary, problems
    summary["asset_object_key"] = key

    entries = [(aid, a) for aid, a in assets.get("files", {}).items()
               if any(d.get("objectKey") == key for d in a.get("destinations", {}).values())]
    if len(entries) != 1:
        problems.append(f"{len(entries)} file assets publish {key}, expected 1")
        return summary, problems
    aid, entry = entries[0]
    src = entry.get("source", {})
    if src.get("packaging") != "file":
        problems.append(f"asset {aid} is packaged as {src.get('packaging')!r}: CDK would re-zip it at publish time")
    staged = out / src.get("path", "")
    summary["staged_asset"] = staged.name
    if not staged.is_file():
        problems.append(f"staged asset {staged.name} is missing from the assembly")
        return summary, problems
    if not zipfile.is_zipfile(staged):
        problems.append(f"staged asset {staged.name} is not a zip file")
    sha = _sha256(staged)
    summary["sha256"] = sha
    if zip_path is not None:
        built = _sha256(Path(zip_path))
        summary["built_zip_sha256"] = built
        if built != sha:
            problems.append(f"built zip {built[:12]}… differs from the staged asset {sha[:12]}…")
    if manifest is not None:
        recorded = _artifact_sha256s(manifest)
        if not recorded:
            problems.append("the deployment manifest records no artifact_sha256")
        bad = sorted({r for r in recorded if r != sha})
        if bad:
            problems.append(f"the deployment manifest records {', '.join(b[:12] + '…' for b in bad)}, "
                            f"not the staged asset {sha[:12]}…")
    return summary, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cdk_out")
    ap.add_argument("--stack", default=STACK)
    ap.add_argument("--zip", dest="zip_path")
    ap.add_argument("--manifest")
    ap.add_argument("--functions", type=int, default=FUNCTIONS)
    a = ap.parse_args(argv)
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8")) if a.manifest else None
    summary, problems = verify(a.cdk_out, a.stack, a.zip_path, manifest, a.functions)
    if problems:
        for p in problems:
            print(f"ASSEMBLY: {p}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
