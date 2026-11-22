"""The Cognito trigger that puts `facility_scope` into staff access tokens (setuhaul/aws/pretoken.py)."""
from __future__ import annotations

import pytest

from setuhaul.aws import pretoken


def event(groups, **extra_request):
    return {"version": "2", "triggerSource": "TokenGeneration_HostedAuth", "userName": "pilot-planner",
            "request": {"userAttributes": {"sub": "x"}, "groupConfiguration": {"groupsToOverride": groups},
                        "scopes": ["openid"], **extra_request}, "response": {"claimsAndScopeOverrideDetails": None}}


@pytest.fixture(autouse=True)
def allowed(monkeypatch):
    monkeypatch.setenv("FACILITIES", "FAC-JAI-01,FAC-GGN-01")


def scope_of(ev):
    d = pretoken.handler(ev, None)["response"]["claimsAndScopeOverrideDetails"]
    assert d["accessTokenGeneration"]["claimsToAddOrOverride"] == d["idTokenGeneration"]["claimsToAddOrOverride"]
    return d["accessTokenGeneration"]["claimsToAddOrOverride"]["facility_scope"]


def test_one_facility_group_gives_that_facility():
    assert scope_of(event(["planner", "facility-FAC-JAI-01"])) == "FAC-JAI-01"


def test_several_facility_groups_give_several_facilities_sorted():
    assert scope_of(event(["facility-FAC-JAI-01", "ops_lead", "facility-FAC-GGN-01"])) == "FAC-GGN-01,FAC-JAI-01"


def test_no_facility_group_gives_no_facility():
    assert scope_of(event(["planner"])) == "" and scope_of(event([])) == "" and scope_of(event(None)) == ""


def test_unknown_or_malformed_facility_groups_are_ignored():
    assert scope_of(event(["facility-FAC-XYZ-99", "facility-fac-jai-01", "facility-FAC-JAI-01,FAC-GGN-01",
                           "facility-", "FAC-JAI-01", "facility-FAC-JAI-01 "])) == ""


def test_nothing_a_browser_can_send_changes_the_scope():
    ev = event(["planner"], clientMetadata={"facility_scope": "FAC-JAI-01,FAC-GGN-01"})
    ev["request"]["userAttributes"]["custom:facility_scope"] = "FAC-JAI-01"
    ev["response"]["claimsAndScopeOverrideDetails"] = {"accessTokenGeneration": {
        "claimsToAddOrOverride": {"facility_scope": "FAC-GGN-01"}}}
    assert scope_of(ev) == ""
