"""Credential-free check of a synthesised Cloud Assembly (review E5-01, E5-02, E5-03). No AWS call.

    python -m setuhaul.ops.assembly cdk.out --zip build/lambda.zip [--manifest manifest.json]

It follows the chain the deployment will use, from the files CDK wrote, and hashes the bytes CDK will upload:

  cdk.out/manifest.json        the stack artifact and its asset-manifest artifact
  <stack>.template.json        every AWS::Lambda::Function: its Code (S3Bucket, S3Key) pair
  <stack>.assets.json          that pair -> one file-asset destination with the same bucketName AND objectKey,
                               packaging "file" (uploaded as is, not re-zipped)
  cdk.out/asset.<id>.zip       the staged file: SHA-256 of its bytes

and requires: the template exists; every Lambda function in the stack, each checked on its own, names a bucket and a
key that both match one destination of the same staged asset (review E5-07: a function pointing at another bucket
with the same key is refused), and all functions resolve to that one asset (so no function runs code the check did not
hash, such as a CDK-provided helper); that staged file exists and is a zip; its SHA-256 equals
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


def _bucket(value) -> str | None:
    """The bucket expression as the asset manifest writes it: a literal name, or a single Fn::Sub of a string (CDK's
    `cdk-...-assets-${AWS::AccountId}-<region>`). Anything else (Ref, Fn::Join, Fn::Sub with variables, ...) is None."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and list(value) == ["Fn::Sub"] and isinstance(value["Fn::Sub"], str) and value["Fn::Sub"]:
        return value["Fn::Sub"]
    return None


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
    if not fns:
        problems.append("the template has no Lambda functions")
        return summary, problems

    # Each function on its own: its (bucket, key) pair must match one destination of one file asset exactly.
    resolved: dict[str, str] = {}                              # function logical id -> asset id
    for lid, code in sorted(fns.items()):
        if not isinstance(code, dict) or set(code) != {"S3Bucket", "S3Key"}:
            problems.append(f"{lid}: Code must be exactly S3Bucket and S3Key, got {sorted(code) if isinstance(code, dict) else code!r}")
            continue
        bucket, key = _bucket(code["S3Bucket"]), code["S3Key"]
        if bucket is None:
            problems.append(f"{lid}: S3Bucket is not a fixed bucket: {code['S3Bucket']!r}")
            continue
        if not isinstance(key, str) or not key:
            problems.append(f"{lid}: S3Key is not a fixed object key: {key!r}")
            continue
        hits = [aid for aid, a in assets.get("files", {}).items()
                for d in a.get("destinations", {}).values()
                if d.get("objectKey") == key and d.get("bucketName") == bucket]
        if len(hits) != 1:
            same_key = [aid for aid, a in assets.get("files", {}).items()
                        if any(d.get("objectKey") == key for d in a.get("destinations", {}).values())]
            why = "its bucket matches no destination of that key" if same_key else "no asset publishes that key"
            problems.append(f"{lid}: ({bucket}, {key}) matches {len(hits)} asset destinations, expected 1 ({why})")
            continue
        resolved[lid] = hits[0]
    summary["functions_checked"] = sorted(fns)
    if problems:
        return summary, problems
    if len(set(resolved.values())) != 1:
        problems.append(f"Lambda functions use {len(set(resolved.values()))} different code objects, expected exactly 1")
        return summary, problems
    aid = next(iter(resolved.values()))
    entry = assets["files"][aid]
    code0 = fns[sorted(fns)[0]]
    summary["asset_object_key"], summary["asset_bucket"] = code0["S3Key"], _bucket(code0["S3Bucket"])
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
