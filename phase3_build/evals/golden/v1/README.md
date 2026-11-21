# Golden dataset v1 (gate G3)

60 driver scenarios on the 4 August 2026 seed snapshot. Spec: `phase2_design/08_signoff_and_backlog.md` §G3. Built on 18 Nov 2026, **before any prompt tuning**. Every run records `v1+sha256:<hash>` of `scenarios.jsonl`. Changing a label means a review and a new version (`v2/`), never an edit in place.

The labels describe the **designed** behaviour (Phase 1 and the Phase 2 decision register), not what today's code does. When the code disagrees with a label, the scenario records a finding. The label does not change to match the code.

## Composition

| Group | Count | Drawn from |
|---|---|---|
| `brief_beat` | 13 | The brief's 7 required beats (p.5), its three example phrases (p.3: "around two hours late", "anything after 7", "the second one works, but I need to leave by 9"), "ask vs look up", "driver vs backend", and "ten drivers, one slot" |
| `delay_ladder` | 10 | One shipment (SHP1017, D1 12:00–13:00), from on time to 6 h late, plus 10 h late past the snapshot horizon (SHP1020) |
| `seed_thread` | 12 | THR001–THR012, replayed at their own message time |
| `audit_trap` | 10 | A04, A08, A11, A12, A18, the SHP1006 / SHP1021 conflict, APT1015A + RULE003, RULE004, RULE005, APT1008 |
| `language` | 8 | Hinglish, Devanagari, voice-note style, typos, a Hindi yes and a conditional yes |
| `adversarial` | 7 | Prompt injection, priority spoofing, another driver's slot, another driver's phone, an option never shown, abuse, "cancel all appointments" |

No real personal data: driver and shipment ids come from the synthetic seed. The replies never contain a phone number, and scenario ADV04 checks this.

## Format (`scenarios.jsonl`, one scenario per line; schema in `evals/golden.py`)

| Field | Meaning |
|---|---|
| `fixture` | `seed-2026-08-04`. A turn's `event` patches the database just before that turn (for example `block D4 13:00-14:00 CAPACITY_REDUCTION`) |
| `config` | For example `{"booking_enabled": true}`. The production switch stays off, so booking scenarios state that they turn it on |
| `now`, `turns[].at` | Frozen IST clock (HH:MM on 4 Aug) |
| `turns[].say` | The driver's message. `driver` (multi-driver scenarios) and `duplicate_of` (provider redelivery) are optional |
| `turns[].ctx` | Conversation state before the turn, for extraction-only runs: `base_eta`, shown `options` ("D2 13:00"), `awaiting` (index read back), `ships` (live order refs) |
| `turns[].x` | Expected extraction, graded after the code checks: `intent`, `eta`, `earliest`, `latest`, `latest_kind`, `kind`, `dmin`, `safety`, `pick`, `yes`, `lang`, `ref`, `exc`, `missing`. A list means any of these values passes |
| `turns[].outcome` | What code must decide: `KEEP_CURRENT`, `OPTIONS_SHOWN`, `READ_BACK`, `PENDING_REQUEST`, `LOST_REOFFER`, `STATUS`, `EXPLAIN_POLICY`, `ACK`, `UNCLEAR`, `REFUSED`, `SILENT`, `DEDUPED`, `ASK:<field>`, `ESCALATED:<reason>` |
| `turns[].ops` / `forbid` | Tools that must be called, in order (a subsequence), and tools that must not be called |
| `turns[].checks` | Option-level assertions: `start_not_before`, `dock_in`, `dock_not_in`, `not_offered`, `min_window_min`, `gate_out_by`, `needs_approval_from`, `parallel_drivers` + `exactly_one` |
| `turns[].reply` | `must` / `must_not` substrings (case-insensitive) |
| `end` | Expected database end state: escalation reason, request counts per slot, `exceptions`, `agent_replies`, `appointments_changed`, priority unchanged |
| `gates` | Hard-gate assertions D1–D13 that apply (`phase1_frame/07_evaluation_plan.md` §3) |
| `rubric` | Judge criteria that matter most here (§4) |
| `needs` | What a runner needs to play the scenario end to end: `e2e`, `multi_driver`, `dock_event`, `scheduler`, `dedupe`, `booking_on` |

## Running it

```bash
python evals/run_golden.py --extractor rules
```

```bash
python evals/run_golden.py --extractor bedrock
```

The first command is the no-AI baseline: free and deterministic. The second runs Haiku 4.5 through the India profile. It needs AWS, costs a few cents, and stops at the first error, so a blocked account records nothing.

This runner grades the **extraction layer**: schema validity, ETA precision and recall, intent accuracy and each field. Outcomes, operations, end state, grounding and the rubric are labelled for the end-to-end runner, which needs the seed-fixture loader (backlog B-EV-2).

## Results (18 Nov 2026)

Haiku 4.5 was reached through OpenRouter, because the AWS account cannot call Claude yet (decision T38, backlog B-AWS-0). OpenRouter served every Haiku extraction from Amazon Bedrock; the Sonnet phrasing calls in the spike were served by Claude Platform on AWS. The data is synthetic. Result files are in `evals/results/`.

| Run | Dataset | Fully right turns | Schema valid | ETA precision | ETA recall | Intent accuracy |
|---|---|---|---|---|---|---|
| Rules baseline | `66a96947` | 51 / 66 | 1.00 | 1.00 | **0.82 ✗** | **0.76 ✗** |
| Haiku 4.5, prompt p1 (untuned) | `d9f6930b` | 58 / 66 | **0.955 ✗** | 1.00 | 1.00 | 1.00 |
| Haiku 4.5, prompt p2 | `66a96947` | **65 / 66** | 1.00 | 1.00 | 1.00 | 1.00 |

Gates are ≥ 0.99 validity, ≥ 0.95 ETA precision and recall, and ≥ 0.95 intent accuracy.

**Rules vs Haiku (B-AI-3, extraction):** keep Haiku. It passes every gate; the rules baseline fails two. The rules miss ranges, "90 min behind", "in two hours", Devanagari, voice-note numbers and some intents.

**Cost:** about 1,880 input and 330 output tokens per extraction, roughly **$0.0035**, close to the Phase 2 estimate of $0.0045. Latency is a median of about 2.2 s through OpenRouter, which says nothing about Mumbai. The prompt cache read nothing: the system prompt is shorter than Haiku's minimum cacheable length.

**Read the p2 numbers with care.** p2 was tuned on this same set, so 65 / 66 is optimistic. Phase 1 plans three perturbed variants per scenario (paraphrase, time shift, extra detail) as a held-out set (backlog B-EV-1b). Run that before treating the margin as settled.

### What run 1 (p1) found, and what changed in p2

| Seen in | Problem | Change |
|---|---|---|
| S05 (spike), AT01, AT03, AT04 | `explicit_confirmation: "null"`, a string where the schema requires a boolean: the validity failure. A strict flag through OpenRouter did not stop it | Prompt: the two boolean fields are always true or false |
| H06, H08 | Choosing an option, and "yes, but give me the 2 o'clock", came back as confirmations. This was already harmless: the database classifier decides confirmations (T35) | Prompt: choosing is not confirming; any condition means not a yes |
| ADV02 | The model flagged "CRITICAL medical load" as a safety concern, as the code's word list had done | Prompt: the type of cargo is never a safety concern |
| 53 of 66 turns | The order reference was copied from the context although the driver never wrote it | Prompt: only a reference the driver wrote |
| THR002 | "Leave the warehouse before 1:30" read as gate-out, against decision D1 | **Code check:** gate-out is accepted only if the message mentions the gate, unload times only if it mentions unloading; otherwise the agent asks |
| Spike phrasing | **Sonnet 5 rejects a non-default temperature.** The Bedrock client had the same bug, so in production every Sonnet phrasing call would have failed and quietly fallen back to templates | Temperature removed from both clients' phrasing calls; test added |

After p2, the spike's 3 Sonnet 5 drafts (English, Hindi, and an upset driver) all passed the grounding check.

### Open for review

- **AT06:** for "can I get a Gurugram slot at 2 PM?", Haiku records 14:00 as an arrival time, while the label expects an earliest start. The label is debatable. Either way the scenario escalates, because of the assignment conflict.
- **Breakdown rule (H04):** Phase 1 sends any breakdown not yet fixed to a person as a safety case, so every roadside puncture becomes a coordinator call. Is that intended?
- **ETA precision covers only turns that carry an `eta` label.** In AT01, "Is my 11 o'clock dock confirmed?", the model records 11:00 as an arrival time, and that is not counted. v2 should label `eta: null` on every turn.

## Findings from the dataset itself, and how they were fixed (18 Nov)

| # | Finding | Fix | Proof |
|---|---|---|---|
| 1 | ADV02: the safety word list fired on "medical load". The seed's own cargo includes "Medical devices" and "Hospital consumables" | `hospital` and `medical` count only with a person in need ("to the hospital", "medical help", "need a doctor", "अस्पताल ले …") | `test_agent_units.py::test_safety_words_do_not_fire_on_cargo_names`, `test_orchestrator.py::test_finding1_…`; the perfect-extraction check now has no exceptions |
| 2 | L01, L02, ADV02: a delay that still fits the slot was offered new slots | When the engine says `appointment_at_risk` is false, the agent keeps the slot and says so. Asking for options, or giving a time window, still gets options | `test_finding2_…` (two tests) |
| 3 | THR003: "late by one hour" was turned into an ETA without the question decision D1 requires | A bare delay proposes the computed arrival ("Do you mean around 11:00 AM? Reply YES, or send your arrival time"). YES makes it the ETA; NO asks for the time | `test_agent_units.py::test_duration_proposes_…`, `test_finding3_…` (two tests) |
| 4 | THR010, THR002: facts from earlier turns were lost | Migration `0008_turn_facts.sql`: unresolved facts are kept per conversation, written only by `note_turn_facts()`. The function checks the driver owns the thread, takes the conversation lock, accepts a fixed set of keys and caps the size. Facts expire after 2 h and are cleared once acted on. The agent can read its own row but cannot write the table | `test_finding4_…` (two tests), `test_turn_facts_are_written_only_through_the_checked_function`, role allowlists in `test_agent_not_operator.py` |

## Revision log

| Hash | Change |
|---|---|
| `256de641b66e8ed4` | First version |
| `d9f6930bd6269cc5` | Before review, made consistent with decision D1: 16 scenarios with a bare delay (BB08, BB11, L02–L06, L08–L10, THR003, THR010, AT08, AT09, H01, H02) now expect one confirming question and a "Yes" turn before the original outcome. The first version expected options straight away in L04 but a question in THR003 for the same kind of message |
| `66a96947a6bf1ad7` | Before review: H04 ("repair me 40 min lagega") now expects a safety escalation, as `02_variables.md` requires for a breakdown that is not fixed. The first label expected a question |
