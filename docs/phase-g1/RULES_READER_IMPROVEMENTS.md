# Rules reader improvements (G4): I14 deadlines, I15 arrivals

Status: implemented, tested locally, deterministic (no model, no cost). **Not deployed.**
Code: `setuhaul/agent/rules_extractor.py`, `setuhaul/agent/extraction.py`. Cases:
`evals/langsmith/rules_reader_cases.jsonl` (run by `tests/test_phase_g1.py` and the evaluation).

## 1. The two baseline failures

| Row | Driver says | Phase G output | Should be |
|---|---|---|---|
| I14 | "Need to finish unloading by 5 pm" | intent `OTHER`, no time (the weak "by 5 pm" reading was correctly refused as an arrival, and nothing else read it) | a **completion deadline** 17:00, kind `UNLOAD_END`; not an ETA, not a slot start |
| I15 | "pahunch gaya hoon gate pe" | `REPORT_DELAY` ("pahunch" alone was taken as "about arriving") | the truck **is at the gate now** → arrival intent (`EARLY_ARRIVAL`, the schema's "truck is here" intent) |

## 2. Completion deadlines (I14)

A deadline needs a completion verb that governs the time:

* `finish | complete | wrap up | be done/finished/completed/over/free | get (it) done | khatam | poora` + optional
  `(un)loading` + `by | before | till | until | no later than` + a time; or
* `(un)loading` + `should/must/has to/needs to` + `be completed/finished/done/over` + `by/before/…` + a time; or
* Hinglish time-first: `5 baje tak unloading khatam …`, `… se pehle maal utar …`.

Then:
* `latest_finish_local` = the time. A bare `5` before 7 o'clock reads as evening (17:00, the existing convention);
  `17:00` and `05:00` keep their 24-hour meaning.
* `latest_finish_kind` = `UNLOAD_END` only when the words name the (un)loading AND say finish, not start
  (`extraction.kind_stated` re-checks this, and "loading" now counts as dock work). "We need to be done by 4 pm" stays
  `UNKNOWN`, so the agent asks gate-out or unloading: decision **D1** unchanged.
* The deadline owns its words: no arrival-time reading (strong, weak, Hindi time-first, or bare answer) may take a time
  inside the deadline phrase. "I will reach by 3 pm and need to finish unloading by 6 pm" → ETA 15:00 **and** deadline
  18:00.
* Intent: `ASK_OPTIONS` (a slot that meets it), unless the message is a delay report (`REPORT_DELAY`, deadline kept).
* "Finish before facility closing": no time is invented. The facility's closing time is already a hard constraint of
  every offered option (the feasibility engine never offers a slot ending after close), so this is `ASK_OPTIONS`
  with no extra constraint.

**Time zone, explicit.** `extraction.FACILITY_TZ_NAME = "Asia/Kolkata"` (both pilot facilities; `facilities.timezone`).
`FACILITY_TZ` = +05:30 (India has no DST, and a test compares it with the IANA zone). `local_to_ts(…, tz=FACILITY_TZ)`
now **refuses a naive "now"**, so a server's local clock (UTC on Lambda, Sydney or anything else on a laptop) can never
leak in. Test: 17:00 resolves to 17:00+05:30 whatever the server's zone.

## 3. Arrival now (I15)

`arrived_now(text)` returns True only for a present-state arrival in the same clause:

* completed arrival verb: `pahunch/pohonch… gaya/gayi/gaye/chuka`, `aa gaya`, `reached`, `arrived`, `got here`, `made it`, `pulled in`;
* present location: `I am / I'm / we are / truck is … at/near/outside (the) gate/facility/warehouse/dock/…`,
  `gate pe/par khada/hoon/hai`, `facility/gate ke bahar/samne/paas hai`;

and rejects, clause by clause:
* negation (`not, nahi, nahin, yet, never, haven't …`): "abhi nahi pahuncha";
* future or planned (`will, going to, soon`, `pahunchunga/enge`, `jaunga`, a clock time, `by/at <digit>`): "Gate pe 5 baje pahunchunga";
* a question (`kya, kab, kahan, when, where, did, have you …`): "Kya gate pe aa gaya?";
* another named place: "Jaipur pahunch gaya hoon", "reached Chomu". The verb must stand alone with fillers, or name a
  facility word;
* a vague "aa gaya" without a place: "message aa gaya".

Precedence (unchanged except where marked): safety → cancel → book/confirm → **arrival (new detector replaces the
`at the gate` keyword trap)** → status → delay → options → **deadline (new)** → bare arrival verb (delay).
The old keyword list (`at the gate`, `gate ke paas`) used to make "I will be at the gate by 5" an arrival. It is now an
arrival TIME (17:00, `REPORT_DELAY`), via a new arrival verb `be at the gate/facility/…`.

## 4. Supported variants (all pass)

Deadlines: "Need to finish by 5 pm" → 17:00 · "Unloading should be completed before 5" → 17:00 `UNLOAD_END` ·
"Must finish loading by 17:00" → 17:00 `UNLOAD_END` · "Can you complete unloading by 5:30 PM?" → 17:30 `UNLOAD_END` ·
"We need to be done by 4 pm" → 16:00 (kind asked) · "Finish before facility closing" → options, no invented time ·
"5 baje tak unloading khatam karni hai" → 17:00 `UNLOAD_END`.

Arrivals: "I have reached the gate" · "I am at the gate" · "Truck gate pe aa gaya" · "Main gate par pahunch gaya" ·
"Pahunch gaya hoon" · "Gate pe khada hoon" · "Truck facility ke bahar hai" · "Haan ji, gate pe hoon" · "pahunch gaya
hoon gate pe".

Negatives (must not be arrival): the 9 `RN*` cases above. Negatives for deadlines (`DN*`): "Reaching by 5 pm" (ETA),
"slot after 5 pm" (earliest start), "office closes at 5, I will be late" (not a deadline).

## 5. Measured

Final validation, 23 Nov 2026 (local, deterministic, $0):

| Metric | Result |
|---|---|
| intent accuracy (37 cases) | 37 / 37 (100 %) |
| deadline accuracy (12 cases) | 12 / 12 (100 %) |
| arrival accuracy (20 cases, positives and negatives) | 20 / 20 (100 %) |
| false-positive delay rate (17 cases that must not be delays) | 0 / 17 (0 %) |
| Phase G 67 scenarios | 67 / 67 (baseline 65 / 67): I14 and I15 pass, none newly failing |

Source: `evals/results/g1_langsmith_eval_20261123_1426.json`; see PHASE_G1_FINAL_VALIDATION_REPORT.md §3.

## 6. Limits

* Word lists cover the phrasings in the cases and close variants; truly new phrasings fall to `OTHER`/the agent's
  question, never to a wrong booking. The model extraction (when the key is set) handles open language; this reader is
  the deterministic fallback.
* Hindi in Devanagari script: deadline/arrival detection is Latin-script (Hinglish) only, as before for most
  rules. Devanagari arrival ("पहुँच गया") is a follow-up, needing native review.
