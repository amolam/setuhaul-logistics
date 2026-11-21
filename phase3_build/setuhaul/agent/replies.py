"""Reply templates (English, Hindi in Devanagari, Hinglish in Latin letters), the reply-language rule, and the
grounding post-check (rule R5).

Templates are used where no model is needed (options, read-back, status, escalation, safety; about a third of turns)
and as the fallback whenever the model fails or a draft fails grounding twice.

Grounding: every time, dock code, date and reference in a draft must appear in this turn's facts, with the same
dock/time pairing, and a draft may say "confirmed" only when the facts say CONFIRMED.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def _hi(lang: str) -> bool:
    return lang == "hi"


T = {
    "options": ("Available slots:\n{lines}\nReply with the number you want.",
                "उपलब्ध स्लॉट:\n{lines}\nजो चाहिए उसका नंबर भेजें।"),
    "option_line": ("{n}. Dock {dock_code}, {start}{approval}", "{n}. डॉक {dock_code}, {start}{approval}"),
    "pending": ("Request sent for dock {dock_code} at {start}. It is NOT confirmed yet; the warehouse will reply and I will tell you.",
                "डॉक {dock_code}, {start} के लिए अनुरोध भेज दिया है। अभी पक्का नहीं है; वेयरहाउस के जवाब पर मैं बताऊँगा।"),
    "booked": ("Confirmed: dock {dock_code} at {start}.", "पक्का हो गया: डॉक {dock_code}, {start}।"),
    "status_none": ("You have no active booking for {shipment_id}.", "{shipment_id} की कोई सक्रिय बुकिंग नहीं है।"),
    "status": ("{shipment_id}: dock {dock_code} at {start}, status {status}.", "{shipment_id}: डॉक {dock_code}, {start}, स्थिति {status}।"),
    "coordinator": ("A coordinator has your details and will reply within {sla} minutes.",
                    "एक कोऑर्डिनेटर को आपकी जानकारी मिल गई है, {sla} मिनट में जवाब आएगा।"),
    "safety": ("Please stay safe. A coordinator will call you now.", "कृपया सुरक्षित रहें। कोऑर्डिनेटर अभी आपको कॉल करेंगे।"),
    "lost": ("That slot was just taken.", "वह स्लॉट अभी किसी और ने ले लिया।"),
    "ask_yes": ("Please reply YES to confirm, or choose another option.", "पक्का करने के लिए YES भेजें, या दूसरा विकल्प चुनें।"),
    # B2-18 (found again by the G3 replay, AT10): never promise a coordinator unless an escalation row exists
    "no_slot": ("I could not find a slot that works right now. Please call your dispatcher.",
                "अभी कोई सही स्लॉट नहीं मिला। कृपया अपने डिस्पैचर को कॉल करें।"),
    "cancelled": ("{ref} and its appointment were cancelled. Please contact your dispatcher before travelling.",
                  "{ref} और उसकी अपॉइंटमेंट रद्द हो गई है। आने से पहले अपने डिस्पैचर से बात करें।"),
    "no_live": ("I cannot find an active shipment for you today. Please contact your dispatcher before travelling.",
                "आज आपका कोई सक्रिय शिपमेंट नहीं मिला। आने से पहले अपने डिस्पैचर से बात करें।"),
    "not_yours": ("I can only help with your own shipments: {refs}.", "मैं सिर्फ़ आपके अपने शिपमेंट में मदद कर सकता हूँ: {refs}।"),
    "ack": ("Thanks, noted.", "धन्यवाद, नोट कर लिया।"),
    # OD-2: a breakdown is an operational escalation; the safety question is asked only when the driver did not say
    "breakdown": ("A coordinator has the details of your breakdown and will reply within {sla} minutes.",
                  "कोऑर्डिनेटर को आपके ट्रक खराब होने की जानकारी मिल गई है, {sla} मिनट में जवाब आएगा।"),
    "ask_safe": ("Are you and the truck in a safe place? Reply YES or NO.",
                 "क्या आप और ट्रक सुरक्षित जगह पर हैं? YES या NO भेजें।"),
    "safe_ack": ("Thanks. The coordinator will help with the repair. Send me your arrival time once the truck is moving.",
                 "धन्यवाद। मरम्मत में कोऑर्डिनेटर मदद करेंगे। ट्रक चलने पर पहुँचने का समय भेजें।"),
    "keep": ("Your slot stays as it is: dock {dock_code} at {start}. With your new arrival time of {eta} it still works. "
             "I will tell you if anything changes.",
             "आपका स्लॉट वही रहेगा: डॉक {dock_code}, {start}। आपके नए समय {eta} के साथ यह ठीक है। कुछ बदला तो मैं बताऊँगा।"),
    "confirm_eta": ("Do you mean you will now reach the gate around {eta}? Reply YES, or send your arrival time.",
                    "क्या आप अब लगभग {eta} तक गेट पर पहुँचेंगे? हाँ हो तो YES भेजें, नहीं तो पहुँचने का समय भेजें।"),
    "unclear": ("Sorry, I did not understand. When will you reach the warehouse?",
                "माफ़ कीजिए, समझ नहीं आया। आप वेयरहाउस कब पहुँचेंगे?"),
}
# Hinglish (Hindi in Latin letters), B-AI-3 finding: Hinglish drivers were answered in English. Dock codes, times and
# order references keep exactly the English form, so grounding and the driver's reading of them are unchanged.
HL = {
    "options": "Ye slots khaali hain:\n{lines}\nJo slot chahiye, uska number bhej dijiye.",
    "option_line": "{n}. Dock {dock_code}, {start}{approval}",
    "pending": "Dock {dock_code}, {start} ke liye request bhej di hai. Abhi ye NOT confirmed hai; warehouse ka jawab aate hi "
               "main bataunga.",
    "booked": "Pakka ho gaya: dock {dock_code}, {start}.",
    "status_none": "{shipment_id} ki koi active booking nahi hai.",
    "status": "{shipment_id}: dock {dock_code}, {start}, status {status}.",
    "coordinator": "Aapki details coordinator ko mil gayi hain, {sla} minute mein jawab aayega.",
    "safety": "Aap safe rahiye, coordinator abhi aapko call karenge.",
    "lost": "Wo slot abhi kisi aur ne le liya.",
    "ask_yes": "Pakka karne ke liye YES bhej dijiye, ya koi aur option chun lijiye.",
    "no_slot": "Abhi koi sahi slot nahi mila. Apne dispatcher ko call kar lijiye.",
    "cancelled": "{ref} aur uski appointment cancel ho gayi hai. Aane se pehle apne dispatcher se baat kar lijiye.",
    "no_live": "Aaj aapka koi active shipment nahi mila. Aane se pehle apne dispatcher se baat kar lijiye.",
    "not_yours": "Main sirf aapke apne shipments mein help kar sakta hoon: {refs}.",
    "ack": "Theek hai, note kar liya.",
    "breakdown": "Truck kharab hone ki details coordinator ko mil gayi hain, {sla} minute mein jawab aayega.",
    "ask_safe": "Kya aap aur truck safe jagah par hain? YES ya NO bhej dijiye.",
    "safe_ack": "Theek hai. Repair mein coordinator madad karenge. Truck chalne par pahunchne ka time bhej dijiye.",
    # S4-R2-08 native review: the sentence about the new time is dropped in Hinglish (the slot line says enough)
    "keep": "Aapka slot wahi rahega: dock {dock_code}, {start}. Kuch badla to main bataunga.",
    "confirm_eta": "Kya aap ab lagbhag {eta} tak gate par pahunchenge? Haan to YES bhej dijiye, nahi to pahunchne ka time bhej dijiye.",
    "unclear": "Maaf kijiye, samajh nahi aaya. Aap warehouse kab tak pahunchenge?",
}
HL_ASK = {
    "declared_eta_local": "Aap warehouse gate par kitne baje pahunchenge?",
    "delay_kind": "Ye repair ka time hai ya total delay?",
    # S4-R2-08: the reviewer did not understand the first wording; Claude's redraft, approved by the reviewer 21 Nov
    "latest_finish_kind": "Ye time kis cheez ka hai? Us time tak gate se bahar nikalna hai, ya us time tak maal utarna "
                          "shuru ho jaana chahiye?",
    # S4-R2-08: the orders are numbered and the driver answers with the number
    "shipment_id": "Aaj aapke ek se zyada shipment hain. Ye kaunse order ke baare mein hai?\n{refs}\nJo order hai uska "
                   "number bhejiye.",
}
HL_APPROVAL = " (warehouse approval chahiye)"

# ---------------------------------------------------------------- the language of a reply (B-AI-3 finding)
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
HINGLISH_WORDS = re.compile(
    r"\b(aap|aapka|aapki|bhej|bhejo|bhejiye|bhejein|chahiye|wala|wali|kaunsa|kaun|hai|hain|hoon|hu|ka|ki|ke|ko|mein|"
    r"se|jo|nahi|haan|karo|kare|karein|kar|bahut|ghanta|ghante|jaunga|jayega|pahunch\w*|raha|rahe|rahi|gaya|gya|"
    r"abhi|kya|theek|ji|bhai|yaar|lagega|lagbhag|tak|baje|dena|wahi)\b", re.I)


def detect_language(text: str) -> str:
    """Script and register from the driver's own words: Devanagari -> hi; two or more Hinglish words -> hi-Latn."""
    if DEVANAGARI.search(text or ""):
        return "hi"
    return "hi-Latn" if len(HINGLISH_WORDS.findall(text or "")) >= 2 else "en"


def reply_language(text: str, model_lang: str | None, earlier: list[str]) -> str:
    """Answer in the conversation's language, not the last message's.
    * A message written in Devanagari is answered in Hindi, whatever the model said.
    * A short reply ("yes", "2", "haan", "हाँ" is caught above) keeps the language of the driver's last longer message.
    * Otherwise the model's reading of this message stands."""
    if DEVANAGARI.search(text or ""):
        return "hi"
    if len((text or "").split()) <= 2:
        for prev in reversed(earlier):
            if len(prev.split()) > 2:
                return detect_language(prev)
    return model_lang if model_lang in ("en", "hi", "hi-Latn") else "en"


ASK = {
    "declared_eta_local": ("What time do you expect to reach the warehouse gate?", "आप वेयरहाउस गेट पर कितने बजे पहुँचेंगे?"),
    "delay_kind": ("Is that the repair time, or your total delay?", "क्या यह मरम्मत का समय है या कुल देरी?"),
    "latest_finish_kind": ("Is that the time you must leave the gate, or when unloading must start?",
                           "क्या उस समय तक गेट से निकलना है, या तब तक अनलोडिंग शुरू होनी है?"),
    "shipment_id": ("You have more than one shipment today. Which order is this about?\n{refs}\nReply with the number.",
                    "आज आपके एक से ज़्यादा शिपमेंट हैं। यह किस ऑर्डर के बारे में है?\n{refs}\nऑर्डर {choices} नंबर भेजें।"),
}


def t(key: str, lang: str, **kw) -> str:
    if lang == "hi-Latn":
        return HL[key].format(**kw)
    return T[key][1 if _hi(lang) else 0].format(**kw)


def numbered(refs: str) -> tuple[str, str]:
    """S4-R2-08: "ORD-1, ORD-2" -> ("1. ORD-1\n2. ORD-2", "1 या 2"). The numbers are what the driver sends back."""
    items = [r.strip() for r in (refs or "").split(",") if r.strip()]
    nums = [str(i) for i in range(1, len(items) + 1)]
    choices = nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " या " + nums[-1] if nums else ""
    return "\n".join(f"{i}. {r}" for i, r in enumerate(items, 1)), choices


def ask(field_name: str, lang: str, **kw) -> str:
    if field_name == "shipment_id":
        kw["refs"], kw["choices"] = numbered(kw.get("refs", ""))
    if lang == "hi-Latn":
        return HL_ASK.get(field_name, HL["unclear"]).format(**kw)
    pair = ASK.get(field_name, T["unclear"])
    return pair[1 if _hi(lang) else 0].format(**kw)


def options_text(options: list[dict], lang: str) -> str:
    hl = lang == "hi-Latn"
    line = HL["option_line"] if hl else T["option_line"][1 if _hi(lang) else 0]
    mark = HL_APPROVAL if hl else (" (वेयरहाउस मंज़ूरी चाहिए)" if _hi(lang) else " (needs warehouse approval)")
    lines = "\n".join(line.format(n=i, dock_code=o["dock_code"], start=o["start"],
                                  approval=mark if o.get("needs_approval") else "")
                      for i, o in enumerate(options, 1))
    return t("options", lang, lines=lines)


# ---------------------------------------------------------------- grounding
_TIME = re.compile(r"\b(\d{1,2}:\d{2})\s*(AM|PM)?\b", re.I)
_DOCK = re.compile(r"\bD\d{1,2}\b")
_REF = re.compile(r"\b(APT-[0-9a-f]{6,}|ORD-\d{6}-\d{3}|SHP[\w-]+|ESC-[\w-]+)\b", re.I)
_DATE = re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", re.I)
_CONFIRMED = re.compile(r"\bconfirmed\b|पक्का हो गया|कन्फर्म हो", re.I)   # a claim, not "reply YES to confirm"
# H-5r review: deterministic limits on what a phrased reply may claim, whatever its prompt or background note says.
# A status claim (approved, cleared, booked...) needs a CONFIRMED booking in FACTS; an operational promise (guaranteed,
# no waiting, priority...) is never allowed, because no tool ever produces one. "needs warehouse approval" is not a claim.
_STATUS_CLAIM = re.compile(
    r"\b(approved|cleared|booked|reserved|secured|locked\s+in|finali[sz]ed|sanctioned|accepted|okayed|ok'?d)\b|"
    r"\b(pakk[aie]|confirm\s+ho|book\s+ho|clear\s+ho|approve\s+ho|mil\s+gay[aie]|ho\s+gay[aie]|fix\s+ho)\b|"
    r"पक्का|पक्की|मंज़ूर\s*हो|मंजूर\s*हो|बुक\s*हो|तय\s*हो|मिल\s*गया|हो\s*गया", re.I)
_PROMISE = re.compile(
    r"\b(guarantee\w*|assured?|promise\w*|definitely|for\s+sure|100\s*%|no\s+wait\w*|without\s+wait\w*|priority|"
    r"(?:will|is)\s+be\s+ready|ready\s+for\s+you|don'?t\s+worry|nothing\s+to\s+worry|no\s+problem|"
    r"pakka\s+milega|wait\s+nahi|tension\s+mat|chinta\s+mat|turant)\b|गारंटी|पक्का\s*मिलेगा|चिंता\s*मत|इंतज़ार\s*नहीं", re.I)


@dataclass
class Facts:
    """What this turn's tools produced: the only things a reply may state."""
    text: str                                                  # everything the tools returned, rendered
    pairs: set[tuple[str, str]] = field(default_factory=set)   # (dock_code, "1:00 PM") pairs offered or booked
    confirmed: bool = False


def _norm_time(hhmm: str, ap: str | None) -> str:
    h, m = hhmm.split(":")
    return f"{int(h)}:{m} {ap.upper()}" if ap else f"{int(h)}:{m}"


def grounding_violations(draft: str, facts: Facts) -> list[str]:
    v = []
    ft = facts.text
    for hhmm, ap in _TIME.findall(draft):
        if _norm_time(hhmm, ap) not in ft and hhmm not in ft:
            v.append(f"time {hhmm}{' ' + ap if ap else ''} is not in the facts")
    for d in _DOCK.findall(draft):
        if d not in ft:
            v.append(f"dock {d} is not in the facts")
    for r in _REF.findall(draft):
        if r.upper() not in ft.upper():
            v.append(f"reference {r} is not in the facts")
    for day, mon in _DATE.findall(draft):
        if f"{int(day)} {mon.capitalize()}" not in ft:
            v.append(f"date {day} {mon} is not in the facts")
    # each dock is paired with the first time that follows it (before the next dock): that pair must be real
    marks = sorted([(m.start(), "D", m.group(0)) for m in _DOCK.finditer(draft)] +
                   [(m.start(), "T", _norm_time(m.group(1), m.group(2))) for m in _TIME.finditer(draft) if m.group(2)])
    for i, (_, kind, val) in enumerate(marks):
        if kind != "D":
            continue
        nxt = next((x for x in marks[i + 1:] if x[1] in ("D", "T")), None)
        if nxt and nxt[1] == "T" and facts.pairs and (val, nxt[2]) not in facts.pairs:
            v.append(f"{val} at {nxt[2]} was not offered")
    if _CONFIRMED.search(draft) and not facts.confirmed and not re.search(r"not confirmed|NOT confirmed|पक्का नहीं", draft):
        v.append("says confirmed, but the booking is not CONFIRMED")
    if not facts.confirmed and (m := _STATUS_CLAIM.search(draft)):
        v.append(f"claims a booking status (\"{m.group(0)}\") that FACTS do not show")
    if m := _PROMISE.search(draft):
        v.append(f"makes an operational promise (\"{m.group(0)}\") that no tool produced")
    return v


def phrasing_inputs(options: list[dict], lang: str) -> tuple[str, Facts, str]:
    """What an option reply is built from: the template (what the system sends), the grounding facts, and the prompt
    block a phrasing model would get. Since OD-3 production sends the template and calls no phrasing model; the facts
    and block are kept only so the B-AI-3 ablation (evals/phrase_ablation.py) can be re-run on the same inputs."""
    template = options_text(options, lang)
    facts = Facts(text=template + "\n" + "\n".join(f"{o['dock_code']} {o['start']} {o['end']}" for o in options),
                  pairs={(o["dock_code"], o["start"].split(" on ")[0]) for o in options})
    extras = "\n".join(f"- {x}" for x in _extras(lang))
    block = (f"Language: {lang}\nFACTS:\n{facts.text}\nTask: send the option list exactly as written at the top of "
             f"FACTS (its first and last lines and every numbered line, unchanged). You may add at most one of these "
             f"lines, copied exactly, before or after it, and nothing else:\n{extras}")
    return template, facts, block


# ---------------------------------------------------------------- H-5r2: a constrained reply structure
# Status language belongs to the system, not the model. An option reply may be only the system's own option list
# (its fixed status lines and numbered lines, unchanged: the numbers also select the option) plus at most one fixed
# courtesy line. So no wording, however it is phrased ("green light", "all set", "good to go"), can claim approval,
# confirmation or an operational commitment. The word lists in grounding_violations stay as a second layer.
SAFE_EXTRAS = {
    # Round 4: "I have the coordinator's update" was removed. It is a claim about a note, and it passed every check
    # even when there was no note. These two lines claim nothing.
    "en": ("Sorry for the trouble on the road.", "Please drive safely."),
    "hi-Latn": ("Raaste ki pareshani ke liye maaf kijiye.", "Dhyan se chalaiye."),
    "hi": ("रास्ते की परेशानी के लिए माफ़ कीजिए।", "ध्यान से चलाइए।"),
}


def _extras(lang: str) -> tuple[str, ...]:
    return SAFE_EXTRAS["hi-Latn" if lang == "hi-Latn" else "hi" if _hi(lang) else "en"]


def _line(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^(\d)\)\s*", r"\1. ", s)                       # "1) Dock D2" is "1. Dock D2"


def structure_violations(draft: str, template: str, lang: str) -> list[str]:
    want = [_line(x) for x in template.splitlines() if x.strip()]
    extras = {_line(x) for x in _extras(lang)}
    got = [_line(x) for x in draft.splitlines() if x.strip()]
    added = [g for g in got if g in extras]
    core = [g for g in got if g not in extras]
    v = []
    if core != want:
        extra = [g for g in core if g not in want]
        v.append("the reply must be the system's option list unchanged" +
                 (f"; not allowed: \"{extra[0][:80]}\"" if extra else "; a line is missing or out of order"))
    if len(added) > 1:
        v.append("at most one of the fixed courtesy lines")
    if added and got and got[0] not in extras and got[-1] not in extras:
        v.append("a courtesy line goes before or after the list, not inside it")
    return v
