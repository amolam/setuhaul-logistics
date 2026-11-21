# Release plan deliverables (phases A–G), 21 Nov 2026

Status of the "Final FDE Project Cleanup, GitHub Release & AWS Sydney Pilot" plan. Work done locally; nothing pushed,
nothing deployed, no paid model calls, booking switch OFF.

| Phase | State |
|---|---|
| A Repository cleanup | Done (archive moved out, `.gitignore`, `.env.example`, requirements split, restructure into a standard layout) |
| B Provider architecture | Done (T52: OpenRouter primary, pinned ZDR route, Secrets Manager, usage ledger + daily budget; Bedrock optional) plus review fixes R1/R4 |
| C Tests and quality gates | Done locally (`01_TEST_REPORT.md`) |
| D Security and GitHub readiness | Scans done, dependencies upgraded (`02_SECURITY_SCAN.md`); push waits for approval (`03_GITHUB_RELEASE_CHECKLIST.md`) |
| E AWS Sydney pilot | **E0–E6 implemented and tested locally** (`07_AWS_IMPLEMENTATION.md`); not deployed (`04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md`) |
| F Step 6 acceptance | All six gates NOT TESTED, blocked on E (`05_STEP6_GATE_STATUS.md`) |
| G Monitoring, cost, operations | Done as a guide; `/health` added (`06_PILOT_OPERATIONS_GUIDE.md`) |

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
| 9 | Step 6 gate-by-gate status | `05_STEP6_GATE_STATUS.md` |
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
| E0–E6 built and tested locally, not deployed; open items in `07_AWS_IMPLEMENTATION.md` (template sending outside the 24-hour window, secret split, `/tools` roles, action SHA pins) | blocking for the gates | owner approvals |
| R2 (low): spending cap and cost are supplied by the calling code | follow-up | the $3/month key limit is an independent backstop; the cap could move into a database configuration table |
| R3 (low): no length limits on extracted text arguments | follow-up | schema validation still applies |
| R5 (info): a crashed process leaves its reservation counted for the day | follow-up | conservative; reset at 00:00 UTC |
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
| Merge Phase B into `main` and cut `release/fde-sydney-pilot` | **GO** after the owner's final review |
| Push to a **private** GitHub repository and tag `v0.1.0-fde-pilot` | **GO** after the owner approves the checklist |
| Deploy the AWS Sydney pilot | **NO-GO** until the E0–E6 implementation is reviewed and the deployment is separately approved |
| Claim step 6 acceptance | **NO-GO**: no gate tested in AWS |
| Turn on `SETUHAUL_BOOKING_ENABLED` | **NO-GO** (not in scope; separate owner decision after the gates) |
