"""Forward-only migrations over the RDS Data API (build item E5; AURORA gate).

Aurora sits in an isolated VPC with no route in, so the pipeline reaches it through the Data API with the owner
secret. The Data API runs one statement per call, so each migration file is split into statements (respecting quotes,
comments and dollar-quoted function bodies) and run inside one Data API transaction, then recorded with its SHA-256
exactly as `setuhaul.db.migrate` does. An applied migration that has since changed is refused. A transaction-level
advisory lock keeps two runners from interleaving.

    python -m setuhaul.aws.migrate_data_api      # env: SETUHAUL_DB_CLUSTER_ARN, SETUHAUL_DB_OWNER_SECRET_ARN
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..db.migrate import META, MIGRATIONS, MigrationError, discover
from .data_api import Connection, translate

_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


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
            for s in statements:
                conn.execute(s.replace("%", "%%"))                  # no parameters: % is literal
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

    def query(self, sql: str) -> list[tuple]:
        kw = {"resourceArn": self.arn, "secretArn": self.secret}
        tid = self.c.begin_transaction(database=self.db, **kw)["transactionId"]
        try:
            rows = Connection(self.c, self.arn, self.secret, self.db, tid).execute(sql).fetchall()
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
