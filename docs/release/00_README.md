# Release plan deliverables (phases A–G)

> **Current state (23 Nov 2026)**
> - **Option A pilot: deployed and AWS-verified.**
>   - Stack `SetuHaulPilot` in `ap-southeast-2`, deployed from `eaad598` by run 45039719968.
>   - `main` and `release/fde-sydney-pilot` are both at `9d2145c`; the code is the same as `eaad598`, and the later commit only adds documents.
>   - Synthetic data, internal test sink, hosted staff console. **Option D (booking) ACTIVE on the synthetic pilot since 23 Nov 2026** (`OWNER-APPROVAL-20261123-OPTION-D`; `14_OPTION_D_ACTIVATION.md`): booking on for the api and agent only, with the three owner-approved pilot exceptions and a database kill switch.
>   - Evidence: `12_CONSOLE_PILOT_REPORT.md` and `evidence/deploy_check.json` (29 of 29 checks).
> - **Challenge completion work: implemented and tested locally, not deployed.** On branch `phase-f/challenge-completion`: the facility scheduling tool and `move_appointment` (migration 0025), the Option D switch and the gate-evidence tool, the Break and Defend deliverables (`docs/challenge/`), and the follow-ups. Test results: `docs/challenge/TEST_REPORT.md`.
> - **Step 6:** measured live. BOOKING_WORKFLOW, AWS_QUEUE and RECOVERY_WORKER **PASS**; AURORA PASS WITH PILOT EXCEPTION (T33-P); WORKLOAD_IDENTITY PARTIAL (all 5 acceptance checks pass live; only drift detection, not on the Free plan); OUTBOUND_DELIVERY BLOCKED (`13_STEP6_GATE_EVIDENCE.md`). Booking stays OFF.
>
> The tables below are the 21–22 Nov plan, with their status columns updated.

| Phase | State |
|---|---|
| A Repository cleanup | Done (archive moved out, `.gitignore`, `.env.example`, requirements split, restructure into a standard layout) |
| B Provider architecture | Done (T52: OpenRouter primary, pinned ZDR route, Secrets Manager, usage ledger + daily budget; Bedrock optional) plus review fixes R1/R4 |
| C Tests and quality gates | Done; the latest consolidated run is in `docs/challenge/TEST_REPORT.md` |
| D Security and GitHub readiness | Done: private repository `amolam/setuhaul-logistics`, tag `v0.1.0-fde-pilot` (`02_SECURITY_SCAN.md`, `03_GITHUB_RELEASE_CHECKLIST.md`). **Frozen synthetic booking-enabled pilot baseline: `v0.2.1` (`4f7da62`)** (`15_RELEASE_NOTES.md`) |
| E AWS Sydney pilot | **Option A deployed and verified** (`10_EXPRESS_AURORA.md`, `11_STAFF_CONSOLE.md`, `12_CONSOLE_PILOT_REPORT.md`). **Option D (booking) ACTIVE on the synthetic pilot since 23 Nov 2026** (`OWNER-APPROVAL-20261123-OPTION-D`; `14_OPTION_D_ACTIVATION.md`) |
| F Step 6 acceptance | **Measured live (22–23 Nov):** BOOKING_WORKFLOW, AWS_QUEUE and RECOVERY_WORKER **PASS**; AURORA PASS WITH PILOT EXCEPTION (T33-P); WORKLOAD_IDENTITY PARTIAL (all 5 acceptance checks pass live; only drift detection, not on the Free plan); OUTBOUND_DELIVERY BLOCKED (`13_STEP6_GATE_EVIDENCE.md`) |
| G Monitoring, cost, operations | Done as a guide; `/health`; pilot wake and sleep; pause evidence from RDS events (`06_PILOT_OPERATIONS_GUIDE.md`) |

## The twelve deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | Repository cleanup summary | §1 below; layout in the root `README.md` |
| 2 | Architecture and provider dependency map | below, and `docs/2-design/02_architecture.md` (original India design) |
| 3 | OpenRouter and Bedrock configuration | `docs/3-build/README.md` → "Model provider"; `.env.example` |
| 4 | Security scan results | `02_SECURITY_SCAN.md` |
| 5 | Full test report | `01_TEST_REPORT.md` |
| 6 | Updated documentation list | below |
| 7 | GitHub release checklist | `03_GITHUB_RELEASE_CHECKLIST.md` |
| 8 | AWS Sydney pilot deployment plan | `04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md`; implementation of E0–E6: `07_AWS_IMPLEMENTATION.md` |
| 9 | Step 6 gate-by-gate status | `05_STEP6_GATE_STATUS.md`; live evidence `13_STEP6_GATE_EVIDENCE.md` |
| 10 | Monthly cost estimate | `06_PILOT_OPERATIONS_GUIDE.md` §5 |
| 11 | Known risks and unresolved decisions | below |
| 12 | GO / NO-GO | below |

## 1. Repository cleanup (Phase A, commits 51af118 and 77e82cc)

- 25 old review bundles moved out of the repository to `G:\FDE-Projects\setUhaul_archive\`; Python caches removed.
- Root `.gitignore` (caches, `.env*` except the template, keys, zips, local test-cluster folders) and `.env.example`
  with placeholders only.
- Requirements split into `requirements.txt` (runtime), `requirements-aws.txt` (boto3), `requirements-dev.txt` (tests,
  audit), all pinned.
- The repository became a local git repository (`51af118`, a snapshot of the build as it was), then was restructured
  into a standard layout (`77e82cc`): code at the top level (`setuhaul/`, `migrations/`, `tests/`, `evals/`, `demo/`,
  `scripts/`), write-ups under `docs/` by phase. `.gitattributes` sets `* -text` so migration bytes (and their hashes)
  never change.
- Root `README.md` rewritten; current-status blocks added to `docs/3-build/README.md` and `docs/review/REVIEW_REQUEST.md`;
  stale statements corrected (F5/F6 owner confirmation and the native-speaker review recorded as done; B-AI-3 recorded
  as superseded by OD-3, without claiming a Sonnet comparison was run).
- Nothing deleted from tests, fixtures, migrations, evaluation evidence or review history.

## 2. Provider dependency map

```
driver message ─► orchestrator ─► extract(): model_for(env) ─┬─► OpenRouterModel ─► openrouter.ai ─► amazon-bedrock route (pinned, ZDR)
                     │                                       │      key: Secrets Manager (staging/pilot) │ env/key file (dev)
                     │                                       │      meter: reserve_model_call / settle_model_call (0023)
                     │                                       └─► BedrockModel (optional, dev/eval/ci, ap-southeast-2, au. profile)
                     │  any ModelError (budget, timeout, 4xx/5xx, malformed, provider mismatch) ─► rules reader
                     ▼
             validated Extraction (schema, extra keys refused) ─► Python rules + PostgreSQL functions decide
                                                                   (feasibility, hold, request, approval: no model involved)
replies ─► fixed templates (OD-3; no phrasing model in production)
```

## 6. Documentation changed in this work

Since the restructure (`git diff --name-only 77e82cc HEAD`): `README.md`; `.env.example`; `docs/3-build/README.md`;
`docs/operations/COSTS.md`; `docs/2-design/01_decision_register.md` (T52); `docs/review/FINDINGS_LEDGER.md`,
`REVIEW_REQUEST.md`, `TRACEABILITY.md`; `presentation/` (deck, `build_deck.js`, `RUN_SHEET.md`,
`demo_changes_prompt.md`); new `docs/release/00–06`.

## 11. Known risks and unresolved decisions

| Item | Kind | Note |
|---|---|---|
| Step 6 gates (23 Nov): BOOKING_WORKFLOW, AWS_QUEUE and RECOVERY_WORKER **PASS**; AURORA PASS WITH PILOT EXCEPTION (T33-P); WORKLOAD_IDENTITY PARTIAL (all 5 acceptance checks pass live; only drift detection, not on the Free plan); OUTBOUND_DELIVERY BLOCKED | blocking for booking | owner: drift detection (paid AWS Config, or accept as a pilot exception); a Meta test number and templates; whether T33-P may count for Option D (`13_STEP6_GATE_EVIDENCE.md`) |
| R2 (low): spending cap and cost are supplied by the calling code | **deferred (23 Nov)** | the cap comes from the deployment configuration (`SETUHAUL_LLM_DAILY_BUDGET_USD`), backed by the $3/month key limit. Moving it into the database needs a migration and new grants, which is out of scope for a low item |
| R3 (low): no length limits on extracted text arguments | **implemented (23 Nov)** | every free-text field is bounded in the schema and the parser; over-long values fall back to the rules reader (`tests/test_extraction_bounds.py`) |
| R5 (info): a crashed process leaves its reservation counted for the day | **deferred, accepted (23 Nov)** | errs towards spending less; resets at 00:00 UTC |
| Bedrock model access in the Sydney account not verified | external access | AWS CLI not signed in; Bedrock is optional |
| B-AWS-0 (India Bedrock) | open, not blocking | T52 |
| S4-R2-08b "talk to a person" intent | deferred by owner | unchanged |
| Commit author e-mail visible in the repository | decided | owner keeps their address (22 Nov 2026) |
| Challenge brief PDF in the repository | owner decision | fine while private |
| Race panel with N = 50 takes about 3.5 minutes on the laptop | presentation | run sheet updated |
| Booking enablement | owner decision, later | only after all six gates pass |

## 12. GO / NO-GO

| Decision | Recommendation |
|---|---|
| Merge Phase B into `main` and cut `release/fde-sydney-pilot` | Done (22 Nov) |
| Push to a **private** GitHub repository and tag `v0.1.0-fde-pilot` | Done (22 Nov) |
| Deploy the AWS Sydney pilot (Option A) | **Done (22 Nov)**, after the owner's approval |
| Deploy the 23 Nov challenge-completion work | **Done (23 Nov, owner-approved):** `20180e7`, including migration 0025, the relay IAM fix and the task-function fix; booking OFF |
| Claim step 6 acceptance | **NO-GO**: no gate has passed unconditionally; AURORA holds a pilot-only exception (`13_STEP6_GATE_EVIDENCE.md`) |
| Turn on `SETUHAUL_BOOKING_ENABLED` (Option D) | **Done on the synthetic pilot (23 Nov 2026)** through `activate-booking.yml` with the owner's approval; renew the 7-day evidence by 30 Nov; production NO-GO (T33: 18.3+) |
