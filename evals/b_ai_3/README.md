# B-AI-3: templates vs Sonnet for driver replies (free stages)

Run in the order the cost review set: deterministic checks first, then human review, with the Sonnet judge deferred. **No paid calls were made.**

## Scope: what a phrasing model actually touches

> **Status: superseded by OD-3 (21 Nov 2026).** Stages 1–4 (free code checks) ran; stage 5 (human scoring of `review_sheet.md`) and the Sonnet arm were **not** run, and are no longer needed. The templates were instead reviewed by a native speaker (S4-R2-08).
>
> **Outcome (21 Nov 2026): owner decision OD-3.** Production option replies call no phrasing model; they are the deterministic English, Hindi and Hinglish templates. The rest of this page is the historical experiment that led there. The Sonnet arm can still be re-run here (`sonnet_arm` keeps a copy of the old loop); it is evaluation only and not reachable from production.

When this experiment ran, a phrasing model was used for **one** reply type: the list of slot options (the former `Orchestrator._phrase_options`). Status, read-back, confirmation, escalation, safety and refusal replies were always templates. So the question was narrow: should option lists be written by templates or by Sonnet?

## Situations (`situations.json`, frozen at `sha256:4ec8825e833f6aa7`)

The 20 option replies the end-to-end replay actually produced (`evals/results/replay_v4_scripted_*.json`), picked for variety. The ids are listed in `evals/phrase_ablation.py`:
- **4 non-English conversations:** H01 and H03/H05 (Hinglish), H02 (Hindi in Devanagari).
- **16 English ones:** a delay report, an arrival range, "after 7", a gate-out answer, an urgent load, a priority spoof, a broken dock, in the yard, a booking refused at load, a repaired breakdown, a grace boundary, and a plain "yes".

Both arms get exactly the orchestrator's inputs (`replies.phrasing_inputs`). The Sonnet arm runs the loop the orchestrator used before OD-3 (draft, grounding check, redraft, fall back to the template), so it measured what a driver would then have received.

**Gaps:** every replayed option reply offers 3 options and none needs warehouse approval. One-option and approval cases are covered by `tests/test_phrase_checks.py`.

## Stages 1–4: deterministic checks (`evals/phrase_ablation.py::checks`)

| Stage | Checks |
|---|---|
| 1 Grounding | Every dock, time, date and reference is in the facts; dock/time pairs are real; no "confirmed" |
| 2 Completeness | Every offered option is present, the list is numbered, and the reply asks for a number |
| 3 Language | The script matches the **conversation's** language (Devanagari for Hindi, Latin otherwise); the register (Hinglish vs English) is reported separately |
| 4 Policy | No coordinator promise, no internal ids, ≤ 450 characters, ≤ 3 sentences outside the list, 12-hour times, no emoji, approval shown where needed |

### Results

| Arm | Grounding | Completeness | Language | Policy | Register |
|---|---|---|---|---|---|
| Templates before the fixes, 20 situations | 20/20 | 20/20 | **19/20** | 20/20 | **17/20** |
| **Templates after the fixes** (what is sent now), 20 situations | **20/20** | **20/20** | **20/20** | **20/20** | **20/20** |
| Sonnet: 3 drafts recorded by the spike (synthetic options), checked free | 3/3 | 3/3 | 3/3 | 3/3 | n/a (no Hinglish draft) |
| Sonnet on the 20 situations | not run: needs approval (estimate ≤ $0.12, upper bound with every redraft) | | | | |

**What the checks found:**
1. **H02, a language bug.** A Hindi (Devanagari) conversation got an English option list. The reply language follows the **last message**, and the last message here was a one-word "हाँ". The replay's scripted model reported `en` for it; a live model may or may not.
2. **H01, H03, H05: no Hinglish templates.** Drivers who write Hinglish get English replies. The templates exist only in English and Devanagari Hindi.
3. **Otherwise the templates are already correct:** grounded, complete and within policy in every situation.
4. **The recorded Sonnet drafts pass every check but are near-copies of the template** ("Okay, here are…", "Please…").

## Fixed (18 Nov, no spend)

- **Hinglish templates.** Every reply (options, read-back, pending, status, escalation, safety, questions) now has a Hinglish version (`replies.HL`, `HL_ASK`). Dock codes, times and order references keep exactly the English form, so grounding is unchanged.
- **The reply language follows the conversation** (`replies.reply_language`, decision T43):
  - Devanagari in the message means Hindi, whatever the model says;
  - a short reply ("yes", "2", "haan") keeps the language of the driver's last longer message, read from the conversation history the driver context now carries;
  - otherwise the model's reading stands.

  No migration was needed.
- **Tests:** `test_agent_units.py` (every key has a Hinglish version with the same placeholders, and 6 language cases) and `test_orchestrator.py` (a Hinglish driver gets Hinglish options; a one-word "हाँ" stays Hindi; a short "yes" after Hinglish stays Hinglish).

## Proposed decision, pending the human review (stage 5)

With (a) Hinglish templates and (b) the conversation-level language in place, the templates now pass every check on every situation. If the human review finds no other difference that matters to drivers, **remove Sonnet from option replies**, as `07_acceptance_and_verification.md` §4 says ("Sonnet is kept only where it improves outcomes"). That would also remove a model call from the reply path. Stage 6, the Sonnet judge, would then not be needed for this reply type.

## Stage 5: human review (`review_sheet.md`)

Each situation shows the driver's messages, the template reply and its code-check results, with an empty slot for the Sonnet reply (filled only if the Sonnet arm is approved). The reviewer scores clarity, tone, language and register, and "no overpromising", 1–5 each: the qualities code cannot judge.

## Running it

```bash
python evals/phrase_ablation.py
```

```bash
python evals/phrase_ablation.py --arm sonnet --estimate-only
```

The first runs the template arm, the checks and the review sheet, free. The second prints the Sonnet estimate. The Sonnet arm's limit is $0 unless you pass `--approved --max-usd X`.
