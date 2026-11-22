"""E5-08: the deployment job runs only from the release branch, still needs the typed confirmation, and the documented
AWS OIDC trust binds repository, environment and branch. Static checks of the workflow file: no credentials, no AWS,
no GitHub call. (YAML is read as text: the checks look for the exact lines that carry each rule.)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
POLICY = json.loads((ROOT / "infra" / "github_oidc_deploy_trust_policy.json").read_text(encoding="utf-8"))
RELEASE_REF = "refs/heads/release/fde-sydney-pilot"     # the one pilot release branch (03_GITHUB_RELEASE_CHECKLIST.md)
REPO = "amolam/setuhaul-logistics"


def _job(name: str) -> str:
    """The text of one job, from its key to the next job key."""
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)", CI, re.S | re.M)
    assert m, name
    return m.group(1)


DEPLOY = _job("deploy")


def _condition() -> str:
    lines = [l.strip() for l in DEPLOY.splitlines() if l.strip().startswith("if:")]
    assert len(lines) == 1, lines
    return lines[0].removeprefix("if:").strip()


def _evaluate(expr: str, ctx: dict) -> bool:
    """A minimal evaluator for the GitHub expression subset the condition uses: context names, quoted strings,
    ==, !=, &&, || and parentheses. Anything else fails the test (the condition must stay this simple)."""
    tokens = re.findall(r"\s*('[^']*'|==|!=|&&|\|\||\(|\)|[A-Za-z_][A-Za-z0-9_.]*)", expr)
    assert "".join(tokens).replace(" ", "") == expr.replace(" ", ""), f"unsupported syntax in {expr!r}"
    out = []
    for t in (t.strip() for t in tokens):
        if t.startswith("'"):
            out.append(repr(t[1:-1]))
        elif t in ("==", "!=", "(", ")"):
            out.append(t)
        elif t == "&&":
            out.append(" and ")
        elif t == "||":
            out.append(" or ")
        else:
            assert t in ctx, f"unknown context {t}"
            out.append(repr(ctx[t]))
    return bool(eval("".join(out), {"__builtins__": {}}, {}))          # only literals and operators remain


def _ctx(event="workflow_dispatch", deploy="pilot", ref=RELEASE_REF):
    return {"github.event_name": event, "github.event.inputs.deploy": deploy, "github.ref": ref}


def test_the_deploy_condition_has_the_branch_allow_list_and_the_typed_confirmation():
    cond = _condition()
    assert f"github.ref == '{RELEASE_REF}'" in cond
    assert "github.event.inputs.deploy == 'pilot'" in cond
    assert "github.event_name == 'workflow_dispatch'" in cond
    assert "||" not in cond                                            # no alternative path around the gates
    assert _evaluate(cond, _ctx()) is True


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/phase-e/aws-implementation-fixes",
                                 "refs/heads/feature/anything", "refs/heads/release/other",
                                 "refs/heads/release/fde-sydney-pilot-2", "refs/pull/7/merge",
                                 "refs/tags/v0.1.0-fde-pilot", ""])
def test_no_other_branch_tag_or_pull_request_ref_can_deploy(ref):
    assert _evaluate(_condition(), _ctx(ref=ref)) is False


@pytest.mark.parametrize("ctx", [_ctx(deploy=""), _ctx(deploy="Pilot"), _ctx(deploy="yes"),
                                 _ctx(event="push"), _ctx(event="pull_request")])
def test_the_typed_confirmation_and_manual_dispatch_are_still_required(ctx):
    assert _evaluate(_condition(), ctx) is False


def _guard_script() -> str:
    m = re.search(r"- name: Refuse any ref outside the deployment branch allow-list.*?run: \|\n(.*?)\n      - uses:",
                  DEPLOY, re.S)
    assert m, "the deploy job's first step must be the branch guard"
    return "\n".join(l[10:] for l in m.group(1).splitlines())


def test_the_branch_guard_is_the_first_step_before_checkout_and_sign_in():
    steps = [l.strip() for l in DEPLOY.splitlines() if l.strip().startswith("- ")]
    assert steps[0].startswith("- name: Refuse any ref outside the deployment branch allow-list")
    first_uses = next(i for i, s in enumerate(steps) if s.startswith("- uses:"))
    sign_in = next(i for i, s in enumerate(steps) if "configure-aws-credentials" in s)
    assert 0 < first_uses < sign_in
    assert "environment: pilot" in DEPLOY and "id-token: write" in DEPLOY


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
@pytest.mark.parametrize("ref,confirm,ok", [(RELEASE_REF, "pilot", True), ("refs/heads/main", "pilot", False),
                                            ("refs/heads/feature/x", "pilot", False), ("refs/pull/7/merge", "pilot", False),
                                            (RELEASE_REF, "", False)])
def test_the_branch_guard_script_refuses_what_the_condition_refuses(ref, confirm, ok):
    script = _guard_script()
    r = subprocess.run([shutil.which("bash"), "-c", script], env={"DEPLOY_REF": ref, "DEPLOY_CONFIRM": confirm,
                                                                  "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=30)
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert "DEPLOY REFUSED" in r.stderr


def test_the_documented_oidc_trust_binds_repository_environment_and_branch():
    (stmt,) = POLICY["Statement"]
    assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert stmt["Principal"]["Federated"].endswith(":oidc-provider/token.actions.githubusercontent.com")
    assert "<ACCOUNT_ID>" in stmt["Principal"]["Federated"]          # a template: no account id in the repository
    cond = stmt["Condition"]
    assert set(cond) == {"StringEquals"}                              # no StringLike: no wildcard can widen it
    eq = cond["StringEquals"]
    assert eq["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    sub = eq["token.actions.githubusercontent.com:sub"]
    assert sub == f"repo:{REPO}:environment:pilot:ref:{RELEASE_REF}"
    assert "*" not in json.dumps(cond)


def test_an_environment_only_subject_is_not_accepted_as_branch_identity():
    sub = POLICY["Statement"][0]["Condition"]["StringEquals"]["token.actions.githubusercontent.com:sub"]
    assert sub != f"repo:{REPO}:environment:pilot"                     # the default subject names no branch
    assert ":ref:" + RELEASE_REF in sub
    note = " ".join(POLICY["_comment"])
    assert "names no branch" in note and "include_claim_keys" in note and "NOT verified in AWS" in note
    plan = (ROOT / "docs" / "release" / "04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md").read_text(encoding="utf-8")
    assert sub in plan and "external gate" in plan
