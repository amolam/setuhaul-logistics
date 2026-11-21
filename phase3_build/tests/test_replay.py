"""B-EV-2 as a regression gate: the golden set replayed end to end (API, orchestrator, booking service, PostgreSQL)
with a scripted model that returns each turn's labelled extraction. Everything after extraction must be right:
decisions, tool calls, database end state and reply rules (06_review_response.md: "core-only on every commit").
The live-model replay (`python evals/replay.py --model live`) is run on demand; it costs money."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import BASE_DSN, _pg_available  # noqa: E402

KNOWN_SKIPS = {"BB04", "BB07", "BB13"}            # same-slot races (tests/test_api.py) and the step 4 scheduler


@pytest.fixture(scope="module")
def replay():
    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    from evals.replay import run
    return run(live=False, base_dsn=BASE_DSN)


def test_every_playable_scenario_passes_end_to_end(replay):
    failed = {r["id"]: [f for t in r["turns"] for f in t["fails"] + t["hard"]] or r.get("error") or
              {k: v for k, v in r["end"].items() if v["pass"] is False}
              for r in replay["scenarios"] if not r.get("skipped") and not r["pass"]}
    assert not failed, failed


def test_no_hard_gate_violations_and_no_crashes(replay):
    s = replay["summary"]
    assert s["hard_gate_violations"] == 0 and s["errors"] == 0


def test_only_the_known_scenarios_are_skipped(replay):
    assert {r["id"] for r in replay["scenarios"] if r.get("skipped")} == KNOWN_SKIPS
