"""In-VPC bootstrap for the express Aurora cluster (invoked only by express_cluster.py during a deploy).

The master user of an express cluster signs in with IAM only, and the Data API refuses it, so this one function, in
the VPC's isolated subnets with port 5432 open to the database only, signs in as the master with a short-lived IAM
token over TLS and creates what the master secret used to be for: the non-master owner login (CREATEROLE, CREATEDB, member
of rds_superuser, exactly as the master was; never SUPERUSER, which Aurora does not grant) and the `setuhaul` database it
owns. From then on the pipeline's migrations, logins and seed run as that owner through the Data API, unchanged.

Idempotent. The owner password arrives as a SCRAM-SHA-256 verifier only; nothing secret is logged or returned.
"""
from __future__ import annotations

import os
import re

NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def run(conn, database: str, owner: str, owner_verifier: str) -> dict:
    from psycopg import sql
    if not (NAME.match(database) and NAME.match(owner)) or not owner_verifier.startswith("SCRAM-SHA-256$"):
        raise ValueError("refused: database/owner names or the password verifier are malformed")
    o = sql.Identifier(owner)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (owner,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    # no NOSUPERUSER clause: only a superuser may name that attribute; the role is checked below instead
    conn.execute(sql.SQL(verb + " ROLE {} LOGIN CREATEROLE CREATEDB PASSWORD {}").format(o, sql.Literal(owner_verifier)))
    conn.execute(sql.SQL("GRANT rds_superuser TO {}").format(o))
    conn.execute(sql.SQL("GRANT {} TO current_user WITH SET TRUE").format(o))      # PG16+: needed for OWNER below
    created_db = False
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), o))
        created_db = True
    attrs = conn.execute("""SELECT rolsuper, rolcreaterole, rolcreatedb, pg_has_role(rolname, 'rds_superuser', 'MEMBER')
                            FROM pg_roles WHERE rolname = %s""", (owner,)).fetchone()
    db_owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (database,)).fetchone()[0]
    if attrs != (False, True, True, True) or db_owner != owner:
        raise RuntimeError(f"owner not as intended: attrs={attrs} database owner={db_owner}")
    version = conn.execute("SELECT current_setting('server_version')").fetchone()[0]
    return {"ok": True, "owner": owner, "owner_created": not exists, "database": database, "database_created": created_db,
            "server_version": version}


def handler(event, _context):
    import boto3
    import psycopg
    host = event["host"]
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    token = boto3.client("rds", region_name=region).generate_db_auth_token(host, 5432, "postgres")
    with psycopg.connect(host=host, port=5432, user="postgres", dbname="postgres", password=token,
                         sslmode="require", connect_timeout=30, autocommit=True) as conn:
        return run(conn, event["database"], event["owner"], event["owner_verifier"])
