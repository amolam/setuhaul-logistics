"""Create or update one database login per application role (build item E5; WORKLOAD_IDENTITY gate).

Run by the pipeline after migrations, with the owner secret, over the Data API. For each role it reads the login's
Secrets Manager secret ({"username", "password"}), then creates the login (or resets its password) and makes it a
member of **exactly one** application role. Membership in any other application role is revoked. A login cannot
create roles or databases.

The password has to appear in the statement (CREATE ROLE takes no parameters). Two things keep it out of logs:
`pgaudit.log` is switched off for this transaction only, and the statement is never logged by this code.

    python -m setuhaul.aws.provision_logins     # env: SETUHAUL_DB_CLUSTER_ARN, SETUHAUL_DB_OWNER_SECRET_ARN,
                                                #      SETUHAUL_DB_SECRET_ARNS (all roles + deploy_evidence)
"""
from __future__ import annotations

import json
import os
import re

from ..api.db import ROLES
from .data_api import literal
from .migrate_data_api import OwnerSession

APP_ROLES = {**ROLES, "deploy_evidence": "setuhaul_deploy_evidence"}
_LOGIN = re.compile(r"^setuhaul_login_[a-z_]{2,40}(_[0-9a-f]{12})?$|^test_login_[a-z0-9_]{2,60}$")
# M2 (0032): the booking roles get one login per release, setuhaul_login_<role>_<first 12 hex of the artifact SHA-256>
# (the stack writes that name into the role's secret). Provisioning one makes every OTHER login of that role NOLOGIN
# and ends its sessions, so a previous release's identity stops working at cutover; provisioning the previous name
# again (a rollback) reverses it the same way.
RELEASE_ROLES = ("agent", "console")


def provision(session: OwnerSession, logins: dict[str, dict]) -> list[str]:
    """`logins`: role → {"username", "password"}. Returns the usernames provisioned."""
    statements = ["SET LOCAL pgaudit.log = 'none'"]
    names = [r[0] for r in session.query("SELECT rolname FROM pg_roles")]
    # Aurora's owner is not a superuser, and only a superuser may name SUPERUSER, REPLICATION or BYPASSRLS on an
    # existing role, even to turn them off. So an existing login is checked here instead (fail closed: a non-superuser
    # could not remove them) and its ALTER sets only what the owner may set. CREATE ROLE may state them off.
    bad = session.query("""SELECT rolname FROM pg_roles WHERE rolname LIKE 'setuhaul!_login!_%' ESCAPE '!'
                             AND (rolsuper OR rolreplication OR rolbypassrls)""")
    if bad:
        raise RuntimeError(f"login roles with SUPERUSER, REPLICATION or BYPASSRLS: {[r[0] for r in bad]}")
    done = []
    for role, cred in logins.items():
        if role not in APP_ROLES:
            raise ValueError(f"unknown role {role!r}")
        user, password = cred["username"], cred["password"]
        if not _LOGIN.match(user):
            raise ValueError(f"login name {user!r} does not follow setuhaul_login_<role>")
        if user.startswith("setuhaul_login_") and not re.fullmatch(rf"setuhaul_login_{role}(_[0-9a-f]{{12}})?", user):
            raise ValueError(f"login name {user!r} is not a login of role {role!r}")
        if len(password) < 24:
            raise ValueError(f"the password for {user} is too short")
        if user in names:
            statements.append(f"ALTER ROLE {user} WITH LOGIN NOCREATEDB NOCREATEROLE PASSWORD {literal(password)}")
        else:
            statements.append(f"CREATE ROLE {user} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                              f"PASSWORD {literal(password)}")
        for other_role, other in APP_ROLES.items():
            if other_role != role:
                statements.append(f"REVOKE {other} FROM {user}")
        statements.append(f"GRANT {APP_ROLES[role]} TO {user}")
        if role in RELEASE_ROLES:
            for other in sorted(n for n in names if n != user and re.fullmatch(rf"setuhaul_login_{role}(_[0-9a-f]{{12}})?", n)):
                statements.append(f"ALTER ROLE {other} WITH NOLOGIN")
                statements.append(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = {literal(other)}")
        done.append(user)
    session.run(statements)
    return done


def read_logins(secret_arns: dict[str, str], client=None) -> dict[str, dict]:
    if client is None:
        import boto3
        client = boto3.client("secretsmanager")
    out = {}
    for role, arn in secret_arns.items():
        v = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
        out[role] = {"username": v["username"], "password": v["password"]}
    return out


def release_problems(logins: dict[str, dict], release: str) -> list[str]:
    """M2: before anything is provisioned, the booking roles' secrets must already name this release's login (the
    stack writes it; if CloudFormation did not regenerate a secret, stop here, before any evidence is recorded)."""
    if not re.fullmatch(r"[0-9a-f]{12}", release or ""):
        return [f"the expected release {release!r} is not 12 hex characters"]
    return [f"{role}: the secret names {logins[role]['username']!r}, expected setuhaul_login_{role}_{release}"
            for role in RELEASE_ROLES if role in logins and logins[role]["username"] != f"setuhaul_login_{role}_{release}"]


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    import boto3
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--expect-release", help="M2: refuse unless the agent and console secrets name this release's "
                                             "logins (the first 12 hex of the Lambda zip's SHA-256)")
    a = ap.parse_args(argv)
    logins = read_logins(json.loads(os.environ["SETUHAUL_DB_SECRET_ARNS"]))
    if a.expect_release and (problems := release_problems(logins, a.expect_release)):
        print("REFUSED (M2 release identity): " + "; ".join(problems), file=sys.stderr)
        return 1
    s = OwnerSession(boto3.client("rds-data"), os.environ["SETUHAUL_DB_CLUSTER_ARN"],
                     os.environ["SETUHAUL_DB_OWNER_SECRET_ARN"], os.environ.get("SETUHAUL_DB_NAME", "setuhaul"))
    for u in provision(s, logins):
        print("provisioned", u)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
