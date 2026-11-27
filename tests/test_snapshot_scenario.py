"""Defend: the scenario run on the SUPPLIED snapshot (scripts/snapshot_scenario.py) passes every check.

The script loads data/setuhaul_freight_operations.db into a fresh PostgreSQL database through the real constraints,
replays four ladder cases with the drivers' recorded words through the signed webhook and the orchestrator (rules
reader, no model call), and ends with SHP1015's NO_FEASIBLE_SLOT escalation. Evidence is rewritten only with
`--record`, never by this test.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import BASE_DSN, _pg_available

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_scenario.py"


def test_snapshot_scenario_passes_and_shp1015_escalates():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable at SETUHAUL_TEST_DSN")
    r = subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ, "SETUHAUL_TEST_DSN": BASE_DSN},
                       capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "RESULT: PASS" in r.stdout
    assert "[PASS] L6: NO_FEASIBLE_SLOT escalation, OPEN, with a hand-off record" in r.stdout
