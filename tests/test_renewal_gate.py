"""Booking evidence renewal gate (docs/booking-renewal/). Unit tests for every rule in setuhaul/ops/renewal.py, the
evidence writer's renewal path, the activation path unchanged, and migration 0029 (RLS-safe record access) on PostgreSQL."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from setuhaul.ops import renewal as R
from setuhaul.ops.gate_evidence import EvidenceRefused, activate, rows_from_report

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "docs" / "release" / "evidence"
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
SHA = "e280a40789598adc2720417dc59cd423dec28921"
ART = hashlib.sha256(b"lambda.zip").hexdigest()
REF = "OWNER-APPROVAL-20261123-PHASE-G1"
C1, C2 = "87915a37-918e-46b5-8872-8ecaa4fc7a24", "29ffac03-d8bd-4dac-b35c-66b0f8ebf24b"
B_OLD = "73cac501-fe58-457a-80d5-b90234d56139"
OWNER = json.loads((EVID / "owner_attribution_20261123.json").read_text(encoding="utf-8"))
SMOKE_OLD = json.loads((EVID / "option_d_smoke.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ fixtures (pure data)
def fns(**over):
    base = {n: {"booking": "false", "env": "pilot", "data_class": "synthetic"} for n in (
        "setuhaul-pilot-recovery", "setuhaul-pilot-outbound", "setuhaul-pilot-approval-relay",
        "setuhaul-pilot-approval-notify", "setuhaul-pilot-approval-token", "setuhaul-pilot-approval-expire")}
    base["setuhaul-pilot-api"] = {"booking": "true", "env": "pilot", "data_class": "synthetic"}
    base["setuhaul-pilot-agent"] = {"booking": "true", "env": "pilot", "data_class": "synthetic"}
    base["setuhaul-pilot-db-bootstrap"] = {"booking": None, "env": None, "data_class": None}     # platform function
    for k, v in over.items():
        base[k.replace("_", "-")] = {**base.get(k.replace("_", "-"), {}), **v}
    return base


HEALTH = {"status": "ok", "database": "ok", "booking_enabled": True, "environment": "pilot-ap-southeast-2",
          "booking_kill_switch": "disengaged", "gate_exceptions": ["AURORA", "OUTBOUND_DELIVERY", "WORKLOAD_IDENTITY"]}
MODE = "ON (OWNER-APPROVAL-20261123-PHASE-G)"


def row(aid, shipment="SYN-SHP-AAAA0001", facility="FAC-JAI-01", status="APPROVED", appt="APT-1", appt_status="CONFIRMED",
        by="HUMAN", kind="console_user", decided="2026-11-24T09:00:05+00:00"):
    return {"approval_id": aid, "kind": "STANDARD", "facility_id": facility, "shipment_id": shipment,
            "shipment_is_synthetic": shipment.startswith("SYN-"), "appointment_id": appt, "appointment_status": appt_status,
            "status": status, "decided_by_type": by, "decided_by_role": "planner", "decider_kind": kind,
            "created_at": "2026-11-24T09:00:00+00:00", "decided_at": decided}


def ex(aid, status="SUCCEEDED", start="2026-11-24T09:00:00+00:00"):
    return {"name": aid, "status": status, "start": start, "arn": f"arn:aws:states:x:************:execution:a:{aid}"}


FRESH_ID = "11111111-2222-3333-4444-555555555555"


def fresh_smoke(**over):
    s = {"smoke_id": "SMOKE-20261124T090000Z-abcd1234", "environment": "pilot-ap-southeast-2", "release_sha": SHA,
         "deployed_code_sha256": ART, "facility_id": "FAC-JAI-01", "collected_utc": "2026-11-24T09:05:00+00:00",
         "all_pass": True, "checks": {k: True for k in SMOKE_OLD["checks"]},
         "executions": [{"approval_id": FRESH_ID, "execution_arn": "arn:…:" + FRESH_ID, "shipment_id": "SYN-SHP-FRESH001",
                         "appointment_id": "APT-F", "facility_id": "FAC-JAI-01"}]}
    s.update(over)
    return s


def live_shaped(owner_rows=True):
    """The pilot as it is: the 23 Nov smoke (73cac501, B), the two owner-confirmed tests (C), plus a fresh smoke (B)."""
    execs = [ex(B_OLD, start="2026-11-23T07:30:13+00:00"), ex(C1, start="2026-11-23T08:11:28+00:00"),
             ex(C2, start="2026-11-23T08:36:29+00:00"), ex(FRESH_ID)]
    recs = {B_OLD: row(B_OLD, shipment="SYN-SHP-2172ACB8", appt="APT-01c31ce02a444ad79d6f43d0fa874cc0",
                       kind="drill_staff", decided="2026-11-23T07:31:05+00:00"),
            FRESH_ID: row(FRESH_ID, shipment="SYN-SHP-FRESH001", appt="APT-F", kind="drill_staff")}
    if owner_rows:
        recs[C1] = row(C1, shipment="SYN-SHP-OWNER001", status="EXPIRED", by="SYSTEM", kind="system",
                       decided="2026-11-23T08:15:48+00:00",
                       appt="APT-2dc6e787f98848e8902afa8b4b173f7b", appt_status="EXPIRED")
        recs[C2] = row(C2, shipment="SYN-SHP-OWNER002", appt="APT-89027b09ab6449d1acdd5a51c6549bc5",
                       decided="2026-11-23T08:36:30+00:00")
    return execs, recs


BASE = {"live alias 'live' routes 100% to one published version": True,
        "published definition equals the repository ASL": True, "no execution has failed": True}


# Recovery Release A (check 1): the owner record's console requests, as the collector found them in the API log
VERIFIED = {str(e["approval_id"]): {"status": "verified"} for e in OWNER["executions"]}


def gate(execs, recs, smoke=None, owner=OWNER, functions=None, health=HEALTH, mode=MODE, ref=REF, smokes=None,
         api_log=VERIFIED):
    smoke = fresh_smoke() if smoke is None else smoke
    att = R.attribute_executions(execs, recs, (smokes if smokes is not None else [SMOKE_OLD]) + ([smoke] if smoke else []),
                                 owner, api_log)
    cfg = R.check_booking_config(functions or fns(), health, mode, ref)
    return R.booking_workflow_renewal(BASE, cfg, att, R.fresh_smoke_checks(smoke or None, SHA, ART, NOW), smoke or None)


# ------------------------------------------------------------------------------------------------ the pilot, valid
def test_the_live_shaped_pilot_passes_renewal_with_each_execution_in_its_category():
    g = gate(*live_shaped())
    assert g["status"] == "PASS", g["diagnostics"]
    s = g["evidence"]["attribution_summary"]
    assert s["B"] == sorted([B_OLD[:8], FRESH_ID[:8]]) and s["C"] == sorted([C1[:8], C2[:8]]) and not s["A"]
    assert not s["unresolved"] and not s["failed"] and g["not_shown"] == []
    old = next(v for v in g["evidence"]["approval_executions"] if v["approval_id"] == B_OLD)
    assert "SHA not recorded" in old["reasons"][0]                          # honest: the 23 Nov format had no SHA


def test_category_c_is_never_smoke_evidence_and_without_a_fresh_smoke_the_gate_fails():
    execs, recs = live_shaped()
    g = gate(execs[:3], recs, smoke={})                                      # no fresh smoke at all
    assert g["status"] == "FAIL"
    assert any("no fresh smoke test record" in d for d in g["diagnostics"])


# ------------------------------------------------------------------------------------------------ configuration
@pytest.mark.parametrize("over, words", [
    ({"setuhaul_pilot_api": {"booking": "false"}}, "setuhaul-pilot-api: booking is 'false'"),
    ({"setuhaul_pilot_agent": {"booking": "false"}}, "setuhaul-pilot-agent: booking is 'false'"),
    ({"setuhaul_pilot_outbound": {"booking": "true"}}, "setuhaul-pilot-outbound: booking is 'true', but only"),
    ({"setuhaul_pilot_recovery": {"booking": "TRUE?"}}, "unexpected value"),
    ({"setuhaul_pilot_agent": {"env": "prod"}}, "environment 'prod'"),
    ({"setuhaul_pilot_outbound": {"data_class": "real"}}, "data class 'real'"),
])
def test_booking_configuration_is_exact(over, words):
    execs, recs = live_shaped()
    g = gate(execs, recs, functions=fns(**over))
    assert g["status"] == "FAIL" and any(words in d for d in g["diagnostics"]), g["diagnostics"]


@pytest.mark.parametrize("health, mode, ref, words", [
    ({**HEALTH, "booking_kill_switch": "ENGAGED"}, MODE, REF, "kill switch is 'ENGAGED'"),
    ({**HEALTH, "environment": "production"}, MODE, REF, "/health environment"),
    ({**HEALTH, "booking_enabled": False}, MODE, REF, "booking_enabled False"),
    (HEALTH, MODE, "", "owner approval reference is missing"),
    (HEALTH, MODE, "APPROVED-BY-ME", "owner approval reference is missing or malformed"),
    (HEALTH, "OFF", REF, "deployed BookingMode is 'OFF'"),
])
def test_health_kill_switch_and_owner_approval(health, mode, ref, words):
    execs, recs = live_shaped()
    g = gate(execs, recs, health=health, mode=mode, ref=ref)
    assert g["status"] == "FAIL" and any(words in d for d in g["diagnostics"]), g["diagnostics"]


def test_an_engaged_kill_switch_can_be_the_expected_state_but_is_never_assumed():
    execs, recs = live_shaped()
    cfg = R.check_booking_config(fns(), {**HEALTH, "booking_kill_switch": "ENGAGED"}, MODE, REF, "ENGAGED")
    assert all(cfg[0].values())


# ------------------------------------------------------------------------------------------------ attribution
def verdict(g, aid):
    return next(v for v in g["evidence"]["approval_executions"] if v["approval_id"] == aid)


def test_a_documented_step6_drill_is_category_a():
    aid = str(uuid.uuid4())
    execs, recs = live_shaped()
    recs[aid] = row(aid, shipment="SYN-STEP6-0001", kind="drill_staff")
    g = gate(execs + [ex(aid)], recs)
    assert verdict(g, aid)["category"] == "A" and g["status"] == "PASS"


def test_an_execution_without_evidence_is_unresolved_and_named():
    aid = "deadbeef-0000-4000-8000-000000000000"
    execs, recs = live_shaped()
    recs[aid] = row(aid, shipment="SYN-SHP-ORPHAN01")
    g = gate(execs + [ex(aid)], recs)
    assert g["status"] == "UNRESOLVED" and verdict(g, aid)["verdict"] == "UNRESOLVED"
    assert any(d.startswith("BOOKING_WORKFLOW: UNRESOLVED — approval execution deadbeef has no attributable evidence")
               for d in g["diagnostics"])


def test_an_execution_whose_record_is_not_visible_is_unresolved():
    execs, recs = live_shaped(owner_rows=False)                             # e.g. 0029 not deployed, or out of scope
    g = gate(execs, recs)
    assert g["status"] == "UNRESOLVED"
    assert verdict(g, C1)["reasons"] == ["no approval record is visible for this execution under the pilot facility scope"]


@pytest.mark.parametrize("change, words", [
    ({"shipment_id": "SHP-REAL-0001", "shipment_is_synthetic": False}, "is not synthetic"),
    ({"facility_id": "FAC-DEL-09"}, "not a pilot facility"),
    ({"status": "OPEN"}, "still OPEN"),
    ({"appointment_status": "PENDING_CONFIRMATION"}, "APPROVED but the appointment is PENDING_CONFIRMATION"),
    ({"decided_by_type": "SYSTEM"}, "not a person"),
])
def test_a_record_that_contradicts_its_execution_fails(change, words):
    execs, recs = live_shaped()
    recs[FRESH_ID] = {**recs[FRESH_ID], **change}
    g = gate(execs, recs)
    assert g["status"] == "FAIL" and any(words in r for r in verdict(g, FRESH_ID)["reasons"]), verdict(g, FRESH_ID)


def test_a_failed_execution_fails():
    execs, recs = live_shaped()
    execs[-1] = ex(FRESH_ID, status="FAILED")
    g = gate(execs, recs)
    assert g["status"] == "FAIL" and "ended FAILED" in " ".join(verdict(g, FRESH_ID)["reasons"])


def test_smoke_evidence_for_the_wrong_facility_or_shipment_fails():
    execs, recs = live_shaped()
    bad = fresh_smoke(executions=[{**fresh_smoke()["executions"][0], "facility_id": "FAC-GGN-01"}])
    g = gate(execs, recs, smoke=bad)
    assert g["status"] == "FAIL" and "facility FAC-GGN-01" in " ".join(verdict(g, FRESH_ID)["reasons"])
    bad = fresh_smoke(executions=[{**fresh_smoke()["executions"][0], "shipment_id": "SYN-SHP-OTHER"}])
    assert "shipment SYN-SHP-OTHER" in " ".join(verdict(gate(execs, recs, smoke=bad), FRESH_ID)["reasons"])


def test_duplicate_evidence_fails():
    execs, recs = live_shaped()
    twin = fresh_smoke(smoke_id="SMOKE-TWIN")
    g = gate(execs, recs, smokes=[SMOKE_OLD, twin])
    assert g["status"] == "FAIL" and "attributed more than once" in " ".join(verdict(g, FRESH_ID)["reasons"])


def test_a_smoke_record_that_did_not_pass_cannot_attribute():
    execs, recs = live_shaped()
    old = copy.deepcopy(SMOKE_OLD)
    old["all_pass"] = False
    g = gate(execs, recs, smokes=[old])
    assert g["status"] == "FAIL" and "did not pass" in " ".join(verdict(g, B_OLD)["reasons"])


# ------------------------------------------------------------------------------------------------ category C limits
def test_category_c_covers_exactly_the_two_owner_confirmed_executions():
    assert R.OWNER_CONFIRMED_IDS == {C1, C2}
    got, problems = R.load_owner_attributions(OWNER)
    assert set(got) == {C1, C2} and not problems
    assert OWNER["category"] == "C" and OWNER["owner_confirmation"]["confirmed_by"] == "owner"
    for e in OWNER["executions"]:                                   # original references preserved
        assert e["cloudwatch"]["requests"] and e["step_functions"]["started_utc"] and e["execution_arn"].endswith(e["approval_id"])
    assert [r["line"].split()[-1] for r in OWNER["executions"][0]["cloudwatch"]["requests"]] == ["500"] * 5
    assert [r["line"].split()[-1] for r in OWNER["executions"][1]["cloudwatch"]["requests"]] == ["200"]


def test_a_third_execution_in_the_owner_record_rejects_the_whole_record():
    rec = copy.deepcopy(OWNER)
    aid = "deadbeef-0000-4000-8000-000000000001"
    rec["executions"].append({**rec["executions"][1], "approval_id": aid})
    got, problems = R.load_owner_attributions(rec)
    assert got == {} and any("outside the two executions the owner confirmed" in p for p in problems)
    execs, recs = live_shaped()
    recs[aid] = row(aid, shipment="SYN-SHP-SNEAK001")
    g = gate(execs + [ex(aid)], recs, owner=rec)
    assert g["status"] == "FAIL"                                    # record invalid: nothing is attributed by it
    assert verdict(g, C1)["verdict"] == "UNRESOLVED" and verdict(g, aid)["verdict"] == "UNRESOLVED"


def test_an_owner_record_without_the_owner_confirmation_is_rejected():
    rec = copy.deepcopy(OWNER)
    rec["owner_confirmation"] = {}
    assert R.load_owner_attributions(rec)[0] == {}


@pytest.mark.parametrize("aid, change, words", [
    (C1, {"status": "APPROVED", "decided_by_type": "HUMAN", "appointment_status": "CONFIRMED"}, "not one of ['EXPIRED']"),
    (C1, {"appointment_id": "APT-other"}, "is not the one in the owner's record"),
    (C2, {"decided_at": "2026-11-23T09:30:00+00:00"}, "outside the recorded window"),
    (C2, {"decider_kind": "drill_staff"}, "decider kind 'drill_staff'"),
    (C2, {"decided_by_type": "SYSTEM"}, "not a person"),
])
def test_category_c_is_verified_against_the_database_record(aid, change, words):
    execs, recs = live_shaped()
    recs[aid] = {**recs[aid], **change}
    g = gate(execs, recs)
    assert g["status"] == "FAIL" and any(words in r for r in verdict(g, aid)["reasons"]), verdict(g, aid)


def test_category_c_checks_the_step_functions_record_too():
    execs, recs = live_shaped()
    execs[1] = ex(C1, start="2026-11-23T08:20:00+00:00")
    g = gate(execs, recs)
    assert g["status"] == "FAIL" and "the record says 2026-11-23T08:11:28Z" in " ".join(verdict(g, C1)["reasons"])


# ------------------------------------------------------------------------------------------------ fresh smoke binding
@pytest.mark.parametrize("over, words", [
    ({"release_sha": "1d304f874e4f4443fc565f010050a055528c930b"}, "not the target"),
    ({"deployed_code_sha256": "0" * 64}, "against the deployed code"),
    ({"environment": "staging"}, "on this environment"),
    ({"collected_utc": "2026-11-22T09:00:00+00:00"}, "is fresh"),
    ({"all_pass": False}, "every smoke check passed"),
    ({"checks": {k: True for k in list(SMOKE_OLD["checks"])[:-1]}}, "missing checks 6"),
    ({"smoke_id": None}, "renewal format"),
])
def test_the_fresh_smoke_must_be_bound_to_the_release_and_deployment(over, words):
    execs, recs = live_shaped()
    g = gate(execs, recs, smoke=fresh_smoke(**over))
    assert g["status"] == "FAIL" and any(words in d for d in g["diagnostics"]), g["diagnostics"]


def test_the_lambda_code_hash_converts_to_the_manifest_form():
    raw = hashlib.sha256(b"zip-bytes").digest()
    assert R.code_sha256_hex(base64.b64encode(raw).decode()) == raw.hex()


# ------------------------------------------------------------------------------------------------ statuses
def test_the_approved_exceptions_keep_their_scope_in_renewal_wording():
    ok = {"checks": {"a": True}}
    assert R.renewal_status("AURORA", {**ok, "status": "PASS WITH PILOT EXCEPTION (T33-P)"}) == R.PASS_EXC
    assert R.renewal_status("WORKLOAD_IDENTITY", {**ok, "status": "PARTIAL", "not_shown": ["drift"]}) == R.PARTIAL_EXC
    assert R.renewal_status("OUTBOUND_DELIVERY", {"checks": {}, "status": "BLOCKED"}) == R.BLOCKED_EXC
    assert R.renewal_status("AWS_QUEUE", {**ok, "status": "PASS"}) == R.PASS
    assert R.renewal_status("AWS_QUEUE", {**ok, "status": "PARTIAL", "not_shown": ["x"]}) == R.FAIL   # no exception here
    assert R.renewal_status("AURORA", {"checks": {"a": False}, "status": "PASS WITH PILOT EXCEPTION (T33-P)"}) == R.FAIL
    assert R.renewal_status("OUTBOUND_DELIVERY", {"checks": {}, "status": "PARTIAL"}) == R.FAIL
    assert set(R.STATUSES) == {"PASS", "PASS WITH APPROVED EXCEPTION", "PARTIAL WITH APPROVED EXCEPTION",
                               "BLOCKED WITH APPROVED EXCEPTION", "FAIL", "UNRESOLVED"}


# ------------------------------------------------------------------------------------------------ writing evidence
def manifest(art=ART):
    from setuhaul.api.inbound import MANIFEST_GATES
    m = {"environment": "pilot-ap-southeast-2", "gates": {}}
    for gname, keys in MANIFEST_GATES.items():
        m["gates"][gname] = {k: (art if k == "artifact_sha256" else ("a" * 64 if k.endswith("sha256") else "x"))
                             for k in keys}
    m["gates"]["RECOVERY_WORKER"]["artifact_sha256"] = art
    return m


def renewal_report(**over):
    execs, recs = live_shaped()
    bw = gate(execs, recs)
    other = {"checks": {"c": True}, "not_shown": [], "status": "PASS", "evidence": {}}
    r = {"mode": "renewal", "booking_enabled": True, "environment": "pilot-ap-southeast-2", "release_sha": SHA,
         "booking_observed": {"source": "https://api.example/health", "observed_utc": datetime.now(timezone.utc).isoformat(),
                              "http_status": 200, "booking_state": "authorized", "booking_enabled": True},
         "approval_ref": REF, "artifact_sha256": ART, "collected_utc": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
         "gates": {"AWS_QUEUE": other, "RECOVERY_WORKER": other, "BOOKING_WORKFLOW": bw,
                   "AURORA": {**other, "status": "PASS WITH PILOT EXCEPTION (T33-P)"},
                   "WORKLOAD_IDENTITY": {**other, "status": "PARTIAL", "not_shown": ["live drift detection (AWS Config)"]},
                   "OUTBOUND_DELIVERY": {"checks": {}, "status": "BLOCKED", "not_shown": ["meta"]}}}
    r.update(over)
    return r


FPS = {g: "f" * 64 for g in ("AWS_QUEUE", "RECOVERY_WORKER", "BOOKING_WORKFLOW", "AURORA", "WORKLOAD_IDENTITY",
                             "OUTBOUND_DELIVERY")}


def test_a_valid_renewal_report_becomes_evidence_for_the_renewed_gates():
    rows, refused = rows_from_report(renewal_report(), manifest(), "pilot-ap-southeast-2", "https://x/y.json", b"{}", FPS)
    assert {r["gate"] for r in rows} == {"AWS_QUEUE", "RECOVERY_WORKER", "BOOKING_WORKFLOW"}
    assert set(refused) == {"AURORA", "WORKLOAD_IDENTITY", "OUTBOUND_DELIVERY"}       # exceptions, never evidence


@pytest.mark.parametrize("over, words", [
    ({"mode": None}, "booking switch off"),                                             # activation rule, unchanged
    ({"artifact_sha256": "0" * 64}, "different code than this deployment"),
    ({"collected_utc": "2026-11-01T00:00:00+00:00"}, "not fresh"),
    ({"environment": "prod"}, "is for 'prod'"),
    ({"release_sha": "1d304f874e4f4443fc565f010050a055528c930b"}, "not bound to the report's release SHA"),
])
def test_a_renewal_report_is_refused_unless_bound_and_fresh(over, words):
    with pytest.raises(EvidenceRefused, match=words):
        rows_from_report(renewal_report(**over), manifest(), "pilot-ap-southeast-2", "https://x/y.json", b"{}", FPS)


def test_a_renewal_report_whose_booking_workflow_did_not_pass_is_refused():
    r = renewal_report()
    r["gates"]["BOOKING_WORKFLOW"] = {**r["gates"]["BOOKING_WORKFLOW"], "status": "UNRESOLVED"}
    with pytest.raises(EvidenceRefused, match="BOOKING_WORKFLOW is 'UNRESOLVED'"):
        rows_from_report(r, manifest(), "pilot-ap-southeast-2", "https://x/y.json", b"{}", FPS)


def test_the_renewal_approval_must_be_the_one_this_run_uses():
    with pytest.raises(EvidenceRefused, match="names approval"):
        activate(None, renewal_report(), b"{}", manifest(), "pilot-ap-southeast-2", "https://x/y.json",
                 "OWNER-APPROVAL-20261124-SOMETHING-ELSE", FPS, 170009, {})


def test_an_activation_report_still_must_be_booking_off():
    with pytest.raises(EvidenceRefused, match="booking switch off"):
        rows_from_report({"booking_enabled": True, "gates": {}}, manifest(), "pilot-ap-southeast-2", "https://x/y.json",
                         b"{}", FPS)


def test_the_activation_collector_is_unchanged_and_renewal_is_opt_in():
    src = (ROOT / "scripts" / "step6_gate_evidence.py").read_text(encoding="utf-8")
    assert '"every execution is an owner-approved Step 6 drill request (SYN-STEP6-*)": all(' in src
    assert '"booking switch false on every function": all(f["booking"] == "false"' in src
    assert 'default="activation"' in src and "renewal_gate(" in src
    wf = (ROOT / ".github" / "workflows" / "activate-booking.yml").read_text(encoding="utf-8")
    assert "default: activation" in wf and "activation) REPORT=docs/release/evidence/step6_gates.json" in wf
    assert "renewal) REPORT=docs/release/evidence/renewal_gates.json" in wf


def test_the_smoke_test_is_release_bound_and_never_overwrites_the_23_nov_record():
    src = (ROOT / "scripts" / "smoke_option_d.py").read_text(encoding="utf-8")
    assert '"--release-sha", required=True' in src and '"evidence" / "smoke" /' in src
    assert '"smoke_id": smoke_id' in src and '"executions": executions' in src


# ------------------------------------------------------------------------------------------------ migration 0029 (RLS)
COLS = ["approval_id", "kind", "facility_id", "shipment_id", "shipment_is_synthetic", "appointment_id",
        "appointment_status", "status", "decided_by_type", "decided_by_role", "decider_kind", "outcome_reason",
        "created_at", "decided_at", "expires_at", "task_token_recorded"]


@pytest.fixture(scope="module")
def approvals(dsn):
    """Two approvals in JAI (one decided by a drill identity, one by a console user) and one in GGN, written as the
    test superuser."""
    u = uuid.uuid4().hex[:8]
    ids = {"jai_drill": str(uuid.uuid4()), "jai_console": str(uuid.uuid4()), "jai_expired": str(uuid.uuid4()),
           "ggn": str(uuid.uuid4())}
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET search_path = setuhaul")
        c.execute("INSERT INTO drivers (driver_id,carrier_id,driver_name,phone_e164) VALUES (%s,'CAR1','t',%s)",
                  (f"SYN-DRV-{u}", f"+999{uuid.uuid4().int % 10**9:09d}"))
        for i, (key, fac, dock, who, st, typ, appt_st) in enumerate([
                ("jai_drill", "FAC-JAI-01", "DOCK-JAI-D1", "staff:drill-planner", "APPROVED", "HUMAN", "CONFIRMED"),
                ("jai_console", "FAC-JAI-01", "DOCK-JAI-D2", "planner@example.test", "APPROVED", "HUMAN", "CONFIRMED"),
                ("jai_expired", "FAC-JAI-01", "DOCK-JAI-D3", "system:approval-workflow", "EXPIRED", "SYSTEM", "EXPIRED"),
                ("ggn", "FAC-GGN-01", "DOCK-GGN-D1", "staff:ggn", "APPROVED", "HUMAN", "CONFIRMED")]):
            sid, veh, apt = f"SYN-SHP-{u}{i}", f"SYN-VEH-{u}{i}", f"APT-{u}{i}"
            c.execute("INSERT INTO vehicles VALUES (%s,'CAR1','32FT',%s,20000,false)", (veh, f"REG-{u}{i}"))
            c.execute("""INSERT INTO shipments (shipment_id,order_reference,carrier_id,driver_id,vehicle_id,
                         destination_facility_id,load_weight_kg,expected_unload_min,planned_departure_ts,original_eta_ts,
                         current_status) VALUES (%s,%s,'CAR1',%s,%s,%s,10000,45,now()-interval '3 hours',
                         now()+interval '2 hours','IN_TRANSIT')""", (sid, f"ORD-{u}{i}", f"SYN-DRV-{u}", veh, fac))
            c.execute("""INSERT INTO appointments (appointment_id,shipment_id,dock_id,during,status,booking_source)
                         VALUES (%s,%s,%s,tstzrange(now()+make_interval(days=>%s+30),now()+make_interval(days=>%s+30)
                         +interval '1 hour'),%s,'DRIVER_CHAT')""", (apt, sid, dock, i, i, appt_st))
            c.execute("""INSERT INTO approval_requests (approval_id,kind,facility_id,shipment_id,proposed_action,evidence,
                         status,expires_at,decided_by,decided_at,decision_note,task_token,appointment_id,decided_by_type,
                         decided_by_role,outcome_reason) VALUES (%s,'STANDARD',%s,%s,'{"secret":"proposal"}','{"x":1}',
                         %s,now()+interval '1 hour',%s,now(),'private note','tok',%s,%s,'planner',%s)""",
                      (ids[key], fac, sid, st, who, apt, typ, st))
    return ids


def call(dsn, role, scope, ids):
    with psycopg.connect(dsn) as c:
        c.execute(f"SET ROLE {role}")
        if scope is not None:
            c.execute("SELECT set_config('app.facility_scope', %s, true)", (scope,))
        cur = c.execute("SELECT * FROM setuhaul.approval_execution_evidence(%s::uuid[])", (list(ids),))
        return [d.name for d in cur.description], cur.fetchall()


def test_0029_the_evidence_identity_reads_only_its_facility_scope_and_only_the_needed_fields(dsn, approvals):
    names, rows = call(dsn, "setuhaul_deploy_evidence", "FAC-JAI-01", approvals.values())
    assert names == COLS
    got = {str(r[0]): dict(zip(names, r)) for r in rows}
    assert set(got) == {approvals[k] for k in ("jai_drill", "jai_console", "jai_expired")}   # GGN out of scope: absent
    assert got[approvals["jai_drill"]]["decider_kind"] == "drill_staff"
    assert got[approvals["jai_console"]]["decider_kind"] == "console_user"          # the person's id is never returned
    assert got[approvals["jai_expired"]]["decider_kind"] == "system"
    assert got[approvals["jai_expired"]]["status"] == "EXPIRED" == got[approvals["jai_expired"]]["appointment_status"]
    blob = json.dumps([list(map(str, r)) for r in rows])
    assert "example.test" not in blob and "private note" not in blob and "proposal" not in blob
    assert "system:approval-workflow" not in blob and "'tok'" not in blob
    assert all(g["shipment_is_synthetic"] and g["task_token_recorded"] is True for g in got.values())
    _, both = call(dsn, "setuhaul_deploy_evidence", "FAC-JAI-01,FAC-GGN-01", approvals.values())
    assert len(both) == 4


def test_0029_without_a_facility_scope_is_an_error_not_an_empty_answer(dsn, approvals):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="no facility scope"):
        call(dsn, "setuhaul_deploy_evidence", None, approvals.values())
    with pytest.raises(psycopg.errors.InvalidParameterValue, match="between 1 and 200"):
        call(dsn, "setuhaul_deploy_evidence", "FAC-JAI-01", [str(uuid.uuid4()) for _ in range(201)])


@pytest.mark.parametrize("role", ["setuhaul_console", "setuhaul_agent", "setuhaul_system", "setuhaul_ingest",
                                  "setuhaul_feasibility"])
def test_0029_no_application_role_may_call_it(dsn, approvals, role):
    with psycopg.connect(dsn) as c:
        exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    if not exists:
        pytest.skip(f"{role} does not exist in this schema")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        call(dsn, role, "FAC-JAI-01", approvals.values())


def test_0029_is_narrow_by_construction_and_rls_is_unchanged(dsn, approvals):
    with psycopg.connect(dsn) as c:
        f = c.execute("""SELECT p.prosecdef, p.provolatile, pg_get_userbyid(p.proowner), p.proconfig,
                                has_function_privilege('public', p.oid, 'EXECUTE')
                           FROM pg_proc p WHERE p.proname = 'approval_execution_evidence'""").fetchone()
        assert f[0] is True and f[1] == "s" and f[2] == "setuhaul_rpc" and f[4] is False
        assert any(x.startswith("search_path=") for x in f[3])
        rpc = c.execute("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname='setuhaul_rpc'").fetchone()
        assert rpc == (False, False)                                               # RLS applies inside the function
        rls = c.execute("""SELECT relrowsecurity, relforcerowsecurity FROM pg_class
                            WHERE oid = 'setuhaul.approval_requests'::regclass""").fetchone()
        assert rls == (True, True)
        # the evidence identity still cannot read the table itself, and gained no write
        assert c.execute("SELECT has_table_privilege('setuhaul_deploy_evidence','setuhaul.approval_requests','SELECT')"
                         ).fetchone()[0] is False
        assert c.execute("SELECT has_table_privilege('setuhaul_deploy_evidence','setuhaul.appointments','SELECT')"
                         ).fetchone()[0] is False
    with psycopg.connect(dsn) as c:                                                # console RLS behaves as before
        c.execute("SET ROLE setuhaul_console")
        c.execute("SELECT set_config('app.facility_scope', 'FAC-GGN-01', true)")
        seen = {str(r[0]) for r in c.execute("SELECT approval_id FROM setuhaul.approval_requests").fetchall()}
        assert approvals["ggn"] in seen and not {approvals[k] for k in ("jai_drill", "jai_console", "jai_expired")} & seen


# ------------------------------------------------------------------------------------------------ Recovery A, check 1
@pytest.mark.parametrize("corroboration", [None, {}, {"status": "missing"}, {"status": "not checked"},
                                           {"status": "beyond_retention_unverified"}])
def test_an_owner_record_attributes_nothing_unless_the_api_log_corroborates_it(corroboration):
    """The owner record and the database row alone are not enough: without the API log's own lines (or an earlier
    committed verification once the log has expired) the owner-confirmed execution is a FAIL, never category C."""
    api_log = None if corroboration is None else {**VERIFIED, C2: corroboration} if corroboration else {}
    g = gate(*live_shaped(), api_log=api_log)
    v = {x["approval_id"]: x for x in g["evidence"]["approval_executions"]}
    assert v[C2]["verdict"] == "FAIL" and v[C2]["category"] is None
    assert any("API log does not corroborate" in r for r in v[C2]["reasons"])
    assert g["status"] == "FAIL"


def test_a_previously_verified_corroboration_is_accepted_after_the_log_expires():
    g = gate(*live_shaped(), api_log={**VERIFIED, C2: {"status": "previously_verified"}})
    v = {x["approval_id"]: x for x in g["evidence"]["approval_executions"]}
    assert v[C2]["verdict"] == "ATTRIBUTED" and v[C2]["category"] == "C"


# ------------------------------------------------------------------------------------------------ category D
# Owner-confirmed test origin of an execution started by a FAILED smoke (26 Nov 2026, check 7 defect).
D_ID = "3c3ff424-5d40-4033-bfd2-28f1837fa7c5"
ORIGIN = json.loads((EVID / "owner_test_origin_20261126.json").read_text(encoding="utf-8"))
FAILED_SMOKE = json.loads((EVID / "smoke" / "option_d_smoke_20261126T040450Z.json").read_text(encoding="utf-8"))


def with_failed_smoke(origin=ORIGIN, smoke_over=None, row_over=None, smokes_extra=()):
    execs, recs = live_shaped()
    execs.append(ex(D_ID, start="2026-11-26T04:06:26+00:00"))
    recs[D_ID] = {**row(D_ID, shipment="SYN-SHP-DDD13851", appt="APT-af0e9d8a19174cd7baf0ea7e7a39d139",
                        kind="drill_staff", decided="2026-11-26T04:06:53+00:00"), **(row_over or {})}
    failed = {**copy.deepcopy(FAILED_SMOKE), **(smoke_over or {})}
    att = R.attribute_executions(execs, recs, [SMOKE_OLD, failed, *smokes_extra, fresh_smoke()], OWNER, VERIFIED,
                                 origin)
    cfg = R.check_booking_config(fns(), HEALTH, MODE, REF)
    g = R.booking_workflow_renewal(BASE, cfg, att, R.fresh_smoke_checks(fresh_smoke(), SHA, ART, NOW), fresh_smoke())
    return g, {v["approval_id"]: v for v in att[0]}


def test_the_committed_failed_smoke_record_is_kept_as_failed():
    assert FAILED_SMOKE["all_pass"] is False and FAILED_SMOKE["smoke_id"] == ORIGIN["executions"][0]["smoke_id"]
    assert [k for k, ok in FAILED_SMOKE["checks"].items() if not ok] == [
        "7 isolation: only synthetic identities; no delivery outside the test sink"]
    assert R.OWNER_TEST_ORIGIN_IDS == {D_ID, "a95aaaa1-b515-466a-a628-54214b801519"}   # 27 Nov: owner confirmed (DEMO-02)


def test_a_failed_test_booking_with_the_owner_test_origin_record_is_category_d_not_a_passing_smoke():
    g, v = with_failed_smoke()
    assert g["status"] == "PASS", g["diagnostics"]
    assert v[D_ID]["category"] == "D" and v[D_ID]["verdict"] == "ATTRIBUTED"
    assert "smoke outcome FAIL" in v[D_ID]["reasons"][0] and "never smoke evidence" in v[D_ID]["reasons"][0]
    s = g["evidence"]["attribution_summary"]
    assert s["D"] == ["3c3ff424"] and "3c3ff424" not in s["B"]


def test_a_failed_test_booking_without_the_owner_record_still_fails():
    g, v = with_failed_smoke(origin=None)
    assert g["status"] == "FAIL" and v[D_ID]["verdict"] == "FAIL"
    assert "the smoke record that names it did not pass" in v[D_ID]["reasons"]


def test_category_d_never_satisfies_the_fresh_smoke_requirement():
    execs, recs = live_shaped()
    execs = [e for e in execs if e["name"] != FRESH_ID] + [ex(D_ID, start="2026-11-26T04:06:26+00:00")]
    recs.pop(FRESH_ID)
    recs[D_ID] = row(D_ID, shipment="SYN-SHP-DDD13851", appt="APT-af0e9d8a19174cd7baf0ea7e7a39d139", kind="drill_staff")
    att = R.attribute_executions(execs, recs, [SMOKE_OLD, FAILED_SMOKE], OWNER, VERIFIED, ORIGIN)
    cfg = R.check_booking_config(fns(), HEALTH, MODE, REF)
    g = R.booking_workflow_renewal(BASE, cfg, att, R.fresh_smoke_checks(FAILED_SMOKE, SHA, ART, NOW), FAILED_SMOKE)
    assert g["status"] == "FAIL"
    assert not g["checks"]["smoke: the fresh smoke's own executions are attributed to it (category B)"]


def test_an_execution_outside_the_confirmed_ids_cannot_use_category_d():
    other = "99999999-0000-0000-0000-000000000001"
    origin = copy.deepcopy(ORIGIN)
    origin["executions"][0]["approval_id"] = other
    g, v = with_failed_smoke(origin=origin)
    assert g["status"] == "FAIL" and v[D_ID]["verdict"] == "FAIL"
    assert not g["checks"]["owner test-origin record is valid and limited to its confirmed executions (category D)"]


@pytest.mark.parametrize("mutate, why", [
    (lambda o: o.pop("owner_confirmation"), "no owner confirmation"),
    (lambda o: o.update(category="C"), "not category D"),
    (lambda o: o.update(environment="production"), "not 'pilot"),
])
def test_an_invalid_test_origin_record_attributes_nothing(mutate, why):
    origin = copy.deepcopy(ORIGIN)
    mutate(origin)
    g, v = with_failed_smoke(origin=origin)
    assert g["status"] == "FAIL" and v[D_ID]["verdict"] == "FAIL"
    assert any(why in d for d in g["diagnostics"]), g["diagnostics"]


@pytest.mark.parametrize("smoke_over, row_over, why", [
    ({"smoke_id": "SMOKE-OTHER"}, None, "is not the one the owner named"),
    ({"environment": "production"}, None, "failed smoke record is for"),
    ({"release_sha": None}, None, "not in the renewal format"),
    (None, {"shipment_id": "SYN-SHP-ELSE0001"}, "shipment"),
    (None, {"appointment_id": "APT-ELSE"}, "appointment"),
])
def test_category_d_fails_closed_when_the_records_disagree(smoke_over, row_over, why):
    g, v = with_failed_smoke(smoke_over=smoke_over, row_over=row_over)
    assert g["status"] == "FAIL" and v[D_ID]["verdict"] == "FAIL"
    assert any(why in r for r in v[D_ID]["reasons"]), v[D_ID]["reasons"]


def test_category_d_keeps_the_hard_checks_for_a_failed_execution_and_a_non_synthetic_shipment():
    g, v = with_failed_smoke(row_over={"shipment_id": "SHP-REAL-0001", "shipment_is_synthetic": False})
    assert v[D_ID]["verdict"] == "FAIL" and any("not synthetic" in r for r in v[D_ID]["reasons"])


def test_the_owner_record_without_a_failed_smoke_naming_it_fails():
    execs, recs = live_shaped()
    execs.append(ex(D_ID, start="2026-11-26T04:06:26+00:00"))
    recs[D_ID] = row(D_ID, shipment="SYN-SHP-DDD13851", appt="APT-af0e9d8a19174cd7baf0ea7e7a39d139", kind="drill_staff")
    verdicts, _ = R.attribute_executions(execs, recs, [SMOKE_OLD, fresh_smoke()], OWNER, VERIFIED, ORIGIN)
    v = {x["approval_id"]: x for x in verdicts}[D_ID]
    assert v["verdict"] == "FAIL" and any("no committed failed smoke record names it" in r for r in v["reasons"])


def test_a_successful_test_booking_is_still_category_b_and_a_genuine_operational_booking_is_unaccounted():
    real = "77777777-0000-0000-0000-000000000002"
    g, v = with_failed_smoke()
    assert v[FRESH_ID]["category"] == "B"                                     # successful test booking: unchanged
    execs, recs = live_shaped()
    execs.append(ex(real))
    recs[real] = row(real, shipment="SYN-SHP-OPS00001", appt="APT-OPS", kind="console_user")
    verdicts, _ = R.attribute_executions(execs, recs, [SMOKE_OLD, FAILED_SMOKE, fresh_smoke()], OWNER, VERIFIED, ORIGIN)
    o = {x["approval_id"]: x for x in verdicts}[real]
    assert o["verdict"] == "UNRESOLVED" and o["category"] is None             # no evidence: never D by default


# ------------------------------------------------------------------------------------------------ Category E
E1, E2 = "453358af-04ae-4f8e-907e-543498da963c", "251b0c87-a097-4814-8690-e16d5ad88ede"
DRIVERS = json.loads((EVID / "owner_test_drivers_20261126.json").read_text(encoding="utf-8"))


def e_row(aid, **over):
    return {**row(aid, shipment="SYN-SHP-TEST-1", appt="APT-" + aid[:8], kind="console_user",
                  decided="2026-11-26T07:57:34+00:00"), "created_at": "2026-11-26T07:56:35+00:00", **over}


def with_test_drivers(record=DRIVERS, rows_over=None):
    execs, recs = with_failed_smoke_inputs()
    for aid in (E1, E2):
        execs.append(ex(aid, start="2026-11-26T07:56:35+00:00"))
        recs[aid] = e_row(aid, **(rows_over or {}))
    att = R.attribute_executions(execs, recs, [SMOKE_OLD, FAILED_SMOKE, fresh_smoke()], OWNER, VERIFIED, ORIGIN, record)
    cfg = R.check_booking_config(fns(), HEALTH, MODE, REF)
    g = R.booking_workflow_renewal(BASE, cfg, att, R.fresh_smoke_checks(fresh_smoke(), SHA, ART, NOW), fresh_smoke())
    return g, {v["approval_id"]: v for v in att[0]}


def with_failed_smoke_inputs():
    execs, recs = live_shaped()
    execs.append(ex(D_ID, start="2026-11-26T04:06:26+00:00"))
    recs[D_ID] = row(D_ID, shipment="SYN-SHP-DDD13851", appt="APT-af0e9d8a19174cd7baf0ea7e7a39d139",
                     kind="drill_staff", decided="2026-11-26T04:06:53+00:00")
    return execs, recs


def test_the_committed_test_driver_record_states_exactly_the_coded_rule():
    ok, problems = R.load_test_driver_rule(DRIVERS)
    assert ok and not problems
    assert {e["approval_id"] for e in DRIVERS["confirmed_executions_at_record_time"]} == {E1, E2}


def test_console_approved_test_driver_bookings_are_category_e_and_the_gate_passes():
    g, v = with_test_drivers()
    assert g["status"] == "PASS", g["diagnostics"]
    assert v[E1]["category"] == v[E2]["category"] == "E"
    assert g["evidence"]["attribution_summary"]["E"] == ["251b0c87", "453358af"]
    assert g["checks"]["owner test-driver rule record is valid (category E)"]


def test_without_the_owner_record_test_driver_bookings_stay_unresolved():
    g, v = with_test_drivers(record=None)
    assert g["status"] == "UNRESOLVED" and v[E1]["verdict"] == "UNRESOLVED"


def test_category_e_never_satisfies_the_fresh_smoke_requirement():
    execs = [ex(E1, start="2026-11-26T07:56:35+00:00")]
    att = R.attribute_executions(execs, {E1: e_row(E1)}, [], None, None, None, DRIVERS)
    cfg = R.check_booking_config(fns(), HEALTH, MODE, REF)
    g = R.booking_workflow_renewal(BASE, cfg, att, R.fresh_smoke_checks(None, SHA, ART, NOW), None)
    assert att[0][0]["category"] == "E" and g["status"] == "FAIL"
    assert not g["checks"]["smoke: the fresh smoke's own executions are attributed to it (category B)"]


@pytest.mark.parametrize("over, why", [
    ({"status": "REJECTED", "appointment_status": "CANCELLED"}, "covers only APPROVED"),
    ({"decider_kind": "drill_staff"}, "not the hosted console"),
    ({"decided_by_role": "ops_lead"}, "not a human planner"),
    ({"created_at": "2026-11-25T23:59:00+00:00"}, "before the owner's rule"),
])
def test_test_driver_bookings_outside_the_rule_are_not_category_e(over, why):
    g, v = with_test_drivers(rows_over=over)
    assert v[E1]["category"] is None and v[E1]["verdict"] in ("UNRESOLVED", "FAIL")
    assert any(why in r for r in v[E1]["reasons"]), v[E1]["reasons"]
    assert g["status"] != "PASS"


def test_category_e_needs_a_test_driver_shipment_and_keeps_the_hard_checks():
    g, v = with_test_drivers(rows_over={"shipment_id": "SYN-SHP-OPS00001"})
    assert v[E1]["verdict"] == "UNRESOLVED"
    g, v = with_test_drivers(rows_over={"facility_id": "FAC-BLR-09"})
    assert v[E1]["verdict"] == "FAIL" and any("not a pilot facility" in r for r in v[E1]["reasons"])


@pytest.mark.parametrize("mutate, why", [
    (lambda o: o.pop("owner_confirmation"), "no owner confirmation"),
    (lambda o: o.update(category="D"), "not category E"),
    (lambda o: o.update(environment="production"), "not 'pilot"),
    (lambda o: o["rule"].update(shipment_pattern="SYN-SHP-.*"), "shipment pattern"),
    (lambda o: o["rule"].update(authorized_drivers=["SYN-DRV-TEST-1", "SYN-DRV-TEST-3"]), "driver allow-list"),
    (lambda o: o["rule"].update(telegram_live_approval="OWNER-APPROVAL-20261126-OTHER"), "Telegram-live approval"),
])
def test_an_invalid_test_driver_record_attributes_nothing(mutate, why):
    rec = copy.deepcopy(DRIVERS)
    mutate(rec)
    g, v = with_test_drivers(record=rec)
    assert g["status"] == "FAIL" and v[E1]["category"] is None
    assert not g["checks"]["owner test-driver rule record is valid (category E)"]
    assert any(why in d for d in g["diagnostics"]), g["diagnostics"]
