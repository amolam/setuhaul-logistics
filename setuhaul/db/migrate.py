"""Forward-only SQL migration runner (B-DB-1).

Each file in migrations/ (NNNN_name.sql) runs once, in order, in its own transaction, and is recorded with its
SHA-256. A migration that was already applied and has since been edited is refused: applied migrations are
immutable, and a change needs a new file. Run by the deployment pipeline with the migration (owner) credentials,
never by the application roles.

    python -m setuhaul.db.migrate "host=localhost port=55432 user=postgres dbname=setuhaul"
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

META = """
CREATE SCHEMA IF NOT EXISTS setuhaul_meta;
CREATE TABLE IF NOT EXISTS setuhaul_meta.schema_migrations (
  version     text PRIMARY KEY,
  sha256      text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON SCHEMA setuhaul_meta FROM PUBLIC;
"""


class MigrationError(RuntimeError):
    pass


def discover(folder: Path = MIGRATIONS) -> list[tuple[str, Path, str]]:
    out = []
    for p in sorted(folder.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        out.append((p.stem, p, hashlib.sha256(p.read_bytes()).hexdigest()))
    return out


def migrate(dsn: str, folder: Path = MIGRATIONS) -> list[str]:
    """Apply pending migrations. Returns the versions applied by this call."""
    applied_now = []
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(META)
        # one runner at a time (two pipelines deploying together must not interleave)
        c.execute("SELECT pg_advisory_lock(hashtext('setuhaul_meta.migrate'))")
        try:
            done = dict(c.execute("SELECT version, sha256 FROM setuhaul_meta.schema_migrations").fetchall())
            for version, path, sha in discover(folder):
                if version in done:
                    if done[version] != sha:
                        raise MigrationError(f"{version} was applied with sha256 {done[version][:12]}… but the file now has "
                                             f"{sha[:12]}…. Applied migrations are immutable; add a new migration instead.")
                    continue
                with c.transaction():
                    c.execute(path.read_text(encoding="utf-8"))
                    c.execute("RESET search_path")
                    c.execute("INSERT INTO setuhaul_meta.schema_migrations (version, sha256) VALUES (%s, %s)", (version, sha))
                applied_now.append(version)
        finally:
            c.execute("SELECT pg_advisory_unlock(hashtext('setuhaul_meta.migrate'))")
    return applied_now


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for v in migrate(sys.argv[1]) or ["(nothing to apply)"]:
        print(v)
