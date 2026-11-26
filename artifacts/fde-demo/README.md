# SetuHaul FDE demo package (26 Nov 2026)

Source of truth: `origin/release/fde-sydney-pilot` at `e3c5bbe` (deployed release `4b9ac56`). All data is synthetic. Nothing in this folder changed application code, infrastructure, migrations or deployment configuration. No paid model call or external message was made to prepare it.

| File | What it is |
|---|---|
| `SetuHaul_FDE_Demo.pptx` | 16-slide editable deck; native shapes and charts; speaker notes on every slide |
| `SetuHaul_FDE_Presenter_Playbook.md` | Per-slide script, timing (≈ 24 min incl. 6-min demo), demo narration, audience variants |
| `SetuHaul_FDE_QA_Preparation.md` | 90 questions + 6 rapid-fire, with short and detailed answers, evidence and follow-ups |
| `SetuHaul_FDE_Technical_Appendix.md` | 21 sections with Mermaid diagrams and file references |
| `SetuHaul_FDE_Live_Demo_Runbook.md` | Owner-run demo steps, prerequisites, fallbacks, reset |
| `SetuHaul_FDE_Evidence_Register.md` | Stage 1 discovery report and every claim with source, status and confidence |
| `SetuHaul_FDE_Gap_Analysis.md` | Skeptical evaluator gap table and FDE capability assessment |
| `SetuHaul_FDE_Cost_Analysis.md` | Actual vs estimated vs projected cost, unit economics, performance |
| `diagrams/`, `charts/` | PNG exports (1920×1080) of the 16 required visuals plus 3 extra charts |
| `screenshots/` | Anonymized Phase G/G1 console screenshots (23 Nov). Fresh Telegram and LangSmith captures are a manual-prep item |
| `source/` | `build_deck.py`, `diagrams.py`, `pptx_kit.py` (python-pptx) and `render.ps1` (PowerPoint export). To rebuild: `python artifacts/fde-demo/source/build_deck.py`, then run `render.ps1` |
