"""Step 6 WORKLOAD_IDENTITY check 5 (23 Nov 2026): an ordinary operations user cannot obtain an ops_lead (or planner)
context. The roles come only from the verified token's Cognito groups; GET /console/me shows what the server derived,
and gated routes refuse. The live check uses a second Cognito user in the operations group."""
from __future__ import annotations

from test_api import CLIENT, H, JAI, app, client, dsn, keys, staff_token  # noqa: F401


def test_an_operations_user_has_operations_only_and_no_lead_or_planner_action(client, keys):
    me = client.get("/console/me", headers=H(staff_token(keys, groups=("operations",))))
    assert me.status_code == 200 and me.headers["cache-control"] == "no-store"
    body = me.json()
    assert body["roles"] == ["operations"] and body["facilities"] == [JAI]
    allowed = {a for a, ok in body["permissions"].items() if ok}
    assert allowed == {"decide", "handback", "list_requests", "takeover", "view_kill_switch", "withdraw"}
    assert not any(body["permissions"][a] for a in ("pause", "booking_hold", "move_appointment", "schedule_proposal",
                                                     "block_dock", "policy_report", "booking_kill_switch"))
    r = client.get(f"/console/facilities/{JAI}/schedule-proposal", headers=H(staff_token(keys, groups=("operations",))))
    assert r.status_code == 403 and "FORBIDDEN" in r.text


def test_a_planner_sees_the_proposal_and_an_ops_lead_gets_lead_actions(client, keys):
    r = client.get(f"/console/facilities/{JAI}/schedule-proposal", headers=H(staff_token(keys, groups=("planner",))))
    assert r.status_code == 200 and r.json()["status"] == "PROPOSAL" and r.json()["not_a_booking"] is True
    lead = client.get("/console/me", headers=H(staff_token(keys, groups=("ops_lead",)))).json()
    assert lead["permissions"]["pause"] and lead["permissions"]["booking_hold"] and lead["permissions"]["move_appointment"]


def test_nothing_but_the_token_groups_makes_a_role(client, keys):
    # a group that is not a staff role, an attempt to smuggle roles in other claims, and browser headers change nothing
    tok = staff_token(keys, groups=("operations", "facility-FAC-JAI-01", "ops-lead", "OPS_LEAD"), roles=["ops_lead"],
                      **{"custom:role": "ops_lead"})
    body = client.get("/console/me", headers={**H(tok), "X-Staff-Role": "ops_lead", "X-Roles": "ops_lead"}).json()
    assert body["roles"] == ["operations"] and body["permissions"]["pause"] is False
    assert client.get("/console/me", headers=H(staff_token(keys, groups=()))).status_code == 401   # no staff group: refused
    assert client.get("/console/me").status_code == 401
