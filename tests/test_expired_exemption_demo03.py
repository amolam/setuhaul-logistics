"""27 Nov 2026 (owner, DEMO-03): Category X, the exemption for expired, never-confirmed booking requests, and the
smoke test's credential check before any driver message."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from smoke_option_d import aws_credentials_ok  # noqa: E402
from setuhaul.ops import renewal as R  # noqa: E402

AID = "3cba692b-0000-4000-8000-000000000001"


def _row(**over):
    r = {"approval_id": AID, "kind": "STANDARD", "facility_id": "FAC-JAI-01", "shipment_id": "SYN-SHP-09C4CA0D",
         "shipment_is_synthetic": True, "appointment_id": "APT-31eac2ce", "appointment_status": "EXPIRED",
         "status": "EXPIRED", "decided_by_type": "SYSTEM", "decided_by_role": "system", "decider_kind": "system",  # as live
         "created_at": "2026-11-27T08:35:46+00:00", "decided_at": "2026-11-27T08:40:56+00:00"}
    return {**r, **over}


SAFE = {"active_claims": 0, "confirmed": False}


def _verdict(row=None, facts=SAFE, status="SUCCEEDED"):
    execs = [{"name": AID, "status": status, "start": "2026-11-27T08:35:46+00:00", "arn": "arn:x"}]
    out, _ = R.attribute_executions(execs, {AID: row or _row()}, [], None, None, None, None,
                                    None if facts is None else {AID: facts})
    return out[0]


def test_an_expired_never_confirmed_request_with_no_dock_time_is_exempt():
    v = _verdict()
    assert (v["verdict"], v["category"]) == ("ATTRIBUTED", "X"), v


@pytest.mark.parametrize("row_over,facts,status", [
    ({"status": "APPROVED", "decided_by_type": "HUMAN", "decided_by_role": "planner", "decider_kind": "console_user",
      "appointment_status": "CONFIRMED"}, SAFE, "SUCCEEDED"),                    # a confirmed booking
    ({"status": "REJECTED"}, SAFE, "SUCCEEDED"),                                 # decided another way
    ({"decided_by_type": "HUMAN", "decider_kind": "drill_staff"}, SAFE, "SUCCEEDED"),   # a person expired it
    ({"decided_by_role": "planner"}, SAFE, "SUCCEEDED"),
    ({"appointment_status": "CONFIRMED"}, SAFE, "SUCCEEDED"),
    ({}, {"active_claims": 1, "confirmed": False}, "SUCCEEDED"),                # dock time still held
    ({}, {"active_claims": 0, "confirmed": True}, "SUCCEEDED"),                 # was confirmed / is current
    ({}, None, "SUCCEEDED"),                                                     # no live read: nothing is exempt
    ({}, SAFE, "RUNNING"),                                                       # execution not finished
])
def test_anything_else_stays_blocking(row_over, facts, status):
    v = _verdict(_row(**row_over), facts, status)
    assert v["category"] != "X" and v["verdict"] != "ATTRIBUTED", v


def test_hard_checks_still_apply_to_an_expired_request():
    v = _verdict(_row(shipment_id="SHP-REAL-1", shipment_is_synthetic=False))
    assert v["verdict"] == R.FAIL


class _Creds:
    def __init__(self, exp):
        self._expiry_time = exp


class _Sess:
    def __init__(self, creds, works=True):
        self.creds, self.works = creds, works

    def get_credentials(self):
        return self.creds

    def client(self, *_a, **_k):
        works = self.works

        class C:
            def get_caller_identity(self):
                if not works:
                    raise RuntimeError("ExpiredToken")
                return {}
        return C()


NOW = datetime(2026, 11, 27, 8, 34, tzinfo=timezone.utc)


def test_the_smoke_refuses_to_start_on_missing_expiring_or_broken_credentials():
    for sess in (_Sess(None), _Sess(_Creds(NOW + timedelta(minutes=5))), _Sess(_Creds(NOW + timedelta(hours=1)), False)):
        with pytest.raises(SystemExit, match="REFUSED before any driver message"):
            aws_credentials_ok(NOW, sess)
    aws_credentials_ok(NOW, _Sess(_Creds(NOW + timedelta(hours=1))))         # valid for long enough: starts


def test_the_check_runs_before_the_first_driver_message():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "smoke_option_d.py").read_text(encoding="utf-8")
    main = src[src.index("def main()"):]
    assert main.index("aws_credentials_ok()") < main.index("Pilot()")
