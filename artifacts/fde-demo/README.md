# SetuHaul FDE demo package (26 Nov 2026, updated 27 Nov 2026)

Source of truth on 26 Nov: `origin/release/fde-sydney-pilot` at `e3c5bbe` (deployed release `4b9ac56`). **Now:**
release `d6b20b4` is deployed on the synthetic pilot, with booking ON under `OWNER-APPROVAL-20261127-DEMO-05`. All data
is synthetic.

**Reviewed against `main` on 28 Nov 2026 (`15553bb`).** This package presents the **private synthetic pilot**. It is not the challenge submission: the six hand-ins are indexed in [`docs/challenge/README.md`](../../docs/challenge/README.md), with their limits in [`docs/challenge/SUBMISSION.md`](../../docs/challenge/SUBMISSION.md). The pilot state below is as recorded on 27 Nov and owner-confirmed on 28 Nov; it was not re-checked in AWS for this review. There is no public demo URL and no production deployment.

| Role | Files |
|---|---|
| Used in the demo | `SetuHaul_FDE_Demo.pptx`, `SetuHaul_FDE_Presenter_Playbook.md` (run of show in §0: 15 minutes plus questions), `SetuHaul_FDE_Live_Demo_Runbook.md` |
| Supporting material | `SetuHaul_FDE_QA_Preparation.md`, `SetuHaul_FDE_Technical_Appendix.md`, `SetuHaul_FDE_Business_Workflow.md`, `SetuHaul_FDE_Evidence_Register.md`, `SetuHaul_FDE_Gap_Analysis.md`, `SetuHaul_FDE_Cost_Analysis.md`, `screenshots/` |
| Generated output | `SetuHaul_FDE_Demo.pptx` (from `source/build_deck.py`), `diagrams/` and `charts/` (from `source/diagrams.py` and `render.ps1`) |
| Source | `source/` |

**Deck wording corrected on 28 Nov 2026** (in the `.pptx` and in `source/build_deck.py` and `source/diagrams.py`, so a rebuild gives the same text):
- slide 1 footer: the release recorded as deployed, `d6b20b4` (27 Nov), and the opening note on concurrency;
- slide 3: traces never carry the driver's name, phone or credentials (message text is exported on the synthetic pilot, as slide 11 says);
- slides 5 and 8: 50 migrations;
- slide 6: a person decides every request in MANUAL;
- slide 12 subtitle: 2,080 tests on the 26 Nov release, 2,233 on `main` since;
- slide 16: one winner at 10, 20 and 50 requests; overlaps counted in the database at 10 and 20.

`diagrams/03_high_level_architecture.png` was re-exported on 28 Nov from the corrected `source/diagrams_source.pptx` and now shows 50 migrations.

**Reading the exports and screenshots (checked image by image on 28 Nov):**
- `diagrams/10`, `11` and `16` carry dated figures: 2,080 tests (CI on the 26 Nov release; `main` has recorded 2,233 since) and "smoke 11/11 on 4b9ac56". They are accurate for those runs.
- `diagrams/17` says "planner" for the approval step. In the demo a planner does; as implemented, an ops lead can decide any request and operations can decide a STANDARD one (Business Workflow §1). `diagrams/18` and deck slide 17 were corrected on 28 Nov to "authorized staff decide", with that note; slide 6 now says the pilot is MANUAL and that AUTO can confirm eligible chat bookings.
- `screenshots/` are 23 Nov captures. Their header pills show the state of that day ("booking execution OFF", "WhatsApp: internal test sink only"), and two are local failure-injection runs. Do not present them as the pilot's current state.
- `charts/` match the deck's numbers and keep their labels: estimate, projection, preliminary.

**27 Nov updates.** Every document below was revised for:
- the fix plan (PR #45): appointment lifecycle, automatic no-show, dispatch workflows, requested dates and full pilot trace content;
- the recovery releases: migration 0049 and categories D and X;
- the DEMO-04 change set:
  - full operational traces, with only the driver's name and phone and credentials removed;
  - one trace tree per Telegram message;
  - a $2.00/day model budget, with $0.75 for the judge;
  - planner Resolve, and no stuck HELD;
- the **canonical business workflow** (`SetuHaul_FDE_Business_Workflow.md`, deck slides 4 and 17). Every step is checked against `d6b20b4`, and the four places where the target differs from what is built are listed as gaps W1–W5.

The 27 Nov evidence is in the Evidence Register, Part D: D3a covers the deploy, D5 the workflow claims.

| File | What it is |
|---|---|
| `SetuHaul_FDE_Business_Workflow.md` | **New (27 Nov).** The canonical end-to-end workflow by actor: MANUAL vs AUTO, driver confirmation vs planner approval, no-show and rebooking, a Telegram example, and gaps W1–W5 |
| `SetuHaul_FDE_Demo.pptx` | 17-slide editable deck (slide 17 is an appendix); native shapes and charts; speaker notes on every slide. Slide 4 is now the business workflow by actor; slides 11, 14 and 15 were updated 27 Nov |
| `SetuHaul_FDE_Presenter_Playbook.md` | Per-slide script, a 15-minute run of show plus questions (§0), the full 24-minute version, demo narration, audience variants |
| `SetuHaul_FDE_QA_Preparation.md` | 101 questions in 14 groups (M: the 27 Nov releases; N: the business workflow) plus 6 rapid-fire questions, with short and detailed answers, evidence and follow-ups |
| `SetuHaul_FDE_Technical_Appendix.md` | 21 sections plus §8a (business workflow and appointment states), with Mermaid diagrams and file references |
| `SetuHaul_FDE_Live_Demo_Runbook.md` | Owner-run demo steps mapped to the canonical workflow (§4a), prerequisites, console workflows, fallbacks, reset |
| `SetuHaul_FDE_Evidence_Register.md` | Stage 1 discovery report and every claim with source, status and confidence; Part D covers 27 Nov |
| `SetuHaul_FDE_Gap_Analysis.md` | Skeptical evaluator gap table (incl. workflow gaps W1–W4) and FDE capability assessment |
| `SetuHaul_FDE_Cost_Analysis.md` | Actual vs estimated vs projected cost, unit economics, performance; 27 Nov budgets |
| `diagrams/`, `charts/` | PNG exports (1920×1080) of the 16 required visuals plus 3 extra charts, `17_business_workflow_by_actor.png` and `18_telegram_conversation_manual_vs_auto.png` (new 27 Nov). `09_langsmith_trace_lifecycle.png` was re-rendered 27 Nov |
| `screenshots/` | Anonymized Phase G/G1 console screenshots (23 Nov). Fresh Telegram and LangSmith captures are a manual-prep item |
| `source/` | `build_deck.py`, `diagrams.py`, `pptx_kit.py` (python-pptx) and `render.ps1` (PowerPoint export). To rebuild: `python artifacts/fde-demo/source/build_deck.py`, then run `render.ps1` |

**Outstanding (not done):**
- workflow gaps W1 (multi-option dispatcher offers), W3 (driver confirmation of a dispatcher time change) and W4 (driver-requested cancellation): not implemented;
- the paid golden re-run for prompt p6, deferred by the owner, so p6 is not model-validated;
- native review of the Hindi/Hinglish date replies;
- an eyeball check of one trace tree in LangSmith;
- fresh screenshots;
- the OpenRouter key's $10 lifetime limit, which is the first limit reached at $2/day.
