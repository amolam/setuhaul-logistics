# SetuHaul FDE demo package (26 Nov 2026, updated 27 Nov 2026)

Source of truth on 26 Nov: `origin/release/fde-sydney-pilot` at `e3c5bbe` (deployed release `4b9ac56`). All data is synthetic.

**27 Nov update.** Every document below was revised for the 27 Nov releases:
- the fix plan (PR #45): appointment lifecycle, automatic no-show, dispatch workflows, requested dates and full pilot trace content;
- the recovery releases: migration 0049 and categories D and X. Release `5b661a1` renewed, and booking was ON at 10:23 UTC;
- the DEMO-04 change set:
  - full operational traces, with only the driver's name and phone and credentials removed;
  - one trace tree per Telegram message;
  - a $2.00/day model budget, with $0.75 for the judge;
  - planner Resolve, and no stuck HELD.

Each document marks what changed. Items that depend on the DEMO-04 deploy are marked and listed as pending in the
Evidence Register, Part D, which also holds the 27 Nov evidence.

| File | What it is |
|---|---|
| `SetuHaul_FDE_Demo.pptx` | 16-slide editable deck; native shapes and charts; speaker notes on every slide (slides 4, 11, 14 and 15 updated 27 Nov) |
| `SetuHaul_FDE_Presenter_Playbook.md` | Per-slide script, timing (≈ 24 min incl. 6-min demo), demo narration, audience variants |
| `SetuHaul_FDE_QA_Preparation.md` | 96 questions in 13 groups (group M covers the 27 Nov releases) plus 6 rapid-fire questions, with short and detailed answers, evidence and follow-ups |
| `SetuHaul_FDE_Technical_Appendix.md` | 21 sections with Mermaid diagrams and file references |
| `SetuHaul_FDE_Live_Demo_Runbook.md` | Owner-run demo steps, prerequisites, new console workflows, fallbacks, reset |
| `SetuHaul_FDE_Evidence_Register.md` | Stage 1 discovery report and every claim with source, status and confidence; Part D covers 27 Nov |
| `SetuHaul_FDE_Gap_Analysis.md` | Skeptical evaluator gap table and FDE capability assessment |
| `SetuHaul_FDE_Cost_Analysis.md` | Actual vs estimated vs projected cost, unit economics, performance; 27 Nov budgets |
| `diagrams/`, `charts/` | PNG exports (1920×1080) of the 16 required visuals plus 3 extra charts (`09_langsmith_trace_lifecycle.png` re-rendered 27 Nov) |
| `screenshots/` | Anonymized Phase G/G1 console screenshots (23 Nov). Fresh Telegram and LangSmith captures are a manual-prep item |
| `source/` | `build_deck.py`, `diagrams.py`, `pptx_kit.py` (python-pptx) and `render.ps1` (PowerPoint export). To rebuild: `python artifacts/fde-demo/source/build_deck.py`, then run `render.ps1` |

**Outstanding (not done):**
- the paid golden re-run for prompt p6, deferred by the owner, so p6 is not model-validated;
- native review of the Hindi/Hinglish date replies;
- verification of DEMO-04 after the deploy: the trace tree and live judge scores;
- fresh screenshots;
- the OpenRouter key's $10 lifetime limit, which is the first limit reached at $2/day.
