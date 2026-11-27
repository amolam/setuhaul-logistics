"""Workstream C (27 Nov fix plan, issue 9): requested dates, the same for the model and the rules reader.

"Reschedule to 28 Nov" must mean 28 Nov in the facility's configured zone, explicit dates up to 7 days ahead, and
never another day silently. Unit tests (no database); the agent turn and the service checks are in
tests/test_reschedule_dates_0048.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from setuhaul.agent import dates, rules_extractor
from setuhaul.agent.extraction import RECORD_DRIVER_REPORT, SCHEMA_VERSION, Extraction, check
from setuhaul.agent.prompts import EXTRACT_SYSTEM, PROMPT_VERSION

IST = dates.facility_tz("Asia/Kolkata")
NOW = datetime(2026, 11, 27, 10, 30, tzinfo=IST)            # a Friday, 10:30 IST


def resolved(phrase, now=NOW, tz=IST):
    return dates.resolve(phrase, now, tz)


@pytest.mark.parametrize("phrase,day", [
    ("28 Nov", date(2026, 11, 28)), ("28th November", date(2026, 11, 28)), ("Nov 28", date(2026, 11, 28)),
    ("28/11", date(2026, 11, 28)), ("28-11-2026", date(2026, 11, 28)), ("2026-11-28", date(2026, 11, 28)),
    ("tomorrow", date(2026, 11, 28)), ("kal", date(2026, 11, 28)), ("कल", date(2026, 11, 28)),
    ("today", date(2026, 11, 27)), ("aaj", date(2026, 11, 27)), ("day after tomorrow", date(2026, 11, 29)),
    ("parso", date(2026, 11, 29)), ("Monday", date(2026, 11, 30)), ("on Sunday", date(2026, 11, 29)),
    ("4 Dec", date(2026, 12, 4)),                                                # exactly 7 days ahead: allowed
    ("Friday", date(2026, 12, 4)),                                               # today is Friday: the next one
])
def test_explicit_days_resolve_in_the_facility_zone(phrase, day):
    r = resolved(phrase)
    assert r.ok and r.day == day, (phrase, r)


@pytest.mark.parametrize("phrase,problem", [
    ("26 Nov", "REQUESTED_DATE_PAST"), ("5 Dec", "REQUESTED_DATE_TOO_FAR"), ("28 Dec", "REQUESTED_DATE_TOO_FAR"),
    ("next Friday", "REQUESTED_DATE_UNCLEAR"), ("31 Feb", "REQUESTED_DATE_UNCLEAR"),
    ("28 Nov | 29 Nov", "REQUESTED_DATE_CONFLICT"),
])
def test_unusable_days_are_refused_with_a_reason_never_replaced(phrase, problem):
    r = resolved(phrase)
    assert not r.ok and r.problem == problem


def test_the_zone_decides_what_today_is():
    late_utc = datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)                # 01:30 on 28 Nov in IST
    assert resolved("today", late_utc).day == date(2026, 11, 28)
    assert resolved("today", late_utc, timezone.utc).day == date(2026, 11, 27)
    assert dates.facility_tz("Asia/Kolkata").utcoffset(NOW) == timedelta(hours=5, minutes=30)


def test_year_end_rolls_over_only_within_the_limit():
    dec30 = datetime(2026, 12, 30, 9, 0, tzinfo=IST)
    assert resolved("2 Jan", dec30).day == date(2027, 1, 2)
    assert resolved("20 Dec", dec30).problem == "REQUESTED_DATE_PAST"           # never moved to next December


def test_phrases_are_found_in_driver_messages():
    f = dates.find_date_phrase
    assert f("Reschedule to 28 Nov please") == "28 Nov"
    assert f("can I come tomorrow after 2 pm") == "tomorrow"
    assert f("kal subah 10 baje slot chahiye") == "kal"
    assert f("27 Nov or 28 Nov") == "27 Nov | 28 Nov"
    assert f("reach at 4.30 pm") is None and f("late by 45 minutes") is None and f("dock 2") is None


# ================================================================ the extraction contract (schema v2, prompt p6)
def test_schema_and_prompt_carry_the_date_and_their_versions_changed():
    props = RECORD_DRIVER_REPORT["input_schema"]["properties"]
    assert "requested_date_local" in props and "requested_date_local" in RECORD_DRIVER_REPORT["input_schema"]["required"]
    assert "RESCHEDULE" in props["intent"]["enum"]
    assert SCHEMA_VERSION == "record_driver_report/v2" and PROMPT_VERSION.startswith("p6-")
    assert "requested_date_local" in EXTRACT_SYSTEM and "Never turn it into another form" in EXTRACT_SYSTEM
    assert Extraction(intent="OTHER").requested_date_local is None                # a v1 answer still parses


def _model(**kw):
    return Extraction(**{"intent": "RESCHEDULE", **kw})


def test_model_and_rules_reader_give_the_same_day():
    text = "Reschedule to 28 Nov"
    by_model = check(_model(requested_date_local="28 Nov"), NOW, None, text, [], tz=IST)
    by_rules = check(rules_extractor.extract(text, []), NOW, None, text, [], tz=IST)
    assert by_model.requested_date == by_rules.requested_date == date(2026, 11, 28)
    assert rules_extractor.extract(text, []).intent == "RESCHEDULE"


def test_a_day_the_model_missed_is_still_the_drivers_day():
    c = check(_model(requested_date_local=None), NOW, None, "Reschedule to 28 Nov", [], tz=IST)
    assert c.requested_date == date(2026, 11, 28)                                # never silently today


def test_model_and_driver_disagreeing_is_asked():
    c = check(_model(requested_date_local="29 Nov"), NOW, None, "Reschedule to 28 Nov", [], tz=IST)
    assert c.requested_date is None and "requested_date_conflict" in c.problems


def test_times_are_read_on_the_requested_day():
    c = check(_model(requested_date_local="tomorrow", earliest_start_local="14:00"), NOW, None,
              "tomorrow after 2 pm", [], tz=IST)
    assert c.earliest_start == datetime(2026, 11, 28, 14, 0, tzinfo=IST)
    # an ambiguous 12-hour time on another day: the earlier reading for a start, the later one for a deadline
    c = check(_model(requested_date_local="28 Nov", earliest_start_local="04:00", latest_finish_local="06:00",
                     latest_finish_kind="GATE_OUT"), NOW, None, "28 Nov after 4, gate out by 6", [], tz=IST)
    assert c.earliest_start.hour == 4 and c.latest_finish.hour == 18


def test_out_of_range_days_become_problems_not_dates():
    for text, prob in (("slot on 5 Dec", "requested_date_too_far"), ("slot on 25 Nov", "requested_date_past"),
                       ("next Friday slot", "requested_date_unclear")):
        c = check(rules_extractor.extract(text, []), NOW, None, text, [], tz=IST)
        assert c.requested_date is None and prob in c.problems, text


def test_messages_without_a_day_behave_exactly_as_before():
    for text in ("I will reach by 5 pm", "Traffic, late by 45 minutes", "any slot after 3"):
        ex = rules_extractor.extract(text, [])
        assert ex.requested_date_local is None
        c = check(ex, NOW, NOW + timedelta(hours=2), text, [], tz=IST)
        assert c.requested_date is None and not [p for p in c.problems if p.startswith("requested_date")]
