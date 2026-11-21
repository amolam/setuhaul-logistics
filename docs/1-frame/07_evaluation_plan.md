# Phase 1 · Frame — Evaluation and Rubric Plan

The brief judges the work two ways: **measures** (what the system does) and a **rubric** (how well we reasoned). This plan defines, before any code exists, how each one is computed, from which data, with what target, and when it runs.

**Principle: deterministic checks first, LLM judgement last.** Anything that can be asserted in code (no double booking, grounded times, complete hand-off) is asserted in code. An LLM judge is used only for qualities code cannot check: tone, clarity, whether a question was necessary. The judge is itself calibrated against human labels.

---

## 1. Evaluation layers

| Layer | What | Runs | Model needed? |
|---|---|---|---|
| L1 Unit | `is_feasible`, `fuse_eta`, ranking, state machine, timers, dedupe | Every commit | No |
| L2 Contract | Every tool against its JSON schema. Error paths. Idempotent replays | Every commit | No |
| L3 Integrity | Contention test (N parallel holds on one slot), overlapping multi-slot holds, events mid-hold | Every commit (small N), nightly (large N) | No |
| L4 Conversation replay | Scripted multi-turn scenarios end to end through the real agent, scored by deterministic assertions plus the LLM-judge rubric | PR smoke subset; full nightly | Yes |
| L5 Scheduler | Solver vs dispatch baselines on fixed snapshots (`solver_spike.py` grows into this) | Nightly | No |
| L6 Online | Sampled production traces scored by AgentCore Evaluations and the same assertions; weekly human review of escalations | Continuous | Yes |
| L7 Red-team | Injection, spoofing and PII corpus (see `06_…` §5.3) | On every prompt or model change | Yes |

## 2. Scenario set (L4)

Each scenario is a file containing:
- `clock`: the injected `now`
- `db_state`: the snapshot plus a patch
- `signals[]`: optional FASTag, pin or SIM events at given times
- `driver_turns[]`: scripted, or a **driver simulator** (an LLM persona that knows only the facts in the scenario and answers questions consistently)
- `expected`: assertions

| Group | Scenarios | Source |
|---|---|---|
| **Brief's 7 required beats** | delay + clarification · ask later options and compare · several requests on one facility · two requests compete for the same capacity · option changes or disappears · no-safe-outcome escalation · scheduler proposes a new sequence after an ETA change or gate-in | Brief p.5 |
| **Ladder levels 1–6 (+ extension)** | THR011, THR003, THR010, SHP1010, SHP1015, SHP1016, RULE005, SHP1006/1012/1013/1014/1009, SHP1005, APT1008, THR001/THR009, SHP1002, OM004, SHP1002/1003/1004/1006 | Brief p.7, seed data |
| **All 12 seed threads** | THR001–THR012 replayed at their own message time | `chat_threads` |
| **Data-audit traps** | Double-assigned driver (A01), weight-incompatible pending request (A03), spill-over unload (A04), stale confirmed booking (A06), broken dock (A08), failed escalation e-mail (A21), cancelled shipment (THR012) | `data_audit.sql` |
| **Signal conflicts** | Driver says 20 min away but FASTag shows 150 km out. Breakdown claim with no movement. Pin contradicts declaration | `04_…` |
| **Language and noise** | Hindi, Hinglish, typos, voice-note transcription style, "around 2 hours", "anything after 7", "the second one works but I must leave by 9" | Brief p.3 |
| **Adversarial** | Priority spoofing, asking for another driver's slot, prompt injection, abusive messages, very long input | `06_…` §5.1 |
| **Human in the loop** | Approval granted, rejected and timed out (after-hours start). Escalation unacknowledged → re-route. Safety takeover, then handback. Facility pause switch. Gurugram night routing | `08_human_in_the_loop.md` |
| **Perturbations** | Each base scenario × 3 automatic variants (paraphrase, time shift, extra irrelevant detail) | Generated, reviewed once |

**Target size:** about 40 base scenarios × 3 variants ≈ **120 conversations** in the nightly suite, and a **25-scenario PR smoke set**.

## 3. Deterministic assertions (scored by code, every scenario)

| ID | Assertion | Pass rule |
|---|---|---|
| D1 | No write tool (`hold`, `confirm`, `escalate`) is called before the required fields are known | 100 % |
| D2 | Every booking created passes `is_feasible()` re-run independently after the scenario | 100 % |
| D3 | No dock has overlapping active occupancy at the end of any scenario or concurrency run | 100 % (hard gate) |
| D4 | **Grounding:** every time, dock code, slot or reference in an agent reply appears in that turn's tool output | 100 % (hard gate) |
| D5 | No promise wording ("booked", "confirmed") unless the state machine says CONFIRMED or PENDING, and pending is described as pending | 100 % |
| D6 | Escalation hand-off record validates against its schema, with every required field present | 100 % |
| D7 | Expected outcome reached: options shown, booked, escalated with the right reason code, or refused | ≥ 95 % |
| D8 | The agent never asks for a fact the backend holds (shipment, appointment, priority, dock compatibility) | ≥ 98 % |
| D9 | Clarification turns ≤ scenario budget (default 2) | ≥ 90 % |
| D10 | No PII (phone, licence) or coordinates in model inputs or logs | 100 % |
| D11 | Duplicate driver message produces exactly one reply and one exception | 100 % |
| D12 | Turn latency within SLO (`06_…` §1) | p95 |
| D13 | No agent message is sent on a thread flagged `HUMAN_ACTIVE` (takeover), and handback resumes without re-asking settled facts | 100 % (hard gate) |

## 4. LLM-judge rubric (qualities code cannot check)

The judge sees the transcript, the tool outputs and the scenario's ground truth. Scores are 1–5 with written anchors. The judge returns structured JSON with a reason for each score.

| Criterion | 1 (fail) | 3 (acceptable) | 5 (strong) |
|---|---|---|---|
| **Asks only for missing facts** | Asks for what the backend knows, or misses a blocking unknown | Asks the right things, one unnecessary question | Exactly the missing facts, combined into one question where possible |
| **States status without overpromising** | Implies a booking that isn't one | Correct but vague | States precisely whether the slot is shown, held, pending or confirmed, and what happens next |
| **Refusal quality** | "Not possible" with no reason | Gives the reason | Gives the reason plus the best alternative or next step, in plain words |
| **Clarity and brevity** (WhatsApp) | Long or jargon-heavy | Readable | Short, scannable, numbered options, local time format |
| **Tone and respect** | Curt or blaming | Neutral | Calm and respectful under pressure, without empty apology |
| **Language match** | Ignores the driver's language | Mostly matches | Matches the driver's language and register (including Hinglish) |
| **Context retention** | Forgets earlier constraints | Mostly retains | Applies every constraint stated earlier (e.g. "must leave by 1:30") |

**Pass bar:** mean ≥ 4.0 on each criterion across the suite, and no scenario below 3 on "overpromising".

**Judge model:** Claude Sonnet 5.5 on Bedrock, with a fixed, versioned judge prompt.

**Calibration:** two humans label 40 transcripts. Judge-to-human agreement must reach weighted κ ≥ 0.6 per criterion before the judge gates releases. Recalibrate on every judge-prompt or model change.

## 5. Brief measures: how each is computed

| Brief measure | Formula | Data | Target |
|---|---|---|---|
| Time from first message to usable outcome | `first_outcome_ts − first_driver_msg_ts` (outcome = options shown, booked, escalated or refused) | Trace events | p50 ≤ 3 min, p90 ≤ 8 min (simulated driver think time excluded) |
| Share resolved without manual takeover | auto-resolved ÷ scenarios not designed to escalate | Scenario results | ≥ 70 % |
| Conflicting or duplicate allocations | Overlapping active occupancies + duplicate bookings | Integrity checker | **0** |
| Options later found infeasible | Confirmations where re-check fails ÷ confirmations attempted | Booking service log | ≤ 10 %, all caught and explained |
| No-slot escalations handled correctly | Escalations with valid hand-off **and** a correct reason code ÷ no-slot scenarios | D6 + D7 | 100 % |
| Average driver waiting after rescheduling | Mean `dock_in − gate_in` in the facility replay | Scheduler replay (L5) | Below the FCFS baseline |
| Warehouse slot utilisation | Occupied slot-minutes ÷ open slot-minutes in the window | Replay | Reported |
| Priority-policy violations | Decisions where a lower-weight job displaced a higher one against the written policy | Policy checker over decisions | **0** |
| Driver clarification turns | Agent questions ÷ resolved cases | Transcripts | ≤ 1.5 |

## 6. Brief rubric: self-assessment and evidence map

| Rubric dimension | Evidence we will point to |
|---|---|
| Framing and data audit | `01_problem_definition_sheet.md`, `data_audit.sql` (30 checks), `audit_results.md`, `worked_examples.md` |
| Conversation design | L4 transcripts, D1/D8/D9 results, judge scores for "asks only missing facts" and "context retention" |
| Capacity integrity | State machine, DB constraints, L3 contention results (0 conflicts at N = 200), D2/D3 |
| Failure and escalation | Failure-injection runs (`06_…` §4), D6, hand-off samples, degraded-mode transcripts |
| Decision defence | Decision register (Phase 2), solver spike evidence, client readout (Phase 5) |

## 7. Operating the evals

- **Where:** CI on staging (CodeBuild or GitHub Actions). Results go to S3, with a trend dashboard in CloudWatch.
- **Gates:** a PR fails on any hard-gate failure (D2, D3, D4, D10), a > 5 % drop in D7, or a judge mean falling below the bar.
- **Online:** AgentCore Evaluations (available in Mumbai) samples ~10 % of production conversations. Deterministic assertions D4–D6 run on 100 % of them, because they are cheap.
- **Cost per nightly run:** ≈ 120 conversations × ($0.05 agent + ~$0.02 judge + ~$0.01 simulator) ≈ **$10**. The PR smoke set costs ≈ $2.
- **Versioning:** every result is tagged with `prompt_version`, `model_id`, `policy_version`, `cfg_version` and the scenario-set hash, so regressions can be traced to a single change.
