"""Facility approval mode (migration 0047) over HTTP: Planner settings read and change, refused for other roles,
facilities and malformed input; /console/me reports the two new permissions."""
from __future__ import annotations

from test_api import GGN, H, JAI, app, client, dsn, keys, staff_token  # noqa: F401 (fixtures)


def test_api_shows_the_default_and_lets_a_planner_change_it(client, keys):
    tok = H(staff_token(keys, groups=("planner",)))
    r = client.get(f"/console/facilities/{JAI}/approval-mode", headers=tok)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["approval_mode"] == "MANUAL" and m["can_edit"] is True and m["facility_id"] == JAI
    r = client.put(f"/console/facilities/{JAI}/approval-mode", headers=tok,
                   json={"mode": "AUTO", "expected_version": m["version"], "reason": "trial"})
    assert r.status_code == 200 and r.json()["approval_mode"] == "AUTO" and r.json()["version"] == m["version"] + 1
    stale = client.put(f"/console/facilities/{JAI}/approval-mode", headers=tok,
                       json={"mode": "MANUAL", "expected_version": m["version"], "reason": "stale"})
    assert stale.status_code == 409 and "VERSION_CONFLICT" in stale.text
    back = client.put(f"/console/facilities/{JAI}/approval-mode", headers=tok,
                      json={"mode": "MANUAL", "expected_version": m["version"] + 1, "reason": "back to manual"})
    assert back.status_code == 200 and back.json()["approval_mode"] == "MANUAL"


def test_api_refuses_other_roles_other_facilities_and_bad_input(client, keys):
    ops_ = H(staff_token(keys, groups=("operations",)))
    assert client.get(f"/console/facilities/{JAI}/approval-mode", headers=ops_).status_code == 403
    assert client.put(f"/console/facilities/{JAI}/approval-mode", headers=ops_,
                      json={"mode": "AUTO", "expected_version": 0, "reason": "nope"}).status_code == 403
    pl = H(staff_token(keys, groups=("planner",)))
    assert client.get(f"/console/facilities/{GGN}/approval-mode", headers=pl).status_code == 403
    assert client.put(f"/console/facilities/{GGN}/approval-mode", headers=pl,
                      json={"mode": "AUTO", "expected_version": 0, "reason": "outside scope"}).status_code == 403
    for bad in ({"mode": "SOMETIMES", "expected_version": 0, "reason": "bad"},
                {"mode": "AUTO", "expected_version": -1, "reason": "bad"},
                {"mode": "AUTO", "expected_version": 0, "reason": ""},
                {"mode": "AUTO", "expected_version": 0, "reason": "ok ok", "approved_by": "me"}):
        assert client.put(f"/console/facilities/{JAI}/approval-mode", headers=pl, json=bad).status_code == 422, bad
    assert client.get(f"/console/facilities/{JAI}/approval-mode").status_code == 401
    me = client.get("/console/me", headers=ops_).json()["permissions"]
    assert me["view_approval_mode"] is False and me["set_approval_mode"] is False
    lead = client.get("/console/me", headers=H(staff_token(keys, groups=("ops_lead",)))).json()["permissions"]
    assert lead["view_approval_mode"] and lead["set_approval_mode"]
