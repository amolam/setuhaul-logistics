"""Requested dates (27 Nov fix plan, issue 9): "reschedule to 28 Nov" must mean 28 Nov.

One deterministic resolver for both extractors. The model only COPIES the day phrase the driver wrote
(`requested_date_local`); the rules reader finds the same phrase with `find_date_phrase`; code alone turns it into a
date, in the facility's configured time zone. Rules (owner decision, 27 Nov 2026):

  * explicit dates up to MAX_DAYS_AHEAD (7) days ahead, today included;
  * a past date, a date beyond the limit, an unreadable phrase, or two different days in one message are refused with
    a reason, and the agent says so; it never silently substitutes another day;
  * a phrase without a year is the next occurrence of that day within the limit (a date that already passed this year
    is NOT moved to next year: that is beyond the limit and refused).

A weekday ("Friday") is the next such day after today. "Next Friday" is ambiguous (this coming one or the one after)
and is asked, never guessed. Hindi "kal" means tomorrow here: a driver asking for a slot talks about the future.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo

log = logging.getLogger("setuhaul.dates")

MAX_DAYS_AHEAD = 7
_FIXED = {"Asia/Kolkata": timezone(timedelta(hours=5, minutes=30), "IST"),     # no DST: exact
          "Asia/Calcutta": timezone(timedelta(hours=5, minutes=30), "IST"),
          "UTC": timezone.utc}


def facility_tz(name: str | None) -> tzinfo:
    """The facility's configured zone (facilities.timezone). zoneinfo when the runtime has the tz database; the fixed
    offset for the pilot's zone otherwise (Lambda images may ship without it). An unknown zone is a configuration
    error: logged, and the pilot zone is used (both pilot facilities are Asia/Kolkata)."""
    name = name or "Asia/Kolkata"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                                                # noqa: BLE001 — no tz database, or unknown
        if name in _FIXED:
            return _FIXED[name]
        log.error("unknown facility time zone %r; using Asia/Kolkata", name)
        return _FIXED["Asia/Kolkata"]


_MONTHS = {m: i for i, ms in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], 1) for m in ms}
_WEEKDAYS = {d: i for i, ds in enumerate(
    [("mon", "monday", "somvar", "somwar"), ("tue", "tues", "tuesday", "mangalvar", "mangalwar"),
     ("wed", "wednesday", "budhvar", "budhwar"), ("thu", "thur", "thurs", "thursday", "guruvar", "guruwar"),
     ("fri", "friday", "shukravar", "shukrawar"), ("sat", "saturday", "shanivar", "shaniwar"),
     ("sun", "sunday", "ravivar", "raviwar", "itvar", "itwar")]) for d in ds}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_WD = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|" \
      r"som[vw]ar|mangal[vw]ar|budh[vw]ar|guru[vw]ar|shukra[vw]ar|shani[vw]ar|ravi[vw]ar|it[vw]ar)"
# Phrases, most specific first. Each finds one day mention in a driver's message.
_PATTERNS = [
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("dmy", re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b(?!\s*(?:am|pm|baje|bje|hrs?|:))", re.I)),
    ("d_mon", re.compile(r"\b" + _DAY + r"\s*(?:of\s+)?" + _MON + r"\b\.?(?:\s*,?\s*(\d{4}))?", re.I)),
    ("mon_d", re.compile(r"\b" + _MON + r"\.?\s+" + _DAY + r"\b(?:\s*,?\s*(\d{4}))?", re.I)),
    ("next_wd", re.compile(r"\b(?:next|agle|agla)\s+" + _WD + r"\b", re.I)),
    ("wd", re.compile(r"\b(?:this\s+|on\s+|is\s+)?" + _WD + r"\b", re.I)),
    ("day_after", re.compile(r"\b(?:day\s+after\s+tomorrow|parso[n]?|parson)\b", re.I)),
    ("tomorrow", re.compile(r"\b(?:tomorrow|tmrw|tmr|kal)\b|कल", re.I)),
    ("today", re.compile(r"\b(?:today|aaj|aj)\b|आज", re.I)),
]
# "kal" in "kal se pehle" / "kal raat" is still tomorrow for a booking; "aj" alone is too short to trust mid-word
_NOT_A_DATE_DMY = re.compile(r"\b\d{1,2}[.:]\d{2}\s*(?:am|pm)?\b", re.I)          # 4.30 is a time


@dataclass(frozen=True)
class Resolved:
    day: date | None
    problem: str | None = None        # REQUESTED_DATE_PAST | _TOO_FAR | _UNCLEAR | _CONFLICT
    phrase: str | None = None

    @property
    def ok(self) -> bool:
        return self.day is not None and self.problem is None


def find_date_phrases(text: str) -> list[str]:
    """Every day mention in the message, as written (left to right, non-overlapping)."""
    t = text or ""
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for kind, rx in _PATTERNS:
        for m in rx.finditer(t):
            a, z = m.span()
            if any(a < zz and z > aa for aa, zz in taken):
                continue
            if kind == "dmy" and _NOT_A_DATE_DMY.fullmatch(m.group(0)):
                continue
            if kind == "dmy" and not (1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12):
                continue
            if kind == "wd" and m.group(0).strip().lower() in ("sat", "sun", "mon", "wed") and \
                    not re.match(r"(this|on|is)\s", m.group(0), re.I):
                continue                                             # bare 3-letter words are too often not days
            taken.append((a, z))
            found.append((a, m.group(0).strip()))
    return [p for _, p in sorted(found)]


def find_date_phrase(text: str) -> str | None:
    """The day phrase of a message for the rules reader: None when none; a JOINED marker when two different ones."""
    ps = find_date_phrases(text)
    if not ps:
        return None
    return ps[0] if len(ps) == 1 else " | ".join(ps)


def _resolve_one(phrase: str, today: date) -> date | str:
    p = phrase.strip()
    low = p.lower()
    for kind, rx in _PATTERNS:
        m = rx.fullmatch(p) or (rx.search(p) if kind in ("tomorrow", "today", "day_after", "wd", "next_wd") else None)
        if not m:
            continue
        if kind == "iso":
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return _safe_date(y, mo, d)
        if kind == "dmy":
            d, mo = int(m.group(1)), int(m.group(2))
            y = m.group(3)
            if y:
                y = int(y) + (2000 if len(y) == 2 else 0)
                return _safe_date(y, mo, d)
            return _next_occurrence(today, mo, d)
        if kind in ("d_mon", "mon_d"):
            if kind == "d_mon":
                d, mon, y = m.group(1), m.group(2), m.group(3)
            else:
                mon, d, y = m.group(1), m.group(2), m.group(3)
            mo = _MONTHS.get(mon.lower().rstrip(".")) or _MONTHS.get(mon.lower()[:3])
            if not mo:
                return "REQUESTED_DATE_UNCLEAR"
            return _safe_date(int(y), mo, int(d)) if y else _next_occurrence(today, mo, int(d))
        if kind == "next_wd":
            return "REQUESTED_DATE_UNCLEAR"                       # this coming one, or the one after? ask
        if kind == "wd":
            wd = _WEEKDAYS.get(m.group(1).lower()) if m.lastindex else None
            if wd is None:
                return "REQUESTED_DATE_UNCLEAR"
            ahead = (wd - today.weekday()) % 7 or 7               # the next one after today ("Friday" on a Friday: +7)
            return today + timedelta(days=ahead)
        if kind == "day_after":
            return today + timedelta(days=2)
        if kind == "tomorrow":
            return today + timedelta(days=1)
        if kind == "today":
            return today
    if low in ("", "none", "null"):
        return "REQUESTED_DATE_UNCLEAR"
    return "REQUESTED_DATE_UNCLEAR"


def _safe_date(y: int, mo: int, d: int) -> date | str:
    try:
        return date(y, mo, d)
    except ValueError:
        return "REQUESTED_DATE_UNCLEAR"


def _next_occurrence(today: date, mo: int, d: int) -> date | str:
    """A day without a year: this year's, or next year's only when that is still within the limit (late December)."""
    this = _safe_date(today.year, mo, d)
    if isinstance(this, str):
        return this
    if this >= today:
        return this
    nxt = _safe_date(today.year + 1, mo, d)
    if isinstance(nxt, date) and (nxt - today).days <= MAX_DAYS_AHEAD:
        return nxt
    return this                                                  # in the past: refused by the range check


def resolve(phrase: str | None, now: datetime, tz: tzinfo) -> Resolved:
    """The requested day in the facility's zone, or why it cannot be used. None phrase = no day was named."""
    if phrase is None or not str(phrase).strip():
        return Resolved(None)
    if now.tzinfo is None:
        raise ValueError("resolve needs a timezone-aware now")
    today = now.astimezone(tz).date()
    parts = [x for x in str(phrase).split("|") if x.strip()]
    days = []
    for part in parts:
        r = _resolve_one(part, today)
        if isinstance(r, str):
            return Resolved(None, r, phrase)
        days.append(r)
    if len(set(days)) > 1:
        return Resolved(None, "REQUESTED_DATE_CONFLICT", phrase)
    day = days[0]
    if day < today:
        return Resolved(day, "REQUESTED_DATE_PAST", phrase)
    if (day - today).days > MAX_DAYS_AHEAD:
        return Resolved(day, "REQUESTED_DATE_TOO_FAR", phrase)
    return Resolved(day, None, phrase)


def last_bookable_day(now: datetime, tz: tzinfo) -> date:
    return now.astimezone(tz).date() + timedelta(days=MAX_DAYS_AHEAD)


def day_label(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def at_on_day(hhmm: str | None, day: date, tz: tzinfo, pick: str = "earliest") -> datetime | None:
    """A stated time on the requested day, in the facility's zone. The model writes a 12-hour time without AM/PM as
    written ("4" -> "04:00"), so 01:00-11:59 is ambiguous on another day: `pick` takes the earlier reading for an
    earliest start (both readings stay in the options shown, each with its date) and the later one for a deadline or
    an arrival (plan for the late end), so neither reading is silently excluded."""
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", hhmm or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if pick == "latest" and 1 <= h <= 11:
        h += 12
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)
