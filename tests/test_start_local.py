"""scripts/start_local.py only ever uses its OWN private cluster, and never touches a server it does not own.

Every test points the script at its own temporary folder (SETUHAUL_LOCAL_DIR) and free ports, so nothing in the
repository's .local/ and no shared server is changed.
- A PostgreSQL server the script does not own (the test server) holds the port: exit 2, nothing created or reset
  on it, in --db-only and in demo (--reset) mode.
- Something that is not PostgreSQL holds the port: exit 2 straight away.
- The private cluster's lifecycle (needs PostgreSQL server binaries; skipped without them): start, reuse on the same
  port, refuse promptly on another port (naming the real one), stop, stop again.
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest

from conftest import BASE_DSN, _pg_available

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start_local.py"


def run(args, local_dir, timeout=120):
    env = {**os.environ, "SETUHAUL_LOCAL_DIR": str(local_dir)}
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout, stdin=subprocess.DEVNULL)
    return r, time.monotonic() - t0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def databases(dsn):
    with psycopg.connect(f"{dsn} dbname=postgres", autocommit=True) as c:
        return {n: oid for n, oid in c.execute("SELECT datname, oid FROM pg_database")}


@pytest.mark.parametrize("mode", [["--db-only"], []])
def test_a_postgresql_server_it_does_not_own_is_refused_and_left_untouched(tmp_path, mode):
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    port = int(re.search(r"port=(\d+)", BASE_DSN).group(1))
    before = databases(BASE_DSN)
    r, _ = run([*mode, "--port", str(port)], tmp_path / "local")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "NOT this script's private cluster" in r.stderr and "nothing was reset, created or dropped" in r.stderr
    assert "PostgreSQL" in r.stderr                                       # version (and data dir where visible)
    assert f"--port {port + 1}" in r.stderr
    assert "[demo]" not in r.stdout                                      # the demo (--reset) never started
    assert databases(BASE_DSN) == before                                 # same databases, same oids: nothing reset
    assert not (tmp_path / "local" / "pgdata").exists()                  # and no cluster was created for it


def test_a_listener_that_is_not_postgresql_is_refused(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                pass

    th = threading.Thread(target=accept, daemon=True)
    th.start()
    try:
        r, secs = run(["--db-only", "--port", str(port)], tmp_path / "local")
    finally:
        stop.set()
        th.join()
        srv.close()
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot identify" in r.stderr and "NOT this script's private cluster" in r.stderr
    assert secs < 30


def _has_binaries() -> bool:
    import importlib.util
    spec = importlib.util.spec_from_file_location("start_local_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                   # module level only defines constants and functions
    try:
        mod.find_bin()
        return True
    except SystemExit:
        return False


def test_private_cluster_start_reuse_refuse_other_port_and_stop(tmp_path):
    local = tmp_path / "local"
    if not _has_binaries():
        pytest.skip("PostgreSQL server binaries (initdb, pg_ctl) not installed")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb refuses to run as root")
    a, b = free_port(), free_port()
    try:
        r, _ = run(["--db-only", "--port", str(a)], local, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[db] ready: PostgreSQL" in r.stdout and f"port={a}" in r.stdout

        r, _ = run(["--db-only", "--port", str(a)], local)
        assert r.returncode == 0 and "already running on port" in r.stdout and "reusing it" in r.stdout, r.stdout

        r, secs = run(["--db-only", "--port", str(b)], local)
        assert r.returncode == 2, r.stdout + r.stderr
        assert f"already running on port {a}" in r.stderr and f"Use --port {a}" in r.stderr
        assert secs < 30                                                 # promptly: no wait for a timeout
        with pytest.raises(OSError):                                     # and no second server was started on b
            socket.create_connection(("127.0.0.1", b), timeout=1).close()
    finally:
        r, _ = run(["--stop"], local)
    assert "[db] stopped" in r.stdout, r.stdout + r.stderr
    r, _ = run(["--stop"], local)
    assert "[db] was not running" in r.stdout, r.stdout + r.stderr
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", a), timeout=1).close()
