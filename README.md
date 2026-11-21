# SetuHaul Logistics: Driver Exception and Dock Re-scheduling Service

FDE portfolio project. Truck drivers message SetuHaul on WhatsApp when they run late or break down. The service reads
the message, offers dock slots that still work, books or requests approval, and stays correct when many drivers want
the same slot. **All driver, shipment, warehouse and booking data in this repository is synthetic.**

## Status (21 Nov 2026)

| Area | Status |
|---|---|
| Phase 1 Frame, Phase 2 Design | Done |
| Phase 3 Build, local | **Done.** 800 tests pass on local PostgreSQL 18 (developer-reported full run after Phase B, 21 Nov 2026) |
| Human checks | Demo walkthroughs F1–F9 done; F5 and F6 confirmed by the owner. Native-speaker Hindi/Hinglish review done (S4-R2-08) |
| AWS deployment (step 6) | **Not started.** B-AWS-0 (the account is limited to Sydney, `ap-southeast-2`) and the six deployment gates in `docs/operations/DEPLOYMENT_GATES.md` are open |
| Live booking | **Off.** `SETUHAUL_BOOKING_ENABLED` stays off until the six gates pass and the owner approves |

**Local work complete does not mean AWS accepted.** Everything above runs locally with stand-ins for AWS (local queue,
local workflow, locally signed tokens, a local WhatsApp provider). Real AWS acceptance is step 6.

## How it works

- **The AI only reads.** One model call per driver message extracts a structured report (arrival time, delay, breakdown,
  chosen option). It is validated against a strict schema; anything invalid or unclear makes the agent ask again.
- **Code and the database decide.** Python rules and PostgreSQL functions check feasibility, select and hold slots,
  submit requests and record approvals. Warehouse staff approve where policy requires.
- **Every reply is a fixed template** in English, Hindi or Hinglish (owner decision OD-3); no model writes replies.
- **If the model is unavailable**, a rules-based reader takes over and booking is unaffected.

**Model provider (decision T52):** OpenRouter is the primary provider for the synthetic-data Sydney pilot: Claude Haiku
4.5, pinned to one zero-data-retention upstream route (Amazon Bedrock through OpenRouter) with fallbacks off, the key in
AWS Secrets Manager, every attempt metered against a daily budget. Amazon Bedrock is kept as an optional provider.
Details: `docs/3-build/README.md` ("Model provider") and `docs/operations/COSTS.md`.

## Layout

| Path | Contents |
|---|---|
| `setuhaul/` | The application package: domain rules (ETA fusion, feasibility), API and booking service, agent (extraction, orchestrator, replies), approval workflow, outbound notifications, operations helpers |
| `migrations/` | PostgreSQL migrations 0001–0022 (database functions own booking and approval state) |
| `tests/` | The test suite (800 tests) |
| `evals/` | Golden sets, held-out set, end-to-end replay, cost ledger and recorded results (evaluation evidence) |
| `demo/` | Local demo console with guided walkthroughs |
| `scripts/` | One-off tools: Phase 1 audit, feasibility prototype and solver spike (`frame/`), schema validator (`design/`), Bedrock spike (`spike/`) |
| `data/` | Synthetic data package: SQLite snapshot, schema and seed SQL, data dictionary, ER diagram, guide |
| `docs/brief/` | Client brief (PDF) and reference architecture sketch |
| `docs/1-frame/` | **Phase 1 · Frame**: problem definition, data audit, variables, high-level design, ETA signal strategy |
| `docs/2-design/` | **Phase 2 · Design**: decision register, AWS architecture, data model (`schema.sql`), tool contracts, state machines, agent design, cost; approval (B-WF-2) and outbound (B-WF-3) designs |
| `docs/3-build/` | **Phase 3 · Build**: the build log (status, step history, evidence) |
| `docs/review/` | `FINDINGS_LEDGER.md` (every review finding and its status), `TRACEABILITY.md` (requirement → test), `REVIEW_REQUEST.md`, `REVIEW_LOG.md` (review history, kept as written) |
| `docs/operations/` | `DEPLOYMENT_GATES.md` (what step 6 must prove), `RUNBOOK_0011.md`, `COSTS.md` (model spend so far) |

The project was built in phases (Frame, Design, Build); the code is at the top level and each phase's write-up is under
`docs/`. Paths in `docs/review/REVIEW_LOG.md` predate this layout and are mapped at the top of that file.

## Run the tests

Needs Python 3.14 and a local PostgreSQL 18 server.

```bash
pip install -r requirements-dev.txt
```

```bash
SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres" python -m pytest tests -q
```

Without `SETUHAUL_TEST_DSN` only the database-free tests run (the rest are skipped). Configuration names are listed in
`.env.example` (placeholders only).

## Run the demo console

```bash
python demo/server.py
```

Opens a local console at `http://127.0.0.1:8765/demo` with guided walkthroughs. It uses the rules-based reader, so no
model is called and nothing is billed.

## Phase 1 and 2 evidence scripts

The audit and feasibility scripts need only the Python standard library; the solver spike needs OR-Tools.

```bash
python scripts/frame/run_audit.py
```

```bash
python scripts/frame/feasibility_prototype.py
```

```bash
pip install ortools==9.15.6755
```

```bash
python scripts/frame/solver_spike.py
```

Validate the Phase 2 data model (creates and drops its own `setuhaul_validate` database):

```bash
python scripts/design/validate_schema.py "host=localhost port=5432 user=postgres"
```
