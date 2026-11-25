"""Pure units (no database): the driver location assessor (roadmap Phase 4) and the Telegram update parser and link
codes (Phase 5)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from setuhaul.api.service import option_ttl_s
from setuhaul.domain import location as L
from setuhaul.notify import telegram as tg

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
JAIPUR = (26.9124, 75.7873)


def ctx(**kw):
    base = dict(has_shipment=True, facility_position=JAIPUR, declared_eta=NOW + timedelta(minutes=90))
    base.update(kw)
    return L.JourneyContext(**base)


def rep(lat=27.43, lon=75.80, age_s=0, acc=30.0, shared=True):
    return L.LocationReport(shared, lat, lon, NOW - timedelta(seconds=age_s), acc)


def test_valid_recent_consistent_is_verified_and_coarse():
    a = L.assess(rep(), ctx(), NOW)
    assert a.assessment == L.VERIFIED and len(a.geohash) == 7 and 50 < a.distance_to_facility_km < 70
    rec = a.as_record("TEST_SINK")
    assert "latitude" not in rec and "longitude" not in rec and rec["position_geohash"] == a.geohash


@pytest.mark.parametrize("r,reason", [
    (rep(age_s=3600), "STALE_LOCATION"),
    (rep(lat=91), "BAD_COORDINATES"), (rep(lon=-181), "BAD_COORDINATES"), (rep(lat=0, lon=0), "BAD_COORDINATES"),
    (rep(lat=float("nan")), "BAD_COORDINATES"), (rep(lat=True), "BAD_COORDINATES"),
    (rep(acc=5000), "POOR_ACCURACY"), (rep(acc=-1), "BAD_ACCURACY"), (rep(age_s=-600), "TIMESTAMP_IN_FUTURE"),
    (L.LocationReport(True, 27.0, 75.0, None, 10), "NO_TIMESTAMP"),
])
def test_invalid_inputs(r, reason):
    a = L.assess(r, ctx(), NOW)
    assert a.assessment == L.INVALID and reason in a.reasons


def test_declined_is_unavailable_not_a_failure():
    a = L.assess(rep(shared=False), ctx(), NOW)
    assert a.assessment == L.UNAVAILABLE and not a.shared and a.geohash is None


@pytest.mark.parametrize("c,r,reason", [
    (ctx(), rep(lat=21.0, lon=79.0), "CANNOT_ARRIVE_BY_DECLARED_ETA"),
    (ctx(declared_eta=NOW - timedelta(minutes=5)), rep(), "DECLARED_ETA_PASSED_BUT_FAR_AWAY"),
    (ctx(declared_eta=NOW + timedelta(hours=4)), rep(lat=26.913, lon=75.788), "AT_FACILITY_BUT_REPORTS_LONG_DELAY"),
    (ctx(facility_position=None), rep(), "NO_FACILITY_POSITION"),
    (ctx(has_shipment=False), rep(), "NO_SHIPMENT_CONTEXT"),
    (ctx(declared_eta=None), rep(), "NO_DECLARED_ETA"),
    (ctx(previous_geohash=L.geohash(21.0, 79.0), previous_observed_at=NOW - timedelta(minutes=10)), rep(),
     "IMPOSSIBLE_MOVEMENT_SINCE_LAST_SHARE"),
])
def test_inconsistent_or_missing_context_needs_review_never_verified(c, r, reason):
    a = L.assess(r, c, NOW)
    assert a.assessment == L.NEEDS_REVIEW and reason in a.reasons


def test_geohash_round_trip_is_within_150_m():
    gh = L.geohash(*JAIPUR)
    assert L.km(L.geohash_center(gh), JAIPUR) < 0.15


def test_config_from_env():
    c = L.LocationConfig.from_env({"SETUHAUL_DRIVER_LOCATION_MAX_AGE_S": "60",
                                   "SETUHAUL_DRIVER_LOCATION_PROXIMITY_M": "500"})
    assert c.max_age_s == 60 and c.proximity_km == 0.5 and c.max_accuracy_m == 1000


def test_option_ttl_is_configurable_with_a_300_s_default_and_bounds():
    assert option_ttl_s({}) == 300 and option_ttl_s({"SETUHAUL_OPTION_TTL_S": "120"}) == 120
    for bad in ("10", "7200"):
        with pytest.raises(ValueError):
            option_ttl_s({"SETUHAUL_OPTION_TTL_S": bad})


# ---------------------------------------------------------------- Telegram parsing
def _msg(**m):
    base = {"message_id": 1, "date": 1764064800, "from": {"id": 42, "is_bot": False, "username": "DRV-1"},
            "chat": {"id": 42, "type": "private"}}
    base.update(m)
    return {"update_id": 9, "message": base}


def test_identity_is_the_numeric_id_never_the_username():
    e = tg.parse_update(_msg(text="hello"))
    assert (e.kind, e.user_id, e.external_id) == ("text", "42", "tg:9")


@pytest.mark.parametrize("u", [
    {"update_id": "9"}, {}, _msg(text="   "), _msg(chat={"id": -5, "type": "group"}, text="/start ABCDEFGHJK"),
    _msg(**{"from": {"id": 42, "is_bot": True}}, text="hi"),
    _msg(location={"latitude": 1.0, "longitude": 2.0, "live_period": 600}),
    {"update_id": 9, "callback_query": {"id": "c", "from": {"id": 42}, "data": "o:not-a-uuid:A"}},
    {"update_id": 9, "callback_query": {"id": "c", "from": {"id": 42}, "data": "o:%s:X" % ("0" * 36)}},
])
def test_everything_unexpected_is_ignored(u):
    assert tg.parse_update(u).kind == "ignored"


def test_start_code_callback_and_location():
    code, h = tg.new_link_code()
    e = tg.parse_update(_msg(text=f"/start {code.lower()[:5]}-{code.lower()[5:]}"))
    assert e.kind == "start" and e.code == code and tg.hash_code(e.code) == h
    assert tg.parse_update(_msg(text="/start nonsense!")).code is None
    oid = "12345678-1234-1234-1234-123456789abc"
    e = tg.parse_update({"update_id": 9, "callback_query": {"id": "cb1", "from": {"id": 42},
                                                             "message": {"chat": {"id": 42}}, "data": f"o:{oid}:R"}})
    assert (e.kind, e.offer_id, e.action, e.external_id) == ("callback", oid, "ALTERNATIVE", "tgcb:cb1")
    e = tg.parse_update(_msg(location={"latitude": 27.4, "longitude": 75.8, "horizontal_accuracy": 12.0}))
    assert e.kind == "location" and e.accuracy_m == 12.0 and e.observed_at.tzinfo is not None


def test_link_codes_are_random_readable_and_hashed():
    codes = {tg.new_link_code()[0] for _ in range(200)}
    assert len(codes) == 200 and all(len(c) == tg.CODE_LEN and set(c) <= set(tg.CODE_ALPHABET) for c in codes)
    c, h = tg.new_link_code()
    assert h != hashlib.sha256(c.encode()).hexdigest() and len(h) == 64


def test_secret_check_is_strict():
    assert tg.secret_ok("s" * 40, "s" * 40) and not tg.secret_ok(None, "s" * 40) and not tg.secret_ok("x", None)
    assert not tg.secret_ok("s" * 39, "s" * 40)


def test_buttons_fit_telegram_limits():
    for row in tg.offer_buttons("12345678-1234-1234-1234-123456789abc"):
        for b in row:
            assert len(b["callback_data"].encode()) <= 64
