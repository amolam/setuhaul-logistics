# Held-out set h1 (B-EV-1b)

**Frozen 18 Nov 2026 at `h1+sha256:2e72cb40af0b106d`, before any model was evaluated on it.** It is never used to tune prompts. A new prompt is measured on it; if the prompt is then changed because of h1, h1 stops being held out and a new set (h2) is needed.

## Why it exists

Prompts p2 and p3 were written with G3 in view, so G3 scores are development-set scores. h1 asks the question the reviewer raised: does extraction hold up on messages nobody tuned for?

## How it was built (`evals/make_heldout.py`)

167 one-turn scenarios from the 76 labelled turns of G3 v4:

| Variant | Count | Written by | Labels |
|---|---|---|---|
| Paraphrase: the same facts, said differently | 74 | Sonnet 5 via OpenRouter, which **never saw the extraction prompt, the schema or the labels**: only the message and a plain list of facts to keep | Copied from the base turn by code |
| Extra irrelevant detail (weather, chai, road chatter) | 76 | Sonnet 5, same rules | Copied from the base turn by code |
| Time shift: every clock time +40 min, and the clock and labels with it | 17 | Code. Only turns whose times are all `H:MM` digits; "after 12", "9 AM" and number words are skipped | Shifted by code; checked self-consistent |

**Checks.**
- **Automatic** (none failed): every number in the base survives; no new clock time; order references unchanged; the script is unchanged (Devanagari vs Latin).
- **Review** (Claude, 18 Nov): every variant was read against its base. Dropped 2:
  - BB10#1 paraphrase: "Gate out done" says the truck has already left, while the base names a gate-out deadline.
  - THR002#1 paraphrase: "Gate exit done", the same drift.
- **Kept on purpose:**
  - P02: "broke down in the middle of the highway" became "right on the highway, traffic zooming past". The danger is still there, but the code's danger words no longer match, so it tests the model.
  - ADV07 and P01: the paraphrases moved from English to Hinglish. Neither scenario labels the language.

Generator prompt, raw outputs, usage and drops: `generation.json`. Generation cost about $0.16.

**Limits.**
- **Extraction only.** Paraphrased multi-turn conversations would need consistent rewrites across turns, so h1 is not replayed end to end.
- **Not independent of G3.** The variants are rewrites of G3's base messages, not new situations. New situations need new scenarios, and ideally real (sanitised) driver messages.
- **One reviewer.** The review was done by the same assistant that wrote G3 and the prompt. A human reviewer should spot-check `generation.json`.

## Results (18 Nov 2026, Haiku 4.5 via OpenRouter, prompt p3; `evals/results/golden_h1_*`, `golden_v4_*`)

| | Base: G3 v4, 76 turns | Held-out: h1, 167 turns |
|---|---|---|
| **Haiku 4.5, p3**: fully right turns | 73 / 76 (96 %) | **158 / 167 (95 %)** |
| Haiku: schema validity / ETA precision / ETA recall / intent | 1.00 / 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / **0.98** / **0.98** (all gates pass) |
| Rules only: fully right turns | 60 / 76 | 99 / 167 (59 %) |
| Rules: ETA recall / intent | 0.84 ✗ / 0.78 ✗ | 0.69 ✗ / 0.61 ✗ |

Haiku by variant: time shift 17/17, extra detail 72/76, paraphrase 69/74. The rules baseline gets 34/74 paraphrases.

**Reading.** Accuracy on unseen phrasings is within about 1.5 points of the base set, and every gate still passes. **p3 is not meaningfully overfitted to G3.** The rules baseline falls much further on paraphrases, which confirms keeping Haiku for extraction (B-AI-3, extraction half).

**Misses (9), recorded and not tuned on:**
- **THR011, paraphrase and detail.** When the ETA leads the sentence ("If I arrive by around 12:45, what slots can I get after 1 PM?"), Haiku leaves `declared_eta_local` empty. This is the one real recall miss.
- **BB02, paraphrase.** "What are my choices if it's after 2 PM?" loses the earliest start.
- **AT06, paraphrase and detail.** The known debatable label: "a 2 PM slot" is read as an arrival time.
- **BB10#1, detail.** A bare "Gate-out." plus chatter leaves the kind empty. The code reads it from the words since v3, so the agent still behaves correctly.
- **Label-level disagreements.** THR007 paraphrase: "already at the gate, can I get a dock now" read as ASK_OPTIONS rather than EARLY_ARRIVAL. L08 detail: a clutch problem read as DELAY rather than BREAKDOWN. H06 paraphrase: Devanagari tagged `hi-Latn`.

A prompt p4 that addresses these must be measured on a NEW held-out set (h2), because changing the prompt in response to h1 uses h1 up.

**Cost.** 167 extractions: 326,797 input and 55,289 output tokens, about $0.60 ($0.0036 per extraction, in line with G3).
