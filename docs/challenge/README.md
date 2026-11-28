# SetuHaul FDE challenge: the hand-ins

The brief (`docs/brief/21182914-SetuHaul_Logistics_FDE_Challenge_Project_Brief.pdf`, p.5) asks for six items plus an optional scheduling tool. This page says where each one is and how to reproduce it.
- **State:** 28 Nov 2026, branch `main`. The submission was merged from `feat/fde-challenge-submission` by PR #58 on 27 Nov 2026.
- **Submission summary**, in the brief's template, with *What does not work yet*: [`SUBMISSION.md`](SUBMISSION.md).
- **How to run everything:** the root [`README.md`](../../README.md). It also explains the [live synthetic pilot](../../README.md#live-synthetic-pilot) and the three [test environments](../../README.md#test-environments).
- **Fresh local re-run, 28 Nov 2026:** [`evidence/rerun_20261128/`](evidence/rerun_20261128/README.md). All 30 audit checks, both scenario runs and the integrity proof (N = 10, 20 and 50) were reproduced; earlier evidence files are unchanged.
- **What the evidence is.** Every hand-in below is evidenced by local runs and CI, on synthetic data or the supplied snapshot, with the deterministic rules reader. None of it is pilot or production evidence.

| # | Hand-in | Where | How to reproduce | Status |
|---|---|---|---|---|
| 1 | Problem Definition Sheet and data audit | [`../1-frame/01_problem_definition_sheet.md`](../1-frame/01_problem_definition_sheet.md): facts, estimates, assumptions, unknowns. [`../1-frame/audit_results.md`](../1-frame/audit_results.md): 30 checks, each with its SQL and the rows it returns. Source: [`../../scripts/frame/data_audit.sql`](../../scripts/frame/data_audit.sql) | `python scripts/frame/run_audit.py`: read-only on the supplied SQLite file, regenerates the results byte for byte | Done. All 30 checks were re-executed independently on the original data package on 27 Nov 2026, and every row count matched |
| 2 | Decision register | [`../2-design/01_decision_register.md`](../2-design/01_decision_register.md), Part A, D1–D13 | Each decision cites its test or script | Done. D7, D8 and D11 were corrected on 27 Nov 2026 to the as-built planner and to tested refusal wording |
| 3 | Working prototype | Code in [`../../setuhaul/`](../../setuhaul/), migrations in [`../../migrations/`](../../migrations/). Versions and architecture are in the root [`README.md`](../../README.md) | `python scripts/start_local.py` (one command: local PostgreSQL plus the demo console) | Done |
| 4 | Scenario run | [`SCENARIO_RUN.md`](SCENARIO_RUN.md). Run A: the six items, [`evidence/scenario_run.json`](evidence/scenario_run.json). Run B: the supplied snapshot, [`evidence/snapshot_scenario_run.txt`](evidence/snapshot_scenario_run.txt). Measures: [`evidence/scenario_measures.md`](evidence/scenario_measures.md) | `python -m pytest -q tests/test_scenario_run.py tests/test_snapshot_scenario.py`; `python scripts/snapshot_scenario.py` | Done. Both runs end in a `NO_FEASIBLE_SLOT` escalation; on the snapshot it is SHP1015. No video was recorded |
| 5 | Integrity proof | [`INTEGRITY_PROOF.md`](INTEGRITY_PROOF.md). Output: [`evidence/integrity_proof_n10.txt`](evidence/integrity_proof_n10.txt) (N = 10), [`evidence/integrity_proof.txt`](evidence/integrity_proof.txt) (N = 20) | `python scripts/integrity_proof.py 10`; `pytest tests/test_api.py::test_fifty_drivers_race_for_one_slot` | Done. N = 10 gave 1 claim, 9 LOST, 0 double bookings; N = 20 gave 1 claim, 19 LOST, 0 double bookings; a superuser bypass is refused (`23P01`) |
| 6 | Client readout | [`CLIENT_READOUT.md`](CLIENT_READOUT.md): one page | — | Done. Rewritten 28 Nov 2026 to one page |
| + | Scheduling tool (optional) | [`../3-build/SCHEDULING_TOOL.md`](../3-build/SCHEDULING_TOOL.md), [`../../setuhaul/domain/scheduling.py`](../../setuhaul/domain/scheduling.py), [`../../scripts/scheduling_compare.py`](../../scripts/scheduling_compare.py), [`break_scheduling_cases.md`](break_scheduling_cases.md) | `python scripts/scheduling_compare.py`; `pytest tests/test_scheduling.py tests/test_move_appointment.py` | Done. A deterministic scoring planner, compared with FCFS and earliest-ETA. On the Jaipur snapshot, earliest-ETA wins on total score and waiting; this is reported as found |

**Phase 4, Break:** [`BREAK_REPORT.md`](BREAK_REPORT.md).
- 118 general cases: 114 PASS, 0 FAIL, 2 BLOCKED, 2 NOT COVERED.
- 23 scheduling cases.
- Per-file JUnit results are in [`evidence/`](evidence/).

**Consolidated test run (23 Nov 2026):** [`TEST_REPORT.md`](TEST_REPORT.md).

**Beyond the brief.** A synthetic AWS pilot (`ap-southeast-2`) is deployed. It is not needed to assess the hand-ins.
- Data: synthetic only.
- Channel: live Telegram for two owner-authorised synthetic test drivers (allowlist). WhatsApp outbound is off.
- Staff sign-in: Cognito with TOTP MFA required; roles `planner`, `operations`, `ops_lead`, `dispatcher`.
- Booking: on only through an owner-approved activation, behind a database kill switch.
- Production: there is no production environment; the code refuses one.
- Current state and where the pilot evidence lives: the root [`README.md`](../../README.md#live-synthetic-pilot).
- Pilot material: `docs/release/`, `docs/operations/`, `artifacts/fde-demo/`.
