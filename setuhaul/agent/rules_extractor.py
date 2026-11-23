"""Rules-only extractor: the no-AI baseline for the LLM-vs-code ablation (07_acceptance §4, backlog B-AI-3).

It fills the same `record_driver_report` form with regular expressions and word lists. Haiku is kept only if it
beats this baseline by enough to pay for itself on the golden set. It also serves as the degraded-mode extractor
when the model is unavailable.
"""
from __future__ import annotations

import re

from ..domain.booking import classify_confirmation
from .extraction import SAFETY_WORDS, Extraction, kind_stated

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
_ARRIVAL_VERB = re.compile(r"\b(" + _PAH + r"|reach\w*|arriv\w*|eta|aa\s*ja\w*|aaung\w*|aaoong\w*|aayeng\w*|be there|get there|"
                           r"be at (?:the )?(?:gate|facility|warehouse|godown|dock|dc))\b", re.I)   # G.1: "be at the gate by 5"
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

# ---------------------------------------------------------------- G.1 (I14): completion deadlines
# "Need to finish unloading by 5 pm" is a deadline for the END of dock work (latest_finish_local), never an arrival time
# and never a slot start. The verb of completion must govern the time: "finish/complete/be done ... by|before|till T",
# or "(un)loading should be completed before T". Times are wall-clock in the facility's time zone (extraction.FACILITY_TZ).
_DONE_VERB = (r"(?:finish(?:ed)?|complete(?:d)?|wrap(?:ped)?\s+up|be\s+(?:done|finished|completed|over|free)|"
              r"get\s+(?:it\s+|this\s+)?done|khatam|khatm|poora|pura)")
_DOCK_WORK = r"(?:(?:the|my|our)\s+)?(?:un)?load(?:ing)?(?:\s+work)?"
_BY = r"(?:by|before|till|until|no\s+later\s+than)"
_DEADLINE = re.compile(r"\b(?:" + _DONE_VERB + r"\s+(?:" + _DOCK_WORK + r"\s+)?(?:at\s+the\s+dock\s+)?" + _BY + r"|"
                       + _DOCK_WORK + r"\s+(?:should|must|has\s+to|needs?\s+to|to)?\s*(?:be\s+)?"
                       r"(?:completed|finished|done|over)\s+" + _BY + r")\s*(?:around\s+|about\s+)?" + _TIME + _NOT_DURATION,
                       re.I)
# Hinglish word order: "5 baje tak unloading khatam karni hai", "5 baje se pehle maal utar jana chahiye"
_DEADLINE_HI = re.compile(_TIME + r"\s*(?:baje|bje)?\s*(?:tak|se\s*pehle|se\s*pahle)\s+(?:\w+\s+){0,2}?"
                          r"(?:unload\w*|khali|maal\s+utar\w*|kaam)\s+(?:\w+\s+){0,1}?(?:khatam|khatm|poora|pura|ho\s+ja\w*|utar\s+ja\w*)",
                          re.I)
# "Finish before facility closing": the facility's closing time is already a hard constraint of every option (the
# feasibility engine never offers a slot that ends after close), so this is a request for options, with no time.
_DEADLINE_CLOSE = re.compile(_DONE_VERB + r"\b[^.?!]{0,40}?\b(?:before|by)\s+(?:the\s+)?(?:facility|warehouse|dc|godown|"
                             r"dock|gate)?\s*(?:clos\w*|band\s+ho\w*|shut\w*)", re.I)
_DOCK_WORD = re.compile(r"\b(?:un)?loading\b|\bunload\w*|\bkhali\b|\butar\w*", re.I)

# ---------------------------------------------------------------- G.1 (I15): the truck is at the facility now
# Present-state arrival, in English and Hinglish: a completed arrival verb ("reached", "pahunch gaya", "aa gaya") or a
# present location ("I am at the gate", "gate pe khada hoon", "facility ke bahar hai"). Not arrival: a future or
# planned arrival ("pahunchunga", "will reach", "5 baje"), a negation ("abhi nahi pahuncha", "not reached yet"), a
# question ("kya gate pe aa gaya?"), or reaching another named place ("Jaipur pahunch gaya", "reached Chomu").
_LOC = (r"(?:main\s+gate|gate|facility|warehouse|godown|dc|dock|yard|plant|depot|site|premises|destination|"
        r"factory|unloading\s+point)")
_PERFECTIVE_HI = r"(?:gaya|gayi|gaye|gya|gyi|gye|chuka|chuki|chuke)"
_ARRIVED_HI = re.compile(r"\b(" + _PAH + r"|aa|a)\s*" + _PERFECTIVE_HI + r"\b", re.I)
_ARRIVED_EN = re.compile(r"\b(?:reached|arrived|got\s+here|made\s+it|pulled\s+in)\b", re.I)
_PRESENT_EN = re.compile(r"\b(?:i\s*am|i'?m|im|we\s*are|we'?re|truck\s+is|vehicle\s+is|lorry\s+is|driver\s+is)\s+"
                         r"(?:now\s+|already\s+|just\s+|standing\s+|waiting\s+|parked\s+|here\s+)?"
                         r"(?:at|near|outside|in\s+front\s+of|by)\s+(?:the\s+|your\s+)?" + _LOC + r"\b", re.I)
_PRESENT_HI = re.compile(r"\b" + _LOC + r"\s*(?:pe|par|pr|me|mein|ke\s+(?:bahar|baahar|samne|saamne|paas|pass|andar))\s*"
                         r"(?:hi\s+|abhi\s+)?(?:khada|khadi|khade|khda|hoon|hu|hun|hai|hain|he|waiting|wait\s+kar\s+raha)\b",
                         re.I)
_NEGATION = re.compile(r"\b(?:not|nahi|nahin|nhi|nahee|yet|never|didn'?t|haven'?t|hasn'?t)\b", re.I)
_FUTURE = re.compile(r"\b(?:will|shall|going\s+to|gonna|about\s+to|soon|ll)\b|'ll\b|" + _PAH.replace(r"\w*", "") +
                     r"(?:unga|ungi|enge|ega|egi|ne)|\b(?:jaunga|jaungi|jayenge|jaega|jayega|aaunga|aaungi|aayenge)\b|"
                     r"\b\d{1,2}(?::\d{2})?\s*(?:baje|bje|am|pm)\b|\b(?:by|around|at)\s+\d", re.I)
_QUESTION = re.compile(r"\b(?:kya|kab|kahan|kaha|kaise|kitne|when|where|how|did|have\s+you|has\s+the)\b", re.I)
# Words that may stand before/after the arrival verb without naming another place.
_ARRIVAL_FILLER = {"i", "we", "have", "has", "had", "just", "already", "now", "finally", "truck", "gaadi", "gadi", "lorry",
                   "vehicle", "main", "mai", "mein", "me", "hum", "ham", "abhi", "ab", "bas", "ji", "haan", "haa", "han",
                   "sir", "bhai", "madam", "the", "at", "to", "here", "yahan", "yaha", "pe", "par", "pr", "ke", "bahar",
                   "baahar", "paas", "samne", "hoon", "hu", "hun", "hai", "hain", "he", "ho", "aur", "and", "ok", "okay",
                   "safely", "on", "time", "early", "jaldi", "chuka", "your", "our", "my", "is", "am", "are", "its",
                   "it's", "it", "se", "ka", "ki", "wala", "wale", "khada", "khadi", "please", "pls", "plz", "unloading",
                   "for", "dock", "number", "no", "hello", "hi", "namaste", "good", "morning"}


def _clause_bounds(t: str, start: int, end: int) -> tuple[int, int]:
    a = max((b.end() for b in _CLAUSE_BREAK.finditer(t, 0, start)), default=0)
    z = next((b.start() for b in _CLAUSE_BREAK.finditer(t, end)), len(t))
    return a, z


def arrived_now(text: str) -> bool:
    """True when the driver says the truck is at the facility now (G.1, I15). Deterministic; see the module notes."""
    t = text or ""
    hits = [(m, "verb") for m in _ARRIVED_HI.finditer(t)] + [(m, "verb") for m in _ARRIVED_EN.finditer(t)] + \
           [(m, "place") for m in _PRESENT_EN.finditer(t)] + [(m, "place") for m in _PRESENT_HI.finditer(t)]
    for m, kind in hits:
        a, z = _clause_bounds(t, m.start(), m.end())
        clause = t[a:z]
        if _NEGATION.search(clause) or _FUTURE.search(clause) or _QUESTION.search(clause):
            continue
        if kind == "place":
            return True
        has_loc = bool(re.search(r"\b" + _LOC + r"\b", clause, re.I))
        if m.group(0).lower().startswith(("aa", "a ")) or re.match(r"a\s*" + _PERFECTIVE_HI, m.group(0), re.I):
            if has_loc:                                    # "aa gaya" alone is too vague ("message aa gaya")
                return True
            continue
        if has_loc:
            return True
        words = [w for w in re.findall(r"[a-z']+", (t[a:m.start()] + " " + t[m.end():z]).lower())]
        if all(w in _ARRIVAL_FILLER or re.fullmatch(_PERFECTIVE_HI, w) for w in words):
            return True                                   # "Pahunch gaya hoon", "I have reached": nothing else named
    return False
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


def _evening_guess(hour: str) -> str | None:
    """"after 5" / "before 6" from a driver means the evening. A zero-padded hour ("05:26", "06:00") is written as
    24-hour time and keeps its morning meaning (scenario run, 23 Nov 2026: "anything after 05:26" was read as 17:26)."""
    return "pm" if int(hour) < 7 and not hour.startswith("0") else None


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
    elif arrived_now(t) or re.search(r"reached early|\bearly\b|jaldi (?:" + _PAH + r"|aa)", low):
        # G.1 (I15): "at the gate" / "gate ke paas" alone were keyword traps ("I will be at the gate by 5" is an arrival
        # TIME); the truck being here now is decided by arrived_now(), which rejects future, negated and asked arrivals.
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

    # G.1 (I14): a completion deadline owns its time; no arrival reading may take the same words
    dl = _DEADLINE.search(t) or _DEADLINE_HI.search(t)

    def _free(m) -> bool:
        return m is not None and (dl is None or m.end() <= dl.start() or m.start() >= dl.end())

    def _first_weak(rx):
        return next((m for m in rx.finditer(t) if _free(m) and _about_arriving(m)), None)

    if out["delay_kind"] == "UNKNOWN" and _free(m := _ETA.search(t)):      # "reach 11:20", "ETA 6:30"
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _hhmm(m.group(1), m.group(2), m.group(3))
    if out["delay_kind"] == "UNKNOWN" and (m := (m2 if _free(m2 := _ETA_HI_ARR.search(t)) else None)
                                          or _first_weak(_ETA_HI)):        # "6 baje pahunchunga"
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _daypart_hhmm(*m.group(1, 2, 3, 4))
    if out["delay_kind"] == "UNKNOWN" and (m := _first_weak(_ETA_WEAK)):    # "late, around 3 pm"
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _hhmm(m.group(1), m.group(2), m.group(3))
    if out["delay_kind"] == "UNKNOWN" and dl is None and asked in ETA_QUESTIONS and (m := _BARE.fullmatch(t)) and \
            int(m.group(2)) <= 23:
        out["delay_kind"], out["declared_eta_local"] = "ARRIVAL_TIME", _daypart_hhmm(*m.group(1, 2, 3, 4))
    if out["delay_kind"] == "UNKNOWN" and (m := (_DUR.search(t) or _DUR2.search(t) or
                                                 (_DUR_ANY.search(t) if _LATE_WORD.search(low) else None))):
        mins = _minutes(m.group(1), m.group(2))
        out["declared_delay_min"] = mins
        out["delay_kind"] = "REPAIR_DURATION" if out["exception_type"] == "BREAKDOWN" and "repair" in low else "DELAY_DURATION"
    if (m := _AFTER.search(t)) and not any(_NON_ARRIVAL.search(s) for s in _clause(m)):   # not "ke baad 12 baje call"
        out["earliest_start_local"] = _hhmm(m.group(1), m.group(2), m.group(3) or _evening_guess(m.group(1)))
    if dl:                                                           # G.1 (I14): "finish unloading by 5 pm" = 17:00
        out["latest_finish_local"] = _hhmm(dl.group(1), dl.group(2), dl.group(3) or _evening_guess(dl.group(1)))
    elif m := _BEFORE.search(t):
        out["latest_finish_local"] = _hhmm(m.group(1), m.group(2), m.group(3) or _evening_guess(m.group(1)))
    elif m := _BEFORE_HI.search(t):                                  # "raat 10 baje se pehle gate se nikalna hai"
        out["latest_finish_local"] = _daypart_hhmm(m.group(1), m.group(2), m.group(3),
                                                   m.group(4) or (None if m.group(1) else _evening_guess(m.group(2))))
    if out.get("latest_finish_local"):
        # only an explicit gate-out counts; "leave the warehouse by 1:30" is ambiguous (Phase 1 MSG004) → ask.
        # G.1: the END of (un)loading stated with a completion verb is UNLOAD_END; "be done by 4" alone stays UNKNOWN
        # (asked, decision D1), and extraction.kind_stated re-checks the words either way.
        if re.search(r"gate.?out|exit the gate|gate se nikal", low):
            out["latest_finish_kind"] = "GATE_OUT"
        elif dl and _DOCK_WORD.search(t) and kind_stated("UNLOAD_END", t):
            out["latest_finish_kind"] = "UNLOAD_END"
        else:
            out["latest_finish_kind"] = "UNKNOWN"

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
    elif dl or _DEADLINE_CLOSE.search(t):                            # G.1 (I14): a deadline asks for a slot that meets it
        out["intent"] = "ASK_OPTIONS"
    elif _ARRIVAL_VERB.search(t):                                    # "Kal subah pochunga": about arriving, no time
        out["intent"] = "REPORT_DELAY"
    elif out.get("latest_finish_local") or out.get("earliest_start_local"):   # "10 baje se pehle nikalna hai": a slot
        out["intent"] = "ASK_OPTIONS"
    if out["intent"] == "REPORT_DELAY" and out["exception_type"] == "UNKNOWN":
        out["exception_type"] = "DELAY"
    return Extraction(**out)
