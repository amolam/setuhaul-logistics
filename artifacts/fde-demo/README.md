# SetuHaul FDE demo package (26 Nov 2026, updated 27 Nov 2026)

Source of truth on 26 Nov: `origin/release/fde-sydney-pilot` at `e3c5bbe` (deployed release `4b9ac56`). **Now:**
release `d6b20b4` is deployed on the synthetic pilot, with booking ON under `OWNER-APPROVAL-20261127-DEMO-05`. All data
is synthetic.

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
| `SetuHaul_FDE_Presenter_Playbook.md` | Per-slide script, timing (≈ 24 min incl. 6-min demo), demo narration, audience variants |
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
