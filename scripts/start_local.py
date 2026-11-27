"""One-command local start: a private PostgreSQL 18 cluster plus the SetuHaul demo console.

    python scripts/start_local.py              # start (or reuse) the local database, then the demo console
    python scripts/start_local.py --db-only    # only the database; prints the SETUHAUL_TEST_DSN to use for tests
    python scripts/start_local.py --stop       # stop the local database

What it does:
- It finds PostgreSQL's `initdb` and `pg_ctl`, in this order: SETUHAUL_PG_BIN, then PATH, then the standard
  install folders (Windows `C:\\Program Files\\PostgreSQL\\<v>\\bin`, Debian/Ubuntu `/usr/lib/postgresql/<v>/bin`,
  Homebrew).
- It creates a private cluster in `.local/pgdata`. This folder is git-ignored and uses trust authentication on
  localhost only, so it is a throwaway development database. The cluster is created once and reused after that.
- It starts the cluster on port 55432 (`--port` changes this). If something already accepts connections there, it
  is used as is.
- Then it runs `python demo/server.py --reset`. That creates the database `setuhaul_demo`, applies every migration,
  seeds synthetic drivers and serves http://127.0.0.1:8765/demo. The demo uses the rules reader: no AI model, no
  network, no AWS, no cost.

It touches nothing outside the repository folder, apart from the local TCP port.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
PGDATA = LOCAL / "pgdata"
LOG = LOCAL / "pg.log"
EXE = ".exe" if os.name == "nt" else ""


def find_bin() -> Path:
    cands = []
    if os.environ.get("SETUHAUL_PG_BIN"):
        cands.append(Path(os.environ["SETUHAUL_PG_BIN"]))
    w = shutil.which("pg_ctl")
    if w:
        cands.append(Path(w).parent)
    pats = [r"C:\Program Files\PostgreSQL\*\bin", "/usr/lib/postgresql/*/bin", "/opt/homebrew/opt/postgresql@*/bin",
            "/usr/local/opt/postgresql@*/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    for pat in pats:
        cands += sorted((Path(p) for p in glob.glob(pat)), reverse=True)        # newest version first
    for c in cands:
        if (c / f"pg_ctl{EXE}").exists() and (c / f"initdb{EXE}").exists():
            return c
    sys.exit("PostgreSQL 18 binaries not found. Install PostgreSQL 18 (only the server binaries are needed), or set "
             "SETUHAUL_PG_BIN to the folder that holds initdb and pg_ctl.")


def can_connect(port: int) -> bool:
    import psycopg
    try:
        with psycopg.connect(f"host=localhost port={port} user=postgres dbname=postgres connect_timeout=2"):
            return True
    except psycopg.Error:
        return False


def run(cmd: list[str]) -> int:
    # Output goes to files or DEVNULL: on Windows, pg_ctl's server child would otherwise keep a pipe open forever.
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def start_db(port: int) -> str:
    dsn = f"host=localhost port={port} user=postgres"
    if can_connect(port):
        print(f"[db] PostgreSQL already accepting connections on port {port}: using it")
        return dsn
    b = find_bin()
    if not (PGDATA / "PG_VERSION").exists():
        print(f"[db] creating a private cluster in {PGDATA.relative_to(ROOT)} with {b}")
        LOCAL.mkdir(exist_ok=True)
        if run([str(b / f"initdb{EXE}"), "-D", str(PGDATA), "-U", "postgres", "-A", "trust", "-E", "UTF8"]) != 0:
            sys.exit("[db] initdb failed")
    print(f"[db] starting PostgreSQL on port {port} (log: {LOG.relative_to(ROOT)})")
    run([str(b / f"pg_ctl{EXE}"), "-D", str(PGDATA), "-l", str(LOG), "-o", f"-p {port} -c listen_addresses=localhost",
         "start"])
    for _ in range(60):
        if can_connect(port):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"[db] PostgreSQL did not start; see {LOG}")
    with __import__("psycopg").connect(f"{dsn} dbname=postgres") as c:
        ver = c.execute("SHOW server_version").fetchone()[0]
    print(f"[db] ready: PostgreSQL {ver}")
    if not ver.startswith("18"):
        print("[db] note: the project targets PostgreSQL 18 (17.9 is the pilot-only exception T33-P)")
    return dsn


def stop_db() -> None:
    if not (PGDATA / "PG_VERSION").exists():
        print("[db] no local cluster in .local/pgdata")
        return
    b = find_bin()
    rc = run([str(b / f"pg_ctl{EXE}"), "-D", str(PGDATA), "-m", "fast", "-w", "stop"])
    print("[db] stopped" if rc == 0 else "[db] was not running")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("SETUHAUL_PG_PORT", "55432")))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--db-only", action="store_true", help="start the database only and print the DSN")
    g.add_argument("--stop", action="store_true", help="stop the local database")
    ap.add_argument("--keep", action="store_true", help="demo: keep last run's demo data instead of --reset")
    a = ap.parse_args()
    if sys.version_info < (3, 14):
        print(f"note: the project targets Python 3.14 (this is {sys.version.split()[0]})")
    if a.stop:
        stop_db()
        return 0
    dsn = start_db(a.port)
    if a.db_only:
        print(f'\nexport SETUHAUL_TEST_DSN="{dsn}"      # PowerShell: $env:SETUHAUL_TEST_DSN="{dsn}"')
        return 0
    print("[demo] http://127.0.0.1:8765/demo  (Ctrl+C to stop; the database keeps running until --stop)")
    env = {**os.environ, "SETUHAUL_DEMO_DSN": dsn}
    return subprocess.call([sys.executable, str(ROOT / "demo" / "server.py"), *([] if a.keep else ["--reset"])],
                           cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
