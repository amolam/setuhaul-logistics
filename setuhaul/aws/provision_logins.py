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
_LOGIN = re.compile(r"^setuhaul_login_[a-z_]{2,40}$|^test_login_[a-z0-9_]{2,60}$")


def provision(session: OwnerSession, logins: dict[str, dict]) -> list[str]:
    """`logins`: role → {"username", "password"}. Returns the usernames provisioned."""
    statements = ["SET LOCAL pgaudit.log = 'none'"]
    names = [r[0] for r in session.query("SELECT rolname FROM pg_roles")]
    done = []
    for role, cred in logins.items():
        if role not in APP_ROLES:
            raise ValueError(f"unknown role {role!r}")
        user, password = cred["username"], cred["password"]
        if not _LOGIN.match(user):
            raise ValueError(f"login name {user!r} does not follow setuhaul_login_<role>")
        if len(password) < 24:
            raise ValueError(f"the password for {user} is too short")
        verb = "ALTER" if user in names else "CREATE"
        statements.append(f"{verb} ROLE {user} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                          f"PASSWORD {literal(password)}")
        for other_role, other in APP_ROLES.items():
            if other_role != role:
                statements.append(f"REVOKE {other} FROM {user}")
        statements.append(f"GRANT {APP_ROLES[role]} TO {user}")
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


if __name__ == "__main__":
    import boto3
    s = OwnerSession(boto3.client("rds-data"), os.environ["SETUHAUL_DB_CLUSTER_ARN"],
                     os.environ["SETUHAUL_DB_OWNER_SECRET_ARN"], os.environ.get("SETUHAUL_DB_NAME", "setuhaul"))
    for u in provision(s, read_logins(json.loads(os.environ["SETUHAUL_DB_SECRET_ARNS"]))):
        print("provisioned", u)
