"""Rules-only extractor: the no-AI baseline for the LLM-vs-code ablation (07_acceptance §4, backlog B-AI-3).

It fills the same `record_driver_report` form with regular expressions and word lists. Haiku is kept only if it
beats this baseline by enough to pay for itself on the golden set. It also serves as the degraded-mode extractor
when the model is unavailable.
"""
from __future__ import annotations

import re

from ..domain.booking import classify_confirmation
from .extraction import SAFETY_WORDS, Extraction

# S4-R2-08 reading test: drivers spell "pahunch" many ways ("pohonch jaunga", "pohunchuga", "pochunga")
_PAH = r"(?:pahunch|pahuch|pahonch|pohonch|pohunch|pohnch|pohuch|pochunch|pochun|pocho)\w*"
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH = re.compile(r"\b(hai|nahi|haan|bhai|ji|kya|kab|der|late ho|pahunch|raha|rahi|hoon|hun|hu|baje|ghanta|minute|"
                       r"kal|subah|shaam|jaunga|kar|sakte|dijiye)\b|" + _PAH, re.I)
_TIME = r"(\d{1,2})(?!\d)(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
_NOT_DURATION = r"(?!\s*(?:min|minute|hour|hr|h\b|ghant))"          # "late by 45 minutes" is not 45 o'clock
# An arrival verb right before the time is an arrival time. "by/around/at" alone is weak: "closes at 12" is not (H-1 review)
_ETA = re.compile(r"\b(?:reach(?:ing)?|arriv(?:e|ing|al)|eta|" + _PAH + r")\s*(?:around\s*|by\s*|at\s*)?" + _TIME + _NOT_DURATION, re.I)
_ETA_WEAK = re.compile(r"\b(?:by|around|at)\s*(?:around\s*)?" + _TIME + _NOT_DURATION, re.I)
_AFTER = re.compile(r"(?:after|ke baad|baad)\s*" + _TIME, re.I)
_BEFORE = re.compile(r"(?:before|leave (?:the warehouse )?(?:by|before)|se pehle)\s*" + _TIME, re.I)
_BEFORE_HI = re.compile(r"(?:" + r"(subah|savere|morning|dopahar|afternoon|shaam|sham|evening|raat|night)" +
                        r"\s*(?:ke\s*|ko\s*)?)?" + _TIME + r"\s*(?:baje|bje)?\s*(?:se\s*pehle|se\s*pahle|tak)\s+.*\bnikal", re.I)
_RANGE = re.compile(_TIME + r"\s*(?:-|–|to|se)\s*" + _TIME, re.I)
_DUR = re.compile(r"(?:late by|delay(?:ed)? by|late|der)\s*(?:of\s*)?(?:about|around|approx\.?)?\s*"
                  r"(\d+|one|two|three|half an?|an?)\s*(hours?|hrs?|h|minutes?|mins?|ghant[ae]|minute)", re.I)
_DUR2 = re.compile(r"(\d+|one|two|three|half an?|an?)\s*(hours?|hrs?|minutes?|mins?|ghant[ae])\s*(?:late|der|delay)", re.I)
# Hindi word order puts the time first: "12 baje", "shaam 6 baje pahunchunga" (human test H-1, 20 Nov)
_DAYPART = r"(subah|savere|morning|dopahar|afternoon|shaam|sham|evening|raat|night)"
_ETA_HI = re.compile(r"(?:" + _DAYPART + r"\s*(?:ke\s*|ko\s*)?)?" + _TIME + r"\s*(?:baje|bje)(?![a-z])", re.I)
_ETA_HI_ARR = re.compile(_ETA_HI.pattern + r"\s*(?:tak\s*|ko\s*|ke\s*aas\s*paas\s*)?(?:" + _PAH + r"|reach|arriv|aa\s*ja|aaung|aaoong|aayeng)", re.I)
# A time-first phrase without an arrival verb ("12 baje", "at 3") is read clause by clause (H-1r review):
#  - nothing in the time's own clause, before OR after it, may give the time another subject (call, close, lunch...);
#  - its clause must say arriving ("12 baje tak pahunch"), or be the bare time alone ("late ho jayega, raat 10 baje")
#    in a message about arriving late, or answer the agent's arrival-time question.
_CLAUSE_BREAK = re.compile(r"[,;!?\n]|\.(?=\s|$)|\s[-–]\s|\b(?:and|but|then|so|aur|phir|fir|lekin|magar|par)\b", re.I)
_ARRIVAL_VERB = re.compile(r"\b(" + _PAH + r"|reach\w*|arriv\w*|eta|aa\s*ja\w*|aaung\w*|aaoong\w*|aayeng\w*|be there|get there)\b", re.I)
_FILLER = {"tak", "ko", "ke", "ki", "around", "approx", "about", "by", "at", "ji", "bhai", "hoga", "hogi", "hai", "ho", "jayega",
           "jaega", "jayegi", "jaunga", "se", "karib", "lagbhag", "takriban", "pe", "on", "baje", "bje", "oclock", "i", "will",
           "be", "im", "am", "ill", "maybe", "shayad", "now", "ab", "abhi", "only", "sirf", "approximately", "roughly", "hrs", "hr"}
# Something that happens AT a time and so can own a bare time in the next clause. A request such as "call me if
# needed" is not one: it has no time of its own ("late, around 3 pm, call me if needed" is an arrival at 3 pm).
_EVENT_OWNER = re.compile(r"\b(clos\w*|band|khul\w*|open\w*|meeting|lunch|break|shift|office|dukaan|shop|bank|school|"
                          r"appointment|doctor|hospital|train|flight|bus|function|shaadi|party)\b", re.I)
_ARRIVAL_CTX = re.compile(r"\b(" + _PAH + r"|reach\w*|arriv\w*|eta|aa\s*ja\w*|aaung\w*|aaoong\w*|aayeng\w*|be there|get there|"
                          r"late|der|delay\w*|stuck|traffic|jam)\b", re.I)
_NON_ARRIVAL = re.compile(r"\b(call\w*|phone|ring|clos\w*|open\w*|khul\w*|band|shift|lunch|break|meeting|leav\w*|"
                          r"nikal\w*|sleep|so\s*jaa?\w*|gate.?out|unload\w*\s+(?:till|until|by))\b", re.I)
# A bare time, as the whole answer to "what time will you reach?": "1 pm", "12 ko", "dopahar 12", "12:30 tak"
_BARE = re.compile(r"\s*(?:" + _DAYPART + r"\s*(?:ke\s*|ko\s*)?)?" + _TIME +
                   r"\s*(?:baje|bje|o'?clock|hrs?)?\s*(?:ke\s*)?(?:ko|tak|around|approx\.?)?\s*"
                   r"(?:(?:" + _PAH + r"|reach\w*|aa\w*)(?:\s+\w+)?)?\s*[.!]*\s*", re.I)
# "Mera late hai - 3 ghanta": a duration anywhere in a message that says it is late (human test H-1)
_LATE_WORD = re.compile(r"\b(late|der|delay\w*|let)\b", re.I)
_DUR_ANY = re.compile(r"(\d+|one|two|three|half an?|an?)\s*(hours?|hrs?|minutes?|mins?|ghant[ae]|minute)\b", re.I)
ETA_QUESTIONS = ("declared_eta_local", "delay_kind")      # the agent just asked for (or to confirm) the arrival time
_REF = re.compile(r"\b(ORD-\d{6}-\d{3}|SHP\d{4})\b", re.I)
_ORDINAL = {"first": 1, "1st": 1, "pehla": 1, "second": 2, "2nd": 2, "dusra": 2, "doosra": 2, "third": 3, "3rd": 3, "teesra": 3}
WORDS = {"one": 1, "two": 2, "three": 3, "a": 1, "an": 1, "half a": 0.5, "half an": 0.5}


def _hhmm(h: str, m: str | None, ap: str | None) -> str:
    hh, mm = int(h), int(m or 0)
    ap = (ap or "").lower().replace(".", "")
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    return f"{hh:02d}:{mm:02d}"


def _daypart_hhmm(dp: str | None, h: str, m: str | None, ap: str | None) -> str:
    """Time with an optional Hindi/English part of the day. Without am/pm or a day part the hour stays ambiguous and
    local_to_ts picks the nearest sensible occurrence ("1 baje" at 08:40 is 13:00)."""
    hh, dp = int(h), (dp or "").lower()
    if not ap and dp:
        if dp in ("subah", "savere", "morning"):
            ap = "am"
        elif dp in ("dopahar", "afternoon", "shaam", "sham", "evening"):
            ap = "pm"
        elif dp in ("raat", "night"):
            ap = "pm" if hh >= 6 else "am"                     # "raat 10 baje" = 22:00, "raat 2 baje" = 02:00
    return _hhmm(h, m, ap)


def _minutes(n: str, unit: str) -> int:
    v = WORDS.get(n.lower(), None)
    v = float(n) if v is None else v
    return int(round(v * (60 if unit.lower().startswith(("h", "g")) else 1)))


def extract(text: str, shown_option_ids: list[str] | None = None, awaiting_confirmation: bool = False,
            asked: str | None = None) -> Extraction:
    """`asked` is the question the agent asked last turn (stored in turn facts). After an arrival-time question, a
    bare time is the answer."""
    t = text or ""
    low = t.lower()
    lang = "hi" if _DEVANAGARI.search(t) else ("hi-Latn" if _HINGLISH.search(low) else "en")
    out = dict(intent="OTHER", delay_kind="UNKNOWN", exception_type="UNKNOWN", language=lang,
               safety_concern=bool(SAFETY_WORDS.search(t)), explicit_confirmation=False)

    if m := _REF.search(t):
        out["shipment_ref_mentioned"] = m.group(1).upper()
    if re.search(r"breakdown|broke down|tyre|tire|puncture|repair|engine|kharab", low):
        out["exception_type"] = "BREAKDOWN"
        done = re.search(r"\b(repair\w*|kaam|mistri|mechanic)\b.*\b(ho gaya|ho gayi|hogaya|ho gya|khatam|done|complete\w*)\b", low)
        out["vehicle_fixed"] = True if (re.search(r"repaired|fixed|theek ho gay", low) or
                                        (done and not re.search(r"\b(nahi|nhi|not|abhi tak)\b", low))) else (
            False if re.search(r"not fixed|still (broken|stuck)|abhi tak", low) else None)
    elif re.search(r"traffic|jam|congestion", low):
        out["exception_type"] = "TRAFFIC"
    elif re.search(r"rain|fog|storm|baarish|kohra", low):
        out["exception_type"] = "WEATHER"
    elif re.search(r"already at the gate|at the gate|reached early|early|jaldi (?:" + _PAH + r"|aa)|"
                   r"(?:warehouse|gate) ke paas", low):
        out["exception_type"] = "EARLY_ARRIVAL"

    # times
    if m := _RANGE.search(t):
        dp = re.search(_DAYPART + r"\s*(?:ke\s*|ko\s*)?$", t[:m.start()], re.I)    # "Shaam 4 se 6" (reading test)
        if m.group(3) or m.group(6) or ":" in m.group(0) or dp:
            d = dp.group(1) if dp else None
            out["delay_kind"] = "RANGE"
            out["declared_eta_range_local"] = [_daypart_hhmm(d, m.group(1), m.group(2), m.group(3)),
                                               _daypart_hhmm(d, m.group(4), m.group(5), m.group(6))]
    late_msg = bool(_ARRIVAL_CTX.search(t))

    def _clause(m) -> tuple[str, str]:
        """The clause around a match, as (text before the match, text after it), cut at punctuation and conjunctions."""
        a = max((b.end() for b in _CLAUSE_BREAK.finditer(t, 0, m.start())), default=0)
        z = next((b.start() for b in _CLAUSE_BREAK.finditer(t, m.end())), len(t))
        return t[a:m.start()], t[m.end():z]

    def _about_arriving(m) -> bool:
        pre, post = _clause(m)
        if _NON_ARRIVAL.search(pre) or _NON_ARRIVAL.search(post):       # "12 baje office band", "at 1 pm lunch break"
            return False
        if _ARRIVAL_VERB.search(pre + " " + post) or asked in ETA_QUESTIONS:
            return True
        rest = [w for w in re.findall(r"[a-z']+", (pre + " " + post).lower().replace("'", "")) if w not in _FILLER]
        if not (late_msg and not rest):
            return False
        # A bare time alone in its clause ("late ho jayega, raat 10 baje"). The clause next to it may still own the time:
        # "late, 12 baje, office band" (H-1r2 review). Then this is no arrival time: the agent asks for one instead.
        return not any(_EVENT_OWNER.search(x) for x in _neighbours(m))

    def _neighbours(m) -> list[str]:
        """The clause just before and the clause just after the match's own clause."""
        cuts = [(b.start(), b.end()) for b in _CLAUSE_BREAK.finditer(t)]
        before = [c for c in cuts if c[1] <= m.start()]
        after = [c for c in cuts if c[0] >= m.end()]
        prev = t[(before[-2][1] if len(before) > 1 else 0):before[-1][0]] if before else ""
        nxt = t[after[0][1]:(after[1][0] if len(after) > 1 else len(t))] if after else ""
        return [prev, nxt]

    def _first_weak(rx):
        return next((m for m in rx.finditer(t) if _about_arriving(m)), None)

    if out["delay_kind"] == "UNKNOWN" and (m := _ETA.search(t)):           # "reach 11:20", "ETA 6:30"
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _hhmm(m.group(1), m.group(2), m.group(3))
    if out["delay_kind"] == "UNKNOWN" and (m := _ETA_HI_ARR.search(t) or _first_weak(_ETA_HI)):   # "6 baje pahunchunga"
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _daypart_hhmm(*m.group(1, 2, 3, 4))
    if out["delay_kind"] == "UNKNOWN" and (m := _first_weak(_ETA_WEAK)):    # "late, around 3 pm"
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _hhmm(m.group(1), m.group(2), m.group(3))
    if out["delay_kind"] == "UNKNOWN" and asked in ETA_QUESTIONS and (m := _BARE.fullmatch(t)) and int(m.group(2)) <= 23:
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _daypart_hhmm(*m.group(1, 2, 3, 4))
    if out["delay_kind"] == "UNKNOWN" and (m := (_DUR.search(t) or _DUR2.search(t) or
                                                 (_DUR_ANY.search(t) if _LATE_WORD.search(low) else None))):
        mins = _minutes(m.group(1), m.group(2))
        out["declared_delay_min"] = mins
        out["delay_kind"] = "REPAIR_DURATION" if out["exception_type"] == "BREAKDOWN" and "repair" in low else "DELAY_DURATION"
    if (m := _AFTER.search(t)) and not any(_NON_ARRIVAL.search(s) for s in _clause(m)):   # not "ke baad 12 baje call"
        out["earliest_start_local"] = _hhmm(m.group(1), m.group(2), m.group(3) or ("pm" if int(m.group(1)) < 7 else None))
    if m := _BEFORE.search(t):
        out["latest_finish_local"] = _hhmm(m.group(1), m.group(2), m.group(3) or ("pm" if int(m.group(1)) < 7 else None))
    elif m := _BEFORE_HI.search(t):                                  # "raat 10 baje se pehle gate se nikalna hai"
        out["latest_finish_local"] = _daypart_hhmm(m.group(1), m.group(2), m.group(3),
                                                   m.group(4) or (None if m.group(1) else ("pm" if int(m.group(2)) < 7 else None)))
    if out.get("latest_finish_local"):
        # only an explicit gate-out counts; "leave the warehouse by 1:30" is ambiguous (Phase 1 MSG004) → ask
        out["latest_finish_kind"] = "GATE_OUT" if re.search(r"gate.?out|exit the gate|gate se nikal", low) else "UNKNOWN"

    # option choice and confirmation
    if shown_option_ids:
        n = None
        if m := re.fullmatch(r"\s*(?:option\s*)?#?(\d)\s*\.?\s*", low):
            n = int(m.group(1))
        elif m := re.search(r"\b(\d)\s*(?:number|no\.?|nambar|wala|vala)\b", low):   # "1 number ka kar dijiye"
            n = int(m.group(1))
        else:
            n = next((v for k, v in _ORDINAL.items() if re.search(rf"\b{k}\b", low)), None)
        if n and 1 <= n <= len(shown_option_ids):
            out["selected_option_id"] = shown_option_ids[n - 1]
    if awaiting_confirmation and classify_confirmation(t) == "YES":
        out["explicit_confirmation"] = True

    # intent
    if out["safety_concern"]:
        out["intent"] = "REPORT_DELAY"
    elif re.search(r"cancel|don.?t need|nahi chahiye", low):
        out["intent"] = "CANCEL"
    elif out["explicit_confirmation"] or out.get("selected_option_id"):
        out["intent"] = "BOOK"
    elif out["exception_type"] == "EARLY_ARRIVAL":
        out["intent"] = "EARLY_ARRIVAL"
    elif re.search(r"still active|status|has a new dock|been assigned|confirmed\?|kya .*confirm", low):
        out["intent"] = "CHECK_STATUS"
    elif out["delay_kind"] != "UNKNOWN" or out["exception_type"] in ("BREAKDOWN", "TRAFFIC", "WEATHER") or "late" in low:
        out["intent"] = "REPORT_DELAY"
    elif re.search(r"slot|dock|options?|available", low):
        out["intent"] = "ASK_OPTIONS"
    elif _ARRIVAL_VERB.search(t):                                    # "Kal subah pochunga": about arriving, no time
        out["intent"] = "REPORT_DELAY"
    elif out.get("latest_finish_local") or out.get("earliest_start_local"):   # "10 baje se pehle nikalna hai": a slot
        out["intent"] = "ASK_OPTIONS"
    if out["intent"] == "REPORT_DELAY" and out["exception_type"] == "UNKNOWN":
        out["exception_type"] = "DELAY"
    return Extraction(**out)
