# SetuHaul FDE challenge: the hand-ins

The brief (`docs/brief/…Project_Brief.pdf`, p.5) asks for six items plus an optional scheduling tool. Here is where each one is, and its status on 23 Nov 2026.

| # | Hand-in | Where | Status |
|---|---|---|---|
| 1 | Problem Definition Sheet and data audit | `docs/1-frame/01_problem_definition_sheet.md`, `audit_results.md` (30 checks, each with a query) | Done (Frame) |
| 2 | Decision register | `docs/2-design/01_decision_register.md` | Done (Design). T33-P, a pilot database-version exception, is proposed in `docs/release/13_STEP6_GATE_EVIDENCE.md` and still needs the owner's decision |
| 3 | Working prototype | Code in `setuhaul/`; library versions in the root `README.md` and `requirements*.txt`; Option A deployed on AWS (`docs/release/12_CONSOLE_PILOT_REPORT.md`) | Done |
| 4 | Scenario run | `SCENARIO_RUN.md`; raw transcript in `evidence/scenario_run.json` | Done. All six must-show items plus item 7, ending in escalation |
| 5 | Integrity proof | `INTEGRITY_PROOF.md`; output in `evidence/integrity_proof.txt` | Done. 20 concurrent drivers, 1 claim on the slot, 0 overlaps |
| 6 | Client readout | `CLIENT_READOUT.md`; deck `presentation/SetuHaul_readout.pptx` | Done |
| + | Scheduling tool | `docs/3-build/SCHEDULING_TOOL.md`, `setuhaul/domain/scheduling.py`, `scripts/scheduling_compare.py` | Done. Compared with FCFS and earliest-ETA, with the results reported honestly |

**Phase 4, Break:** `BREAK_REPORT.md`. There are 118 general cases (114 PASS, 0 FAIL, 2 BLOCKED, 2 NOT COVERED) and 23 scheduling cases. The per-file JUnit results are in `evidence/`.

**Test report for this work:** `TEST_REPORT.md`.

**Safety state:** synthetic data only; booking (Option D) OFF; real WhatsApp OFF; production refused.
