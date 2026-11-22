"""Forward-only migrations over the RDS Data API (build item E5; AURORA gate).

Aurora sits in an isolated VPC with no route in, so the pipeline reaches it through the Data API with the owner
secret. The Data API runs one statement per call, so each migration file is split into statements (respecting quotes,
comments and dollar-quoted function bodies) and run inside one Data API transaction, then recorded with its SHA-256
exactly as `setuhaul.db.migrate` does. An applied migration that has since changed is refused. A transaction-level
advisory lock keeps two runners from interleaving.

Managed PostgreSQL (Aurora) has no superuser: its owner is a CREATEROLE member of `rds_superuser`. The migrations were
written for a superuser, and three statements kinds fail for anyone else (found by running them as such a role on a
fresh cluster). When the session is not a superuser, and only then, the runner adapts them inside the same
transaction, without changing any migration file or the resulting catalog:

* `ALTER ROLE x NOSUPERUSER NOBYPASSRLS ...`: only a superuser may name those attributes, even to turn them off. The
  runner checks that they are already off (a role created with CREATE ROLE has them off) and runs the rest of the
  clauses; if one is on, it stops with an error, because a non-superuser cannot remove it.
* `ALTER ... OWNER TO setuhaul_rpc`: the new owner must hold CREATE on the schema unless a superuser runs it. The
  runner grants CREATE on `setuhaul` to `setuhaul_rpc` before the first such statement and revokes it before the
  migration commits, so the final privileges are those a superuser run gives.
* Roles created by a non-superuser cannot be SET ROLE to by their creator on PostgreSQL 16+, which ALTER OWNER needs:
  `createrole_self_grant = 'set'` for the transaction makes the migration user a member of the roles it creates
  (INHERIT FALSE), as the cluster's owner would be in any case.

    python -m setuhaul.aws.migrate_data_api      # env: SETUHAUL_DB_CLUSTER_ARN, SETUHAUL_DB_OWNER_SECRET_ARN
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..db.migrate import META, MIGRATIONS, MigrationError, discover
from .data_api import Connection, translate

_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_ALTER_ROLE = re.compile(r"^ALTER\s+ROLE\s+([a-z_][a-z0-9_]*)\s+([A-Z][A-Z ]*)$")
_SUPER_ATTRS = {"NOSUPERUSER": "rolsuper", "NOBYPASSRLS": "rolbypassrls"}


def _code(s: str) -> str:
    s = re.sub(r"--[^\n]*", "", s)
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S).strip()


def adapt_for_non_superuser(conn, statement: str, state: dict) -> str | None:
    """The statement to run in a non-superuser session (None: nothing left to run). `state` tracks the CREATE grant."""
    code = _code(statement)
    m = _ALTER_ROLE.match(code)
    if m and set(m.group(2).split()) & set(_SUPER_ATTRS):
        role, clauses = m.group(1), m.group(2).split()
        attrs = [_SUPER_ATTRS[c] for c in clauses if c in _SUPER_ATTRS]
        row = conn.execute(f"SELECT {', '.join(attrs)} FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if row is None or any(row):
            raise MigrationError(f"role {role} has {attrs} on (or is missing); only a superuser can change that")
        rest = [c for c in clauses if c not in _SUPER_ATTRS]
        return f"ALTER ROLE {role} {' '.join(rest)}" if rest else None
    if "OWNER TO setuhaul_rpc" in code and not state.get("create_granted"):
        conn.execute("GRANT CREATE ON SCHEMA setuhaul TO setuhaul_rpc")
        state["create_granted"] = True
    return statement


def split_sql(text: str) -> list[str]:
    """Split a SQL script into statements on top-level semicolons."""
    out, buf, i, n = [], [], 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":                                # line comment
            j = text.find("\n", i)
            j = n if j < 0 else j
            buf.append(text[i:j])
            i = j
        elif ch == "/" and nxt == "*":                              # block comment (nesting allowed in PostgreSQL)
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            buf.append(text[i:j])
            i = j
        elif ch in ("'", '"'):                                      # string or quoted identifier ('' / "" escapes)
            j = i + 1
            while j < n:
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
        elif ch == "$" and (m := _DOLLAR.match(text, i)) and not (buf and re.match(r"[A-Za-z0-9_]", buf[-1][-1:] or " ")):
            tag = m.group(0)
            j = text.find(tag, m.end())
            if j < 0:
                raise MigrationError(f"unterminated dollar quote {tag}")
            buf.append(text[i:j + len(tag)])
            i = j + len(tag)
        elif ch == ";":
            out.append("".join(buf))
            buf, i = [], i + 1
        else:
            buf.append(ch)
            i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if _has_code(s)]


def _has_code(s: str) -> bool:
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return bool(s.strip())


class OwnerSession:
    """A raw Data API transaction with the owner secret (not an application role)."""

    def __init__(self, client, cluster_arn: str, owner_secret_arn: str, database: str = "setuhaul"):
        self.c, self.arn, self.secret, self.db = client, cluster_arn, owner_secret_arn, database

    def run(self, statements: list[str], record: tuple[str, str] | None = None) -> None:
        kw = {"resourceArn": self.arn, "secretArn": self.secret}
        try:
            tid = self.c.begin_transaction(database=self.db, **kw)["transactionId"]
        except Exception as e:
            raise translate(e) if getattr(e, "response", None) else e from None
        conn = Connection(self.c, self.arn, self.secret, self.db, tid)
        try:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('setuhaul_meta.migrate'))")
            superuser = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
            state: dict = {}
            if not superuser:
                conn.execute("SET LOCAL createrole_self_grant = 'set, inherit'")
            for s in statements:
                if not superuser:
                    s = adapt_for_non_superuser(conn, s, state)
                    if s is None:
                        continue
                conn.execute(s.replace("%", "%%"))                  # no parameters: % is literal
            if state.get("create_granted"):
                conn.execute("REVOKE CREATE ON SCHEMA setuhaul FROM setuhaul_rpc")
            conn.execute("RESET search_path")
            if record:
                conn.execute("INSERT INTO setuhaul_meta.schema_migrations (version, sha256) VALUES (%s, %s)", record)
        except BaseException:
            try:
                self.c.rollback_transaction(transactionId=tid, **kw)
            except Exception:
                pass
            raise
        try:
            self.c.commit_transaction(transactionId=tid, **kw)
        except Exception as e:
            raise translate(e) if getattr(e, "response", None) else e from None

    def query(self, sql: str, setup: tuple[str, ...] = ()) -> list[tuple]:
        """Rows of one read, in a transaction that is rolled back; `setup` runs first (e.g. a facility scope)."""
        kw = {"resourceArn": self.arn, "secretArn": self.secret}
        tid = self.c.begin_transaction(database=self.db, **kw)["transactionId"]
        try:
            conn = Connection(self.c, self.arn, self.secret, self.db, tid)
            for s in setup:
                conn.execute(s)
            rows = conn.execute(sql).fetchall()
        finally:
            self.c.rollback_transaction(transactionId=tid, **kw)
        return rows


def migrate_data_api(session: OwnerSession, folder: Path = MIGRATIONS) -> list[str]:
    session.run(split_sql(META))
    done = dict(session.query("SELECT version, sha256 FROM setuhaul_meta.schema_migrations"))
    applied = []
    for version, path, sha in discover(folder):
        if version in done:
            if done[version] != sha:
                raise MigrationError(f"{version} was applied with sha256 {done[version][:12]}… but the file now has "
                                     f"{sha[:12]}…. Applied migrations are immutable; add a new migration instead.")
            continue
        session.run(split_sql(path.read_text(encoding="utf-8")), (version, sha))
        applied.append(version)
    return applied


if __name__ == "__main__":
    import boto3
    s = OwnerSession(boto3.client("rds-data"), os.environ["SETUHAUL_DB_CLUSTER_ARN"],
                     os.environ["SETUHAUL_DB_OWNER_SECRET_ARN"], os.environ.get("SETUHAUL_DB_NAME", "setuhaul"))
    for v in migrate_data_api(s) or ["(nothing to apply)"]:
        print(v)
