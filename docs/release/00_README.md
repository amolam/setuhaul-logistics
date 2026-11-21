# Release plan deliverables (phases A–G), 21 Nov 2026

Status of the "Final FDE Project Cleanup, GitHub Release & AWS Sydney Pilot" plan. Work done locally; nothing pushed,
nothing deployed, no paid model calls, booking switch OFF.

| Phase | State |
|---|---|
| A Repository cleanup | Done (archive moved out, `.gitignore`, `.env.example`, requirements split, restructure into a standard layout) |
| B Provider architecture | Done (T52: OpenRouter primary, pinned ZDR route, Secrets Manager, usage ledger + daily budget; Bedrock optional) plus review fixes R1/R4 |
| C Tests and quality gates | Done locally (`01_TEST_REPORT.md`) |
| D Security and GitHub readiness | Scans done, dependencies upgraded (`02_SECURITY_SCAN.md`); push waits for approval (`03_GITHUB_RELEASE_CHECKLIST.md`) |
| E AWS Sydney pilot | Planned; build items E0–E6 needed before a first deploy (`04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md`) |
| F Step 6 acceptance | All six gates NOT TESTED, blocked on E (`05_STEP6_GATE_STATUS.md`) |
| G Monitoring, cost, operations | Done as a guide; `/health` added (`06_PILOT_OPERATIONS_GUIDE.md`) |

## The twelve deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | Repository cleanup summary | Phase A entry in `docs/3-build/README.md`; layout in the root `README.md` |
| 2 | Architecture and provider dependency map | below, and `docs/2-design/02_architecture.md` (original India design) |
| 3 | OpenRouter and Bedrock configuration | `docs/3-build/README.md` → "Model provider"; `.env.example` |
| 4 | Security scan results | `02_SECURITY_SCAN.md` |
| 5 | Full test report | `01_TEST_REPORT.md` |
| 6 | Updated documentation list | below |
| 7 | GitHub release checklist | `03_GITHUB_RELEASE_CHECKLIST.md` |
| 8 | AWS Sydney pilot deployment plan | `04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md` |
| 9 | Step 6 gate-by-gate status | `05_STEP6_GATE_STATUS.md` |
| 10 | Monthly cost estimate | `06_PILOT_OPERATIONS_GUIDE.md` §5 |
| 11 | Known risks and unresolved decisions | below |
| 12 | GO / NO-GO | below |

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

`README.md`; `.env.example`; `docs/3-build/README.md`; `docs/operations/COSTS.md`; `docs/2-design/01_decision_register.md`
(T52); `docs/review/FINDINGS_LEDGER.md`, `TRACEABILITY.md`; `presentation/RUN_SHEET.md`; new `docs/release/00–06`.

## 11. Known risks and unresolved decisions

| Item | Kind | Note |
|---|---|---|
| E0–E6 not built (IaC, Lambda entry points, Data API backend, Step Functions adapter, pipeline, Meta setup) | blocking for AWS | sized in the deployment plan |
| R2 (low): spending cap and cost are supplied by the calling code | follow-up | the $3/month key limit is an independent backstop; the cap could move into a database configuration table |
| R3 (low): no length limits on extracted text arguments | follow-up | schema validation still applies |
| R5 (info): a crashed process leaves its reservation counted for the day | follow-up | conservative; reset at 00:00 UTC |
| Bedrock model access in the Sydney account not verified | external access | AWS CLI not signed in; Bedrock is optional |
| B-AWS-0 (India Bedrock) | open, not blocking | T52 |
| S4-R2-08b "talk to a person" intent | deferred by owner | unchanged |
| Commit author e-mail visible in the repository | owner decision | rewrite to GitHub `noreply` before the first push, or keep |
| Challenge brief PDF in the repository | owner decision | fine while private |
| Race panel with N = 50 takes about 3.5 minutes on the laptop | presentation | run sheet updated |
| Booking enablement | owner decision, later | only after all six gates pass |

## 12. GO / NO-GO

| Decision | Recommendation |
|---|---|
| Merge Phase B into `main` and cut `release/fde-sydney-pilot` | **GO** after the owner's final review |
| Push to a **private** GitHub repository and tag `v0.1.0-fde-pilot` | **GO** after the owner approves the checklist |
| Deploy the AWS Sydney pilot | **NO-GO** until E0–E6 are built and reviewed |
| Claim step 6 acceptance | **NO-GO**: no gate tested in AWS |
| Turn on `SETUHAUL_BOOKING_ENABLED` | **NO-GO** (not in scope; separate owner decision after the gates) |
