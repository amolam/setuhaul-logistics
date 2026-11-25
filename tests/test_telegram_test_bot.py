"""Telegram TEST-BOT mode: fail-closed configuration, the chat allowlist, and the real `TelegramBotProvider` HTTP
calls against a FAKE Bot API (httpx.MockTransport, in process).

These are NOT real Telegram results: nothing here reaches api.telegram.org. They prove the request shapes (sendMessage
with inline / reply keyboards, answerCallbackQuery, editMessageReplyMarkup), the error mapping (429, 5xx, 4xx,
timeouts) and the end-to-end wiring (webhook -> service -> relay -> Bot API client) that the owner-run live exercise
(scripts/telegram_test_bot.py) then repeats against the real test bot.

Own database (setuhaul_build_dispatch4).
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from setuhaul.api.app import Settings, create_app
from setuhaul.api.db import Db
from setuhaul.notify.outbound import ACCEPTED, AMBIGUOUS, PERMANENT, RETRYABLE, LocalProvider, OutboundRelay, ProviderTimeout
from setuhaul.notify.telegram import (AllowlistedTelegram, TelegramBotProvider, TelegramTestSink, parse_update,
                                      telegram_from_env)
from test_api import CLIENT, GGN, ISSUER, JAI, META_SECRET, SESSION_SECRET, Driver, H, keys, staff_token  # noqa: F401
from test_dispatch_offers import (TG_SECRET, Tg, add_driver, appointments, make_database, offer_status, ops, planner,
                                  q)

TOKEN = "123456789:AAtest-token-for-a-fake-bot-api-only_xyz"     # shape only; the fake API never checks it
ME, OTHER = 5550001001, 5550002002                                 # allowlisted test account / anyone else


class FakeBotApi:
    """Records every Bot API call; answers like Telegram. `fail` maps a method to a scripted status or 'timeout'."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list] = {}
        self.ids: dict[str, itertools.count] = {}     # like Telegram: message ids count PER CHAT, from 1

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        script = self.fail.get(method)
        if script:
            what = script.pop(0)
            if what == "timeout":
                raise httpx.ReadTimeout("fake timeout", request=request)
            if what == "connect":
                raise httpx.ConnectError("fake connect", request=request)
            return httpx.Response(what, json={"ok": False, "error_code": what, "description": "fake",
                                              **({"parameters": {"retry_after": 1}} if what == 429 else {})})
        if method == "sendMessage":
            ids = self.ids.setdefault(str(body["chat_id"]), itertools.count(1))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": next(ids),
                                                                    "chat": {"id": int(body["chat_id"])}}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent_to(self, chat) -> list[dict]:
        return [b for m, b in self.calls if m == "sendMessage" and str(b["chat_id"]) == str(chat)]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def bot(api: FakeBotApi) -> TelegramBotProvider:
    return TelegramBotProvider(TOKEN, client=httpx.Client(transport=httpx.MockTransport(api)))


# ================================================================ configuration: fail closed
BASE = {"SETUHAUL_TELEGRAM_DELIVERY": "test_bot", "SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED": "true", "SETUHAUL_ENV": "local",
        "SETUHAUL_TELEGRAM_TEST_CHAT_IDS": f"{ME}", "SETUHAUL_TELEGRAM_TEST_BOT_TOKEN": TOKEN}


@pytest.mark.parametrize("change,why", [
    ({"SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED": "false"}, "AUTHORIZED"),
    ({"SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED": None}, "AUTHORIZED"),
    ({"SETUHAUL_ENV": "pilot"}, "SETUHAUL_ENV"),
    ({"SETUHAUL_ENV": "production"}, "SETUHAUL_ENV"),
    ({"SETUHAUL_ENV": None}, "SETUHAUL_ENV"),
    ({"SETUHAUL_TELEGRAM_TEST_CHAT_IDS": ""}, "CHAT_IDS"),
    ({"SETUHAUL_TELEGRAM_TEST_CHAT_IDS": "@somebody"}, "CHAT_IDS"),
    ({"SETUHAUL_TELEGRAM_TEST_CHAT_IDS": "-1001234567890"}, "CHAT_IDS"),       # a group / channel id: refused
    ({"SETUHAUL_TELEGRAM_TEST_CHAT_IDS": ",".join(str(10**6 + i) for i in range(11))}, "CHAT_IDS"),
    ({"SETUHAUL_TELEGRAM_TEST_BOT_TOKEN": None}, "token"),
    ({"SETUHAUL_TELEGRAM_TEST_BOT_TOKEN": "not-a-token"}, "token"),
])
def test_test_bot_mode_refuses_any_missing_piece(change, why):
    env = {**BASE, **change}
    env = {k: v for k, v in env.items() if v is not None}
    with pytest.raises(RuntimeError, match=why):
        telegram_from_env(env)


def test_test_bot_mode_ready_is_always_allowlisted_and_never_the_live_mode():
    t, allow = telegram_from_env(BASE)
    assert isinstance(t, AllowlistedTelegram) and allow == frozenset({str(ME)})
    # the live authorization neither enables nor widens test_bot; live itself still needs its own authorization
    t2, allow2 = telegram_from_env({**BASE, "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true"})
    assert isinstance(t2, AllowlistedTelegram) and allow2 == allow
    with pytest.raises(RuntimeError, match="SETUHAUL_ENV in"):  # Full Activation: live only in the synthetic pilot
        telegram_from_env({**BASE, "SETUHAUL_TELEGRAM_DELIVERY": "live", "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true"})
    pilot = {**BASE, "SETUHAUL_ENV": "pilot", "SETUHAUL_DATA_CLASS": "synthetic"}
    with pytest.raises(RuntimeError, match="LIVE_AUTHORIZED"):
        telegram_from_env({**pilot, "SETUHAUL_TELEGRAM_DELIVERY": "live"})
    with pytest.raises(RuntimeError, match="BOT_SECRET_ID"):   # live never reads a plain-variable token
        telegram_from_env({**pilot, "SETUHAUL_TELEGRAM_DELIVERY": "live", "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true"})
    assert isinstance(telegram_from_env({})[0], TelegramTestSink)      # the default is still the sink


def test_test_bot_token_from_secrets_manager_and_unreadable_secret():
    class Sm:
        def __init__(self, raw):
            self.raw = raw

        def get_secret_value(self, SecretId):            # noqa: N803 (boto3 spelling)
            if self.raw is None:
                raise KeyError("nope")
            return {"SecretString": self.raw}
    env = {k: v for k, v in BASE.items() if k != "SETUHAUL_TELEGRAM_TEST_BOT_TOKEN"}
    env["SETUHAUL_TELEGRAM_BOT_SECRET_ID"] = "arn:test"
    assert isinstance(telegram_from_env(env, Sm(json.dumps({"token": TOKEN})))[0], AllowlistedTelegram)
    with pytest.raises(RuntimeError, match="unreadable") as e:
        telegram_from_env(env, Sm(None))
    assert TOKEN not in str(e.value)


def test_the_app_refuses_to_start_on_a_broken_test_bot_configuration(monkeypatch):
    from setuhaul.api import app as app_mod
    monkeypatch.setenv("SETUHAUL_TELEGRAM_DELIVERY", "test_bot")
    monkeypatch.delenv("SETUHAUL_TELEGRAM_TEST_BOT_AUTHORIZED", raising=False)
    with pytest.raises(RuntimeError, match="AUTHORIZED"):
        app_mod._telegram_from_env()
    monkeypatch.setenv("SETUHAUL_TELEGRAM_DELIVERY", "test_sink")
    assert app_mod._telegram_from_env() == {"telegram_mode": "test_sink", "telegram_webhook_secret": None}


def test_runner_refuses_a_database_that_is_not_the_dedicated_test_bot_database(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("tgrunner", Path(__file__).resolve().parents[1] / "scripts/telegram_test_bot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    for bad in ("host=x dbname=setuhaul", "host=x dbname=setuhaul_pilot", "host=x dbname=postgres", ""):
        monkeypatch.setenv("SETUHAUL_TEST_BOT_DSN", bad)
        with pytest.raises(SystemExit, match="setuhaul_tgbot"):
            mod.config()
    monkeypatch.setenv("SETUHAUL_TEST_BOT_DSN", "host=localhost dbname=setuhaul_tgbot")
    dsn, t, allow = mod.config()
    assert isinstance(t, AllowlistedTelegram) and allow == frozenset({str(ME)})


# ================================================================ the Bot API client against a fake Bot API
def test_allowlist_refuses_before_any_network_call():
    api = FakeBotApi()
    t = AllowlistedTelegram(bot(api), frozenset({str(ME)}))
    r = t.send(str(OTHER), "hello", "k1")
    assert r.outcome == PERMANENT and r.error == "NOT_ALLOWLISTED" and api.calls == []
    t.clear_buttons(str(OTHER), 5)
    assert api.calls == []
    assert t.send(str(ME), "hello", "k2").outcome == ACCEPTED and api.methods() == ["sendMessage"]


def test_provider_request_shapes_and_error_mapping():
    api = FakeBotApi()
    p = bot(api)
    kb = {"inline_keyboard": [[{"text": "Accept", "callback_data": "o:x:A"}]]}
    r = p.send(str(ME), "Booking offer", "k", reply_markup=kb)
    assert r.outcome == ACCEPTED and r.provider_message_id.startswith("tg.")
    assert api.calls[-1] == ("sendMessage", {"chat_id": str(ME), "text": "Booking offer", "reply_markup": kb})
    api.fail["sendMessage"] = [429, 502, 403, 400, "connect"]
    assert p.send(str(ME), "t", "k").outcome == RETRYABLE           # rate limited: retried by the relay
    assert p.send(str(ME), "t", "k").outcome == AMBIGUOUS           # 5xx: may have arrived -> UNKNOWN, not FAILED
    assert p.send(str(ME), "t", "k").outcome == PERMANENT           # blocked by the user
    assert p.send(str(ME), "t", "k").outcome == PERMANENT           # bad chat
    assert p.send(str(ME), "t", "k").outcome == RETRYABLE           # never connected: safe to retry
    api.fail["sendMessage"] = ["timeout"]
    with pytest.raises(ProviderTimeout):
        p.send(str(ME), "t", "k")
    p.clear_buttons(str(ME), 7)
    p.answer_callback("cb-1")
    assert api.calls[-2] == ("editMessageReplyMarkup", {"chat_id": str(ME), "message_id": 7,
                                                        "reply_markup": {"inline_keyboard": []}})
    assert api.calls[-1] == ("answerCallbackQuery", {"callback_query_id": "cb-1"})
    api.fail["editMessageReplyMarkup"] = ["timeout"]
    p.clear_buttons(str(ME), 8)                                     # best effort: never raises
    assert all(TOKEN not in json.dumps(b) for _, b in api.calls)    # the token is only ever in the URL path


def test_callbacks_from_a_group_chat_are_ignored():
    u = {"update_id": 9, "callback_query": {"id": "c", "from": {"id": ME}, "data": f"o:{uuid.uuid4()}:A",
                                            "message": {"message_id": 1, "chat": {"id": -100123, "type": "group"}}}}
    assert parse_update(u).kind == "ignored"


# ================================================================ end to end through the webhook and the relay
@pytest.fixture(scope="module")
def dsn():
    return make_database("setuhaul_build_dispatch4")


@pytest.fixture(scope="module")
def api():
    return FakeBotApi()


@pytest.fixture(scope="module")
def transport(api):
    return AllowlistedTelegram(bot(api), frozenset(str(x) for x in TEST_ACCOUNTS))


@pytest.fixture(scope="module")
def app(dsn, keys, transport):
    return create_app(Settings(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                               meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"],
                               booking_enabled=True, local_test_harness=True, telegram_webhook_secret=TG_SECRET,
                               telegram_replies=transport, telegram_allowlist=transport.allowed))


@pytest.fixture
def client(app):
    return TestClient(app)


def disp(keys):
    return H(staff_token(keys, groups=("dispatcher",)))


TEST_ACCOUNTS = [ME + i for i in range(20)]          # the allowlist: one account links one driver at a time
_accounts = iter(TEST_ACCOUNTS)


def link_me(client, keys, drv) -> Tg:
    r = client.post(f"/console/drivers/{drv}/telegram-link-code", headers=disp(keys))
    assert r.status_code == 200, r.text
    me = Tg(client, user_id=next(_accounts))
    assert "linked to SetuHaul" in me.text(f"/start {r.json()['code']}")
    return me


def send_offer(client, keys, d):
    o = client.get(f"/console/dispatch/shipments/{d['sid']}/options", headers=disp(keys)).json()["options"][0]
    r = client.post("/console/dispatch/offers", headers=disp(keys),
                    json={"shipment_id": d["sid"], "driver_id": d["drv"], "vehicle_id": d["veh"],
                          "dock_code": o["dock_code"], "start": o["start"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()


def flush(dsn, transport):
    return OutboundRelay(Db(dsn), LocalProvider(), telegram=transport).run_once(limit=200)


def test_journey_a_offer_buttons_callback_planner_outcome(client, dsn, keys, api, transport):
    d = add_driver(dsn)
    me = link_me(client, keys, d["drv"])
    assert "linked" in api.sent_to(me.uid)[-1]["text"]                  # the link result went out via the Bot API
    o = send_offer(client, keys, d)
    flush(dsn, transport)
    msg = api.sent_to(me.uid)[-1]
    assert "Booking offer" in msg["text"]
    assert [b["callback_data"] for row in msg["reply_markup"]["inline_keyboard"] for b in row] == \
        [f"o:{o['offer_id']}:A", f"o:{o['offer_id']}:D", f"o:{o['offer_id']}:R"]
    n = len(api.calls)
    assert "planner" in me.press(o["offer_id"], "A", callback_id="cbA1")
    methods = api.methods()[n:]
    assert methods == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]   # spinner, buttons, answer
    assert offer_status(dsn, o["offer_id"]) == "ACCEPTED"
    (appt, st), = appointments(dsn, d["sid"])
    assert st == "PENDING_CONFIRMATION"                     # accepted is not booked
    n = len(api.calls)
    me.press(o["offer_id"], "A", callback_id="cbA1")      # a redelivered callback: no second booking action
    assert len(appointments(dsn, d["sid"])) == 1
    r = client.post(f"/console/requests/{appt}/decision", headers=planner(keys),
                    json={"decision": "CONFIRM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    before = len(api.sent_to(me.uid))
    flush(dsn, transport)
    outcome = api.sent_to(me.uid)[before:]
    assert outcome and any("confirm" in m["text"].lower() for m in outcome), outcome


def test_journey_b_c_decline_and_stale_callbacks(client, dsn, keys, api, transport):
    d = add_driver(dsn, eta_at=datetime.now(timezone.utc) + timedelta(hours=8))
    me = link_me(client, keys, d["drv"])
    o1 = send_offer(client, keys, d)
    assert "declined" in me.press(o1["offer_id"], "D")
    assert offer_status(dsn, o1["offer_id"]) == "DECLINED" and appointments(dsn, d["sid"]) == []
    o2 = send_offer(client, keys, d)
    me.press(o1["offer_id"], "A")                            # the old button: changes nothing
    assert offer_status(dsn, o2["offer_id"]) == "OFFERED" and appointments(dsn, d["sid"]) == []
    q(dsn, "UPDATE shown_options SET expires_at=now()-interval '1 second' WHERE option_id=%s", o2["option_id"])
    q(dsn, "UPDATE dispatch_offers SET expires_at=now()-interval '1 second' WHERE offer_id=%s", o2["offer_id"])
    assert "expired" in me.press(o2["offer_id"], "A")
    assert appointments(dsn, d["sid"]) == []


def test_journey_d_delay_location_keyboard_share_and_not_now(client, dsn, keys, api, transport, app):
    d = add_driver(dsn)
    me = link_me(client, keys, d["drv"])
    wa = Driver(client, d["phone"])
    wa.say("Running 90 min late")
    eta = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
    assert wa.tool("record_driver_report", message_id=wa.last_msg, declared_eta=eta, eta_precision="PRECISE",
                   delay_reason="TRAFFIC")["status"] == "OK"
    from setuhaul.api.security import SessionTokens
    app.state.service.record_reply(SessionTokens(SESSION_SECRET).verify(wa.session), "Noted: about 90 minutes late.",
                                   reply_to=wa.last_msg, generation=0)
    flush(dsn, transport)
    kb = api.sent_to(me.uid)[-1]["reply_markup"]
    assert kb["keyboard"][0][0]["request_location"] is True and kb["one_time_keyboard"] is True
    me.location(27.43, 75.80, accuracy=30)
    last = api.sent_to(me.uid)[-1]
    assert last["reply_markup"] == {"remove_keyboard": True}             # the one-time keyboard goes away
    assert q(dsn, "SELECT assessment FROM driver_location_reports WHERE driver_id=%s", d["drv"]) == [("VERIFIED",)]
    me.location(27.43, 75.80, accuracy=30, live=True)                   # live location: ignored
    assert q(dsn, "SELECT count(*) FROM driver_location_reports WHERE driver_id=%s", d["drv"]) == [(1,)]
    assert "still stands" in me.text("Not now")
    assert api.sent_to(me.uid)[-1]["reply_markup"] == {"remove_keyboard": True}


def test_outside_the_allowlist_nothing_is_processed_or_sent(client, dsn, keys, api, transport):
    d = add_driver(dsn)
    code = client.post(f"/console/drivers/{d['drv']}/telegram-link-code", headers=disp(keys)).json()["code"]
    stranger = Tg(client, user_id=OTHER)
    n = len(api.calls)
    stranger.text(f"/start {code}")
    assert len(api.calls) == n                                       # no reply at all, no Bot API call
    assert q(dsn, "SELECT count(*) FROM driver_channel_links WHERE driver_id=%s", d["drv"]) == [(0,)]
    # and the code is still unused: the real test account can redeem it
    me = Tg(client, user_id=next(_accounts))
    assert "linked to SetuHaul" in me.text(f"/start {code}")


def test_journey_g_failures_through_the_real_client_keep_the_relay_semantics(client, dsn, keys, api, transport):
    d = add_driver(dsn)
    link_me(client, keys, d["drv"])
    o = send_offer(client, keys, d)
    api.fail["sendMessage"] = [429]
    flush(dsn, transport)
    st = q(dsn, "SELECT status FROM notifications WHERE chat_message_id=%s", o["read_back_message_id"])[0][0]
    assert st == "RETRY"
    q(dsn, "UPDATE notifications SET next_retry_at=now() WHERE chat_message_id=%s", o["read_back_message_id"])
    api.fail["sendMessage"] = ["timeout"]
    flush(dsn, transport)
    st = q(dsn, "SELECT status FROM notifications WHERE chat_message_id=%s", o["read_back_message_id"])[0][0]
    assert st == "UNKNOWN"                                            # a timeout is not a failure
    row = next(x for x in client.get("/console/ops/notifications", params={"state": "UNKNOWN"},
                                     headers=ops(keys)).json()["notifications"] if x["driver_id"] == d["drv"])
    assert row["route_now"] == "TELEGRAM"
    assert offer_status(dsn, o["offer_id"]) == "OFFERED"              # delivery state never touches the offer


# ================================================================ 0038: typed yes / no clears the offer's buttons
def _offer_ref(dsn, o):
    return q(dsn, "SELECT provider_message_id FROM notifications WHERE chat_message_id=%s", o["read_back_message_id"])[0][0]


@pytest.mark.parametrize("typed,status", [("haan ji", "ACCEPTED"), ("no", "DECLINED")])
def test_typed_answer_clears_the_offer_buttons_and_a_later_tap_is_still_refused(client, dsn, keys, api, transport,
                                                                                typed, status):
    from setuhaul.notify.telegram import message_id_for_chat
    d = add_driver(dsn, eta_at=datetime.now(timezone.utc) + timedelta(hours=10))
    me = link_me(client, keys, d["drv"])
    o = send_offer(client, keys, d)
    flush(dsn, transport)
    mid = message_id_for_chat(_offer_ref(dsn, o), me.uid)
    assert isinstance(mid, int)                                         # the relay recorded the offer's message id
    n = len(api.calls)
    me.text(typed)
    assert offer_status(dsn, o["offer_id"]) == status
    edits = [b for m, b in api.calls[n:] if m == "editMessageReplyMarkup"]
    assert edits == [{"chat_id": str(me.uid), "message_id": mid, "reply_markup": {"inline_keyboard": []}}]
    booked = appointments(dsn, d["sid"])
    # the server still refuses a stale tap on the (now hidden) button, whatever the client shows
    reply = me.press(o["offer_id"], "A")
    assert offer_status(dsn, o["offer_id"]) == status and appointments(dsn, d["sid"]) == booked
    # the answer only restates the recorded state (accepted / declined); it never reports a new action
    assert reply and ("recorded" in reply.lower() or "declined" in reply.lower()), reply


def test_typed_answer_before_the_offer_reached_telegram_clears_nothing(client, dsn, keys, api, transport):
    d = add_driver(dsn, eta_at=datetime.now(timezone.utc) + timedelta(hours=12))
    me = link_me(client, keys, d["drv"])
    o = send_offer(client, keys, d)                                     # not relayed yet: no Telegram message id
    n = len(api.calls)
    me.text("haan ji")
    assert offer_status(dsn, o["offer_id"]) == "ACCEPTED"
    assert "editMessageReplyMarkup" not in [m for m, _ in api.calls[n:]]


def test_message_ids_repeat_across_chats_without_colliding(client, dsn, keys, api, transport):
    a, b = add_driver(dsn), add_driver(dsn)
    ma, mb = link_me(client, keys, a["drv"]), link_me(client, keys, b["drv"])
    oa, ob = send_offer(client, keys, a), send_offer(client, keys, b)
    api.ids.pop(str(ma.uid), None), api.ids.pop(str(mb.uid), None)     # both chats now answer message_id 1
    flush(dsn, transport)
    ra, rb = _offer_ref(dsn, oa), _offer_ref(dsn, ob)
    assert ra.endswith(".1") and rb.endswith(".1") and ra != rb
    assert q(dsn, "SELECT status FROM notifications WHERE chat_message_id IN (%s,%s)",
             oa["read_back_message_id"], ob["read_back_message_id"]) == [("SENT",), ("SENT",)]


def test_a_reference_from_another_chat_is_never_edited():
    from setuhaul.notify.telegram import message_id_for_chat, provider_ref
    ref = provider_ref("5550001001", 42)
    assert message_id_for_chat(ref, "5550001001") == 42
    assert message_id_for_chat(ref, "5550001002") is None
    assert message_id_for_chat("tg.42", "5550001001") is None and message_id_for_chat(None, "1") is None
    assert "5550001001" not in ref


# ================================================================ console labels: Telegram shown as Telegram
def test_delivery_labels_and_the_shown_conversation_state():
    from setuhaul.api.service import BookingService as S
    from setuhaul.notify.telegram import TelegramTestSink, provider_ref
    assert S._delivery(provider_ref("5550001001", 7)) == "telegram"
    assert S._delivery(TelegramTestSink().send("5550001001", "t", "k").provider_message_id) == "telegram_test_sink"
    assert S._delivery("testsink.0b7f") == "test_sink" and S._delivery("wamid.HBg") == "provider"
    assert S._shown_flow("SUBMITTED", "REJECTED") == "CLOSED" and S._shown_flow("SUBMITTED", "CONFIRMED") == "BOOKED"
    assert S._shown_flow("SUBMITTED", "PENDING_CONFIRMATION") == "SUBMITTED" and S._shown_flow("HELD", None) == "HELD"


def test_health_reports_the_telegram_mode_and_never_its_secrets(client, transport):
    h = client.get("/health").json()
    assert h["telegram"] == "test_bot" and h["booking_enabled"] is False
    assert TOKEN not in json.dumps(h) and not any(str(x) in json.dumps(h) for x in TEST_ACCOUNTS)


def test_only_the_local_runner_is_labelled_a_local_harness(dsn, keys):
    base = dict(dsn=dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                meta_app_secret=META_SECRET, facilities=(JAI, GGN), jwks=keys["jwks"], booking_enabled=True,
                local_test_harness=True)
    runner = TestClient(create_app(Settings(**base, local_runner=True))).get("/health").json()
    assert runner["booking_state"] == "local_test_harness" and runner["booking_enabled"] is False
    assert runner["booking_kill_switch"] in ("ENGAGED", "disengaged")
    other = TestClient(create_app(Settings(**base))).get("/health").json()
    assert other["booking_state"] != "local_test_harness"       # deployment verdicts are never masked
