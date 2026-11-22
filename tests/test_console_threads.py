"""Read-only conversation views for the hosted staff console: GET /console/threads and /console/threads/{id}.

Same staff authentication as every /console route (Cognito access token, staff group), facility isolation by the
database's row-level security (the console role, the person's facility scope), no writes, masked output.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import psycopg
import pytest

from test_api import GGN, H, IST, JAI, Driver, add_driver, app, client, dsn, keys, staff_token  # noqa: F401 (fixtures)


def converse(client, dsn, facility=JAI):
    """A real conversation: signed webhook in, the agent's tools record the delay and compute options."""
    drv, phone, (sid,) = add_driver(dsn, facility)
    d = Driver(client, phone)
    d.say("Running 90 min late, traffic")
    eta = (datetime.now(IST) + timedelta(minutes=90)).isoformat()
    assert d.tool("record_driver_report", message_id=d.last_msg, declared_eta=eta, eta_precision="PRECISE",
                  delay_reason="TRAFFIC")["status"] == "OK"
    assert d.tool("find_feasible_options")["status"] == "OK"
    return d, phone, sid


def rows(dsn, sql, *args):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


def test_staff_see_their_facility_conversations_masked_and_newest_first(client, dsn, keys):
    d, phone, sid = converse(client, dsn, JAI)
    r = client.get("/console/threads", headers=H(staff_token(keys, facilities=(JAI,))))
    assert r.status_code == 200
    mine = next(t for t in r.json()["threads"] if t["thread_id"] == d.thread)
    assert mine["facility_id"] == JAI and mine["shipment_id"] == sid
    assert mine["driver_phone"].endswith(phone[-4:]) and mine["driver_phone"] != phone and "•" in mine["driver_phone"]
    assert phone not in r.text                                                     # the full number never leaves
    times = [t["last_message_at"] for t in r.json()["threads"]]
    assert times == sorted(times, reverse=True)


def test_the_detail_is_what_the_database_holds(client, dsn, keys):
    d, phone, sid = converse(client, dsn, JAI)
    r = client.get(f"/console/threads/{d.thread}", headers=H(staff_token(keys, facilities=(JAI,))))
    assert r.status_code == 200, r.text
    b = r.json()
    db_msgs = rows(dsn, """SELECT message_id, sender_type, body_masked FROM setuhaul.chat_messages
                            WHERE thread_id = %s ORDER BY created_at, message_id""", d.thread)
    assert [(m["message_id"], m["sender"], m["body_masked"]) for m in b["messages"]] == [tuple(x) for x in db_msgs]
    db_opts = rows(dsn, "SELECT option_id::text FROM setuhaul.shown_options WHERE thread_id = %s", d.thread)
    assert db_opts and {o["option_id"] for o in b["options"]} == {x[0] for x in db_opts}
    assert all(o["dock_code"] and o["start"] for o in b["options"])
    db_eta = rows(dsn, "SELECT derived_eta FROM setuhaul.eta_signals WHERE shipment_id = %s", sid)
    assert len(b["extracted"]["eta_signals"]) == min(len(db_eta), 5) and db_eta
    db_notes = rows(dsn, "SELECT notification_id::text, status FROM setuhaul.notifications WHERE thread_id = %s", d.thread)
    assert {(n["notification_id"], n["status"]) for n in b["notifications"]} == {tuple(x) for x in db_notes}
    assert b["thread"]["facility_id"] == JAI and b["flow"]["state"] is not None
    assert phone not in r.text and "payload" not in r.text and "recipient_ref" not in r.text


def test_another_facility_cannot_list_or_open_the_conversation(client, dsn, keys):
    d, _, _ = converse(client, dsn, JAI)
    ggn = H(staff_token(keys, facilities=(GGN,)))
    assert d.thread not in {t["thread_id"] for t in client.get("/console/threads?limit=50", headers=ggn).json()["threads"]}
    other = client.get(f"/console/threads/{d.thread}", headers=ggn)
    unknown = client.get("/console/threads/T-does-not-exist", headers=ggn)
    assert other.status_code == unknown.status_code == 404                 # outside scope looks like absent
    assert other.json() == unknown.json()
    both = H(staff_token(keys, facilities=(JAI, GGN)))
    assert client.get(f"/console/threads/{d.thread}", headers=both).status_code == 200


def test_no_scope_sees_nothing_and_no_staff_group_is_refused(client, dsn, keys):
    d, _, _ = converse(client, dsn, JAI)
    none = H(staff_token(keys, facilities=()))
    assert client.get("/console/threads", headers=none).json()["threads"] == []
    assert client.get(f"/console/threads/{d.thread}", headers=none).status_code == 404
    for h in ({}, {"Authorization": "Bearer nonsense"}, H(staff_token(keys, groups=("drivers",))),
              H(staff_token(keys, groups=("facility-FAC-JAI-01",)))):            # a facility group is not a role
        assert client.get("/console/threads", headers=h).status_code == 401
        assert client.get(f"/console/threads/{d.thread}", headers=h).status_code == 401


def test_pages_do_not_overlap_and_the_size_is_capped(client, dsn, keys):
    for _ in range(3):
        converse(client, dsn, GGN)
    h = H(staff_token(keys, facilities=(GGN,)))
    p1 = client.get("/console/threads?limit=2", headers=h).json()
    assert len(p1["threads"]) == 2 and p1["next_before"]
    p2 = client.get("/console/threads", params={"limit": 2, "before": p1["next_before"]}, headers=h).json()
    assert p2["threads"] and not {t["thread_id"] for t in p1["threads"]} & {t["thread_id"] for t in p2["threads"]}
    assert client.get("/console/threads?limit=51", headers=h).status_code == 422
    assert client.get("/console/threads?limit=0", headers=h).status_code == 422
    assert client.get("/console/threads/" + "x" * 81, headers=h).status_code == 422


def test_reading_changes_nothing(client, dsn, keys):
    d, _, _ = converse(client, dsn, JAI)
    tables = ("chat_threads", "chat_messages", "thread_flow", "thread_facts", "shown_options", "option_selections",
              "notifications", "outbox", "appointments", "approval_requests", "escalations")

    def snapshot():
        with psycopg.connect(dsn) as c:
            return {t: c.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                                 f"FROM setuhaul.{t} x").fetchone() for t in tables}
    before = snapshot()
    h = H(staff_token(keys, facilities=(JAI, GGN)))
    for _ in range(2):
        assert client.get("/console/threads", headers=h).status_code == 200
        assert client.get(f"/console/threads/{d.thread}", headers=h).status_code == 200
    assert snapshot() == before


def test_delivery_kind_and_phone_masking():
    from setuhaul.api.service import BookingService as S
    assert S._delivery("testsink.0b7f") == "test_sink" and S._delivery("wamid.HBg") == "provider" and S._delivery(None) is None
    assert S._mask_phone("+99900001234") == "+999••••1234" and S._mask_phone(None) is None


def test_the_test_sink_evidence_is_the_stored_record(client, dsn, keys):
    """A notice the outbound relay hands to the internal test sink shows as SENT with its test-sink reference;
    a queued one is not shown as delivered."""
    from setuhaul.api.db import Db
    from setuhaul.notify.outbound import OutboundRelay, TestSinkProvider
    from test_approvals import pending
    from test_db_requests import decide
    with psycopg.connect(dsn) as conn:
        t, r = pending(conn)
        decide(conn, r["appointment_id"], "REJECT")               # a driver notice is now owed
        tid = conn.execute("SELECT thread_id FROM setuhaul.chat_threads WHERE shipment_id = %s", (t["sid"],)).fetchone()
    if tid is None:
        pytest.skip("this fixture's thread has no conversation row")
    h = H(staff_token(keys, facilities=(JAI, GGN)))
    OutboundRelay(Db(dsn), TestSinkProvider()).run_once(limit=1000)
    OutboundRelay(Db(dsn), TestSinkProvider()).run_once(limit=1000)
    b = client.get(f"/console/threads/{tid[0]}", headers=h).json()
    stored = {x[0]: (x[1], x[2]) for x in rows(dsn, """SELECT notification_id::text, status, provider_message_id
                                                        FROM setuhaul.notifications WHERE thread_id = %s""", tid[0])}
    for n in b["notifications"]:
        status, pid = stored[n["notification_id"]]
        assert n["status"] == status
        assert (n["delivery"] == "test_sink") == bool(pid and pid.startswith("testsink."))
        assert n["delivery"] != "test_sink" or n["test_sink_reference"] == pid
    assert json.dumps(b).count("payload") == 0


# ---------------------------------------------------------------- the console page, served by the API under /app/
DOMAIN = "setuhaul-pilot-console.auth.ap-southeast-2.amazoncognito.com"


@pytest.fixture(scope="module")
def site(dsn, keys):
    from fastapi.testclient import TestClient
    from setuhaul.api.app import Settings, create_app
    from test_api import CLIENT, ISSUER, META_SECRET, SESSION_SECRET
    return TestClient(create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                                          session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                                          facilities=(JAI, GGN), jwks=keys["jwks"], cognito_domain=DOMAIN,
                                          console_client_id=CLIENT)), follow_redirects=False)


def test_the_page_and_its_assets_are_served_with_a_strict_policy(site):
    import re
    r = site.get("/app/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp and f"https://{DOMAIN}" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-cache" and "<script>" not in r.text and "onclick" not in r.text
    assets = re.findall(r'(?:src|href)="(assets/[^"]+)"', r.text)
    assert len(assets) == 2
    for path in assets:
        a = site.get("/app/" + path)
        assert a.status_code == 200 and "immutable" in a.headers["cache-control"], path
        assert a.headers["content-type"].split(";")[0] in ("text/javascript", "text/css")
        assert a.headers["x-content-type-options"] == "nosniff"
    assert site.get("/app/assets/app.js?v=old").headers["cache-control"] == "no-cache"
    assert site.get("/app").status_code == 308 and site.get("/app").headers["location"] == "app/"


def test_only_the_allow_list_is_served(site):
    for path in ("/app/assets/../console_site.py", "/app/assets/..%2F..%2Fapp.py", "/app/assets/%2e%2e%2fapp.py",
                 "/app/assets/index.html", "/app/index.html", "/app/assets/", "/app/console_static/index.html",
                 "/app/assets/app.js.map", "/app/.env", "/app/assets/%00app.js"):
        r = site.get(path)
        assert r.status_code in (404, 405), (path, r.status_code)
        assert "def " not in r.text and "import " not in r.text, path


def test_the_config_holds_public_values_only(site):
    from test_api import CLIENT
    c = site.get("/app/config.json")
    assert c.status_code == 200 and c.json() == {"cognitoDomain": DOMAIN, "clientId": CLIENT, "apiBase": "../"}


def test_no_console_page_without_cognito_and_no_cors_anywhere(client, site):
    assert client.get("/app/").status_code == 404 and client.get("/app/config.json").status_code == 404
    pre = {"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    for c in (client, site):
        assert "access-control-allow-origin" not in c.options("/console/threads", headers=pre).headers
        assert "access-control-allow-origin" not in c.get("/console/threads", headers={"Origin": "https://evil.example"}).headers


def test_staff_data_is_never_cached(site, keys):
    r = site.get("/console/threads", headers=H(staff_token(keys)))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert site.get("/console/threads").headers["cache-control"] == "no-store"        # the 401 too


def test_the_console_files_hold_no_secret_and_no_demo_shortcut():
    from pathlib import Path
    from setuhaul.ops.console_config import problems
    src = Path(__file__).resolve().parents[1] / "setuhaul" / "api" / "console_static"
    assert problems(src) == []
    js = (src / "assets" / "app.js").read_text(encoding="utf-8")
    assert "localStorage" not in js and "/demo/" not in js and "hmac" not in js.lower()
    assert 'code_challenge_method:"S256"' in js and 'sessionStorage.removeItem("pkce")' in js
    assert "location.origin + location.pathname" in js and 'fetch("config.json"' in js


def test_the_secret_scan_catches_leaks(tmp_path):
    import shutil
    from pathlib import Path
    from setuhaul.ops.console_config import problems
    site_dir = tmp_path / "site"
    shutil.copytree(Path(__file__).resolve().parents[1] / "setuhaul" / "api" / "console_static", site_dir)
    (site_dir / "assets" / "leak.js").write_text('const META_APP_SECRET = "abc"')
    (site_dir / "notes.txt").write_text("x")
    p = problems(site_dir)
    assert any("leak.js" in x for x in p) and any("notes.txt" in x for x in p)
