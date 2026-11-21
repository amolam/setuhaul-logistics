"""App-layer gate: the agent proposes, the flow allows or refuses. Explicit-yes classification."""
import pytest

from setuhaul.domain.booking import BookingFlow, S, classify_confirmation, idem_key, map_db_error

OPTS = {"o1": {"dock": "D2", "start": "13:00"}, "o2": {"dock": "D3", "start": "14:00"}}


def flow_at_selected():
    f = BookingFlow("THR1")
    f.options_shown(OPTS)
    f.option_selected("o1")
    return f


def test_happy_path_order_is_enforced():
    f = BookingFlow("THR1")
    assert not f.allow("hold_option", {"option_id": "o1"}).allowed          # nothing shown yet
    f.options_shown(OPTS)
    assert f.allow("hold_option", {"option_id": "o1"}).reason == "NOT_ALLOWED_IN_OPTIONS_SHOWN"   # "2" is not a yes
    f.option_selected("o1")
    assert not f.allow("hold_option", {"option_id": "o1"}).allowed          # read-back sent, no yes yet
    assert f.driver_replied(101, "haan ji, kar do") == "YES"
    assert f.state is S.CONFIRMED_BY_DRIVER and f.yes_message_id == 101
    assert f.allow("hold_option", {"option_id": "o1"}).allowed
    assert f.allow("hold_option", {"option_id": "o2"}).reason == "NO_EXPLICIT_YES_FOR_THIS_OPTION"
    f.hold_result("HELD", "o1")
    assert f.allow("submit_booking_request", {"option_id": "o1"}).allowed
    assert f.allow("submit_booking_request", {"option_id": "o2"}).reason == "NOT_THE_HELD_OPTION"
    f.submit_result("PENDING", "APT-1")
    assert f.state is S.SUBMITTED
    f.warehouse_decided("CONFIRMED")
    assert f.state is S.BOOKED


def test_invented_option_refused():
    f = BookingFlow("THR1")
    f.options_shown(OPTS)
    assert f.allow("select_option", {"option_id": "o9"}).reason == "OPTION_NOT_SHOWN_IN_THREAD"
    with pytest.raises(PermissionError):
        f.option_selected("o9")


def test_tools_never_available_to_the_agent():
    f = flow_at_selected()
    for tool in ("decide_booking_request", "approve", "block_dock", "set_automation_paused", "move_appointment",
                 "change_priority", "run_sql"):
        assert f.allow(tool).reason == "TOOL_NOT_AVAILABLE_TO_AGENT"


def test_human_takeover_stops_everything():
    f = flow_at_selected()
    f.human_takeover()
    for tool in ("find_feasible_options", "hold_option", "get_booking_status", "escalate_to_human"):
        assert f.allow(tool, {"option_id": "o1"}).reason == "HUMAN_ACTIVE"


def test_no_or_unclear_does_not_hold():
    f = flow_at_selected()
    assert f.driver_replied(5, "the second one works but I must leave by 9") == "UNCLEAR"
    assert f.state is S.SELECTED and not f.allow("hold_option", {"option_id": "o1"}).allowed
    assert f.driver_replied(6, "nahi, rehne do") == "NO"
    assert f.state is S.OPTIONS_SHOWN


def test_lost_hold_returns_fresh_options():
    f = flow_at_selected()
    f.driver_replied(7, "yes")
    f.hold_result("LOST", "o1", fresh={"o3": {"dock": "D1"}})
    assert f.state is S.OPTIONS_SHOWN and set(f.shown) == {"o3"}
    assert not f.allow("hold_option", {"option_id": "o1"}).allowed


@pytest.mark.parametrize("text", ["yes", "Yes!", "ok", "OK 👍", "👍", "haan", "Haan ji", "theek hai", "thik hai bhai",
                                  "kar do", "book kar do", "confirm", "ji", "हाँ", "ठीक है", "हाँ जी", "go ahead",
                                  "yes please", "pakka", "✅"])
def test_yes(text):
    assert classify_confirmation(text) == "YES"


@pytest.mark.parametrize("text", ["no", "nahi", "Nahin", "cancel", "mat karo", "नहीं", "no thanks", "wait"])
def test_no(text):
    assert classify_confirmation(text) == "NO"


@pytest.mark.parametrize("text", ["yes but make it 2 pm", "ok, kya 3 baje ho sakta hai?", "haan lekin D3",
                                  "maybe", "if possible", "which dock?", "yes 2", "", "   ", "I am stuck in traffic",
                                  "ok?", "agar ho sake to", "हाँ लेकिन बाद में", "the second one works but I must leave by 9"])
def test_unclear(text):
    assert classify_confirmation(text) == "UNCLEAR"


def test_idem_key_canonical_and_turn_scoped():
    a = idem_key("THR1", "hold_option", {"option_id": "o1", "explicit_confirmation": True}, 7)
    b = idem_key("THR1", "hold_option", {"explicit_confirmation": True, "option_id": "o1"}, 7)
    c = idem_key("THR1", "hold_option", {"option_id": "o1", "explicit_confirmation": True}, 8)
    assert a == b != c and len(a) == 64


@pytest.mark.parametrize("state,msg,expected", [
    ("23P01", "conflicting key value violates exclusion constraint", "LOST"),
    ("42501", "permission denied for table", "FORBIDDEN"),
    ("P0001", "HUMAN_ACTIVE: a person has taken over", "REQUIRES_HUMAN"),
    ("P0001", "NO_CONFIRMER_AVAILABLE: shift NIGHT, ttl 0", "REQUIRES_HUMAN"),
    ("P0001", "REQUIRES_PERSON: rule FAC-JAI-01:person-safety", "REQUIRES_HUMAN"),
    ("P0001", "NO_DRIVER_CONFIRMATION", "NEEDS_INFO"),
    ("P0003", "OPTION_EXPIRED", "OPTION_EXPIRED"),
    ("P0001", "SOMETHING_NEW", "ERROR"),
])
def test_error_mapping(state, msg, expected):
    assert map_db_error(state, msg) == expected


def test_later_reply_overrides_attested_yes_in_the_flow():
    f = flow_at_selected()
    assert f.confirmation_attested(10, "att-1", "YES", "o1") == "YES" and f.state is S.CONFIRMED_BY_DRIVER
    assert f.confirmation_attested(11, "att-2", "UNCLEAR", "o1") == "UNCLEAR"
    assert f.state is S.SELECTED and f.attestation_id is None and not f.allow("hold_option", {"option_id": "o1"}).allowed
    f.confirmation_attested(12, "att-3", "YES", "o1")
    assert f.confirmation_attested(13, "att-4", "NO", "o1") == "NO" and f.state is S.OPTIONS_SHOWN
