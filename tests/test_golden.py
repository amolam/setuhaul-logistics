"""G3: the golden dataset exists, is well formed, matches the seed snapshot, and its labels are self-consistent."""
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.golden import DATE, GROUPS, POLICY, grade, load, option_ids, oracle_raw  # noqa: E402
from evals.run_golden import run                                 # noqa: E402
from setuhaul.agent.extraction import Extraction                 # noqa: E402

SEED = Path(__file__).resolve().parents[1] / "data" / "setuhaul_schema_and_seed.sql"
SCENARIOS, DATASET = load()


@pytest.fixture(scope="module")
def seed():
    c = sqlite3.connect(":memory:")
    c.executescript(SEED.read_text(encoding="utf-8"))
    ships = {r[0]: (r[1], r[2]) for r in c.execute("SELECT shipment_id, order_reference, driver_id FROM shipments")}
    drivers = {r[0] for r in c.execute("SELECT driver_id FROM drivers")}
    return ships, drivers


def test_composition_matches_the_signed_off_spec():
    core = [s for s in SCENARIOS if s.group != "policy"]
    assert len(core) == 60 and Counter(s.group for s in core) == Counter(GROUPS)
    assert sum(s.group == "policy" for s in SCENARIOS) == POLICY        # v3: OD-1 / OD-2, outside the signed-off 60
    assert len({s.id for s in SCENARIOS}) == len(SCENARIOS)
    assert {f"THR{i:03d}" for i in range(1, 13)} <= {s.id for s in SCENARIOS}
    assert DATASET.startswith("v4+sha256:")
    assert load("v3")[1] == "v3+sha256:f28789922065e7d2"                   # v3 frozen as sent for review
    assert load("v2")[1] == "v2+sha256:d9fc6777cb68343f"                   # v2 frozen as sent for review


def test_reviewed_v1_is_frozen():
    """v1 was reviewed (step 3 update) and its model runs are recorded against this hash; it must never change."""
    v1, ds = load("v1")
    assert len(v1) == 60 and ds == "v1+sha256:66a96947a6bf1ad7"


def test_every_reference_exists_in_the_seed(seed):
    ships, drivers = seed
    refs = {o: (sid, d) for sid, (o, d) in ships.items()}
    for s in SCENARIOS:
        assert s.driver in drivers, s.id
        if s.shipment:
            assert ships[s.shipment][1] == s.driver, f"{s.id}: {s.shipment} is not {s.driver}'s"
        for t in s.turns:
            assert (t.driver or s.driver) in drivers, s.id
            for r in t.ctx.ships:
                assert refs[r][1] == (t.driver or s.driver), f"{s.id}: {r} is not this driver's"


def oracle(s, t) -> Extraction:
    """The extraction a perfect model would return, built from the labels alone (shared with the replay runner)."""
    return Extraction(**oracle_raw(t.x, option_ids(t)))


# A perfect extraction can still fail where the code adds its own judgement. Each entry is a finding, not a label bug.
# Fixed on 18 Nov: ADV02#0 (the safety word list matched 'medical' in 'medical load').
KNOWN_CODE_FINDINGS: dict[str, str] = {}


def test_labels_are_self_consistent_with_the_code_checks():
    failures = {}
    for s in SCENARIOS:
        for i, t in enumerate(s.turns):
            if t.x is None:
                continue
            bad, _ = grade(oracle(s, t), s, t)
            if bad:
                failures[f"{s.id}#{i}"] = bad
    assert set(failures) == set(KNOWN_CODE_FINDINGS), failures


def test_rules_baseline_runs_and_records_the_dataset_version():
    a, b = run("rules"), run("rules")
    # v2 (27 Nov 2026, issue 9): requested_date_local and the RESCHEDULE intent; the dataset's labels are unchanged
    assert a["run"]["dataset"] == DATASET and a["run"]["schema_version"] == "record_driver_report/v2"
    assert a["metrics"] == b["metrics"]                        # deterministic: the ablation baseline is stable
    assert a["metrics"]["turns"] == sum(1 for s in SCENARIOS for t in s.turns if t.x and t.duplicate_of is None)
    assert a["metrics"]["schema_valid"] == 1.0


# ---------------------------------------------------------------- B-EV-1b: held-out set h1
def test_heldout_h1_is_frozen_and_traceable():
    """h1 was frozen before any model ran on it. Every variant traces to a labelled base turn in v4, with the base's
    labels (time variants: the same labels, every time moved by 40 minutes)."""
    h1, ds = load("h1")
    assert ds == "h1+sha256:2e72cb40af0b106d" and len(h1) == 167
    base = {f"{s.id}#{i}": t for s in SCENARIOS for i, t in enumerate(s.turns)}
    shift = lambda v: (datetime.strptime(v, "%H:%M") + timedelta(minutes=40)).strftime("%H:%M")
    for v in h1:
        b, kind = v.id.rsplit("~", 1)
        bt, vt = base[b], v.turns[0]
        if kind == "time":
            assert {k: (shift(x) if k in ("eta", "earliest", "latest") and x else x) for k, x in bt.x.items()} == vt.x, v.id
        else:
            assert bt.x == vt.x and bt.ctx == vt.ctx, v.id


def test_heldout_labels_are_self_consistent():
    h1, _ = load("h1")
    bad = {}
    for s in h1:
        t = s.turns[0]
        b, _ = grade(oracle(s, t), s, t)
        if b:
            bad[s.id] = b
    assert not bad, bad
