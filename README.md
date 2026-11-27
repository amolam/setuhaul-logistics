# SetuHaul Logistics: Driver Exception and Dock Re-scheduling Service

FDE Academy challenge project. Truck drivers message SetuHaul when they run late. The service reads the message,
offers dock slots that still work, takes the driver's choice to the warehouse as a request, and stays correct when
ten drivers want the same dock slot. **All driver, shipment, warehouse and booking data in this repository is
synthetic.**

> **Evaluators: start here → [`docs/challenge/README.md`](docs/challenge/README.md).** It indexes the six hand-ins,
> the optional scheduling tool, the submission summary and *What does not work yet*.

## Status (27 Nov 2026)

| Area | Status |
|---|---|
| Challenge hand-ins 1–6 and the optional scheduling tool | Done, each with reproducible evidence: [`docs/challenge/README.md`](docs/challenge/README.md) |
| Local prototype | Runs with one command (below) on PostgreSQL 18. Booking runs under the local harness |
| AWS synthetic pilot (`ap-southeast-2`) | Deployed, **synthetic data only**. Telegram test bot for two allowlisted test drivers; WhatsApp off; booking on only through an owner-approved activation with fresh evidence, behind a database kill switch; production refused by the code. This is **beyond the brief** and not needed to assess it |

## Architecture

```
driver message ──▶ signed webhook (WhatsApp design / Telegram pilot / local test provider)
                  ──▶ inbound queue (local queue · SQS FIFO in AWS)
                  ──▶ orchestrator: deterministic state machine (Python decides every next step)
                        ├─ reader: AI model (Claude Haiku 4.5 via OpenRouter, strict schema) or the rules reader
                        │          it only turns text into a structured report; it never books, ranks or promises
                        ├─ tools: get_driver_context · record_driver_report · find_feasible_options · select_option ·
                        │         confirm_selection · hold_option · submit_booking_request · release_hold ·
                        │         get_booking_status · escalate_to_human · propose_facility_schedule (read-only)
                        └─ replies: fixed templates (English, Hindi, Hinglish)
                  ──▶ booking service + PostgreSQL: feasibility, holds, requests, approvals, escalations;
                        roles, row-level security, SECURITY DEFINER functions, EXCLUDE constraint ex_one_truck_per_dock
staff console (planner · operations · ops lead · dispatcher) ──▶ approvals (local workflow · Step Functions in AWS),
                        escalations, takeover, scheduling proposals, MANUAL/AUTO mode, kill switch
```

Conversation state lives in PostgreSQL (threads, turn facts, shown options, selections), not in the model. Details:
[`docs/2-design/02_architecture.md`](docs/2-design/02_architecture.md),
[`docs/2-design/04_tool_contracts_and_states.md`](docs/2-design/04_tool_contracts_and_states.md).

## Technology stack and pinned versions

| Layer | Library / tool | Version | Pinned in |
|---|---|---|---|
| Language | Python | 3.14 locally (3.13 in CI and the Lambda runtime) | `pyproject.toml`, `.github/workflows/ci.yml` |
| Database | PostgreSQL | 18 locally (18.6 used for the evidence); 17.9 on the pilot under exception T33-P | — |
| API | fastapi | 0.165.0 | `requirements.txt` |
| | starlette | 1.10.0 | `requirements.txt` |
| | pydantic | 2.12.5 | `requirements.txt` |
| | uvicorn | 0.37.0 | `requirements.txt` |
| DB driver | psycopg[binary] | 3.2.13 | `requirements.txt` |
| HTTP (OpenRouter, Telegram, LangSmith REST) | httpx | 0.28.1 | `requirements.txt` |
| Auth | PyJWT | 2.15.1 | `requirements.txt` |
| | cryptography | 50.0.2 | `requirements.txt` |
| Tests | pytest | 9.2.0 | `requirements-dev.txt` |
| | httpx2 (Starlette test client) | 2.16.2 | `requirements-dev.txt` |
| | pip-audit | 2.10.1 | `requirements-dev.txt` |
| AWS runtime | boto3 / botocore | 1.44.7 | `requirements-aws.txt` |
| | mangum | 0.22.0 | `requirements-aws.txt` |
| Infrastructure | aws-cdk-lib | 2.278.0 | `requirements-infra.txt` |
| | constructs | 10.8.1 | `requirements-infra.txt` |
| AI model | Claude Haiku 4.5 (`anthropic/claude-haiku-4.5`), via OpenRouter, pinned to one upstream route | — | decision T52 |

No agent framework and no solver library are used. The orchestrator is plain Python, and the scheduling tool is a
deterministic scoring planner. OR-Tools was used only in the Frame spike (`scripts/frame/solver_spike.py`).

## Run it locally

**Prerequisites:**
- Python 3.14;
- PostgreSQL 18 server binaries (`initdb`, `pg_ctl`). They are found on PATH or in the standard install folder, or set `SETUHAUL_PG_BIN`.
- No AWS account, API key or network is needed after `pip install`.

```bash
pip install -r requirements-dev.txt
```

```bash
python scripts/start_local.py
```

`start_local.py`:
- creates a private throwaway cluster in `.local/pgdata` (git-ignored);
- starts it on port 55432;
- applies every migration and seeds a synthetic demo;
- serves the console at **http://127.0.0.1:8765/demo**. You play the driver on the left and the warehouse staff on the right.
- The demo uses the rules reader: no model call, no cost.

Stop the database afterwards:

```bash
python scripts/start_local.py --stop
```

If PostgreSQL is already running on port 55432, it is used as is. `--port` picks another port.

## Reproduce the hand-ins

The database for the tests: `python scripts/start_local.py --db-only` prints the DSN. Then:

```bash
export SETUHAUL_TEST_DSN="host=localhost port=55432 user=postgres"
```

| Hand-in | Command | Expected |
|---|---|---|
| 1. Data audit (SQLite only, no PostgreSQL) | `python scripts/frame/run_audit.py` | Regenerates `docs/1-frame/audit_results.md`: 30 checks, each with its SQL and rows |
| 4. Scenario, supplied snapshot | `python scripts/snapshot_scenario.py` | `RESULT: PASS (6/6)`; SHP1015 escalates `NO_FEASIBLE_SLOT` |
| 4. Scenario, six items | `python -m pytest -q tests/test_scenario_run.py tests/test_snapshot_scenario.py` | passed |
| 4. Brief measures | `python scripts/scenario_measures.py` | Table computed from the evidence files |
| 5. Integrity proof | `python scripts/integrity_proof.py 10` | `RESULT: PASS (9/9)`: 1 claim, 9 LOST, 0 double bookings |
| 5. Integrity test, 50 drivers | `python -m pytest -q tests/test_api.py::test_fifty_drivers_race_for_one_slot` | passed |
| + Scheduling comparison | `python scripts/scheduling_compare.py` | FCFS vs EARLIEST_ETA vs FACILITY_SCORE tables |
| Full suite (about 20 minutes) | `python -m pytest tests -q` | See `docs/challenge/TEST_REPORT.md` |

Without `SETUHAUL_TEST_DSN` only the database-free tests run; the rest are skipped. Configuration names are in
`.env.example` (placeholders only).

## Layout

| Path | Contents |
|---|---|
| `setuhaul/` | The application package. `domain/` holds ETA fusion, feasibility, the scheduling planner and the per-truck optimizer. `agent/` holds the orchestrator, readers and reply templates. `api/` holds the API, booking service, inbound queue and staff console. `workflow/` holds the approvals. `notify/` holds outbound and Telegram. `observability/` holds tracing and redaction. `aws/` holds the Lambda entry points |
| `migrations/` | PostgreSQL migrations 0001–0050. Database functions own booking, approval and lifecycle state |
| `tests/` | The test suite (about 1,100 tests) |
| `scripts/` | `start_local.py`, `integrity_proof.py`, `snapshot_scenario.py`, `scenario_measures.py`, `scheduling_compare.py`; Frame audit, prototype and spike (`frame/`); design validator (`design/`); AWS operations scripts |
| `data/` | The supplied data package: SQLite snapshot, schema and seed SQL, data dictionary, ER diagram, guide |
| `docs/brief/` | The client brief (PDF) and data package as received |
| `docs/challenge/` | **The hand-in index**, scenario run, integrity proof, client readout, break report and evidence |
| `docs/1-frame/` | Problem definition, data audit, variables, ETA signals, human-in-the-loop, evaluation plan |
| `docs/2-design/` | Decision register, architecture, data model, tool contracts and states, approval and outbound designs |
| `docs/3-build/` | Build log, scheduling tool |
| `evals/`, `docs/evaluation/` | Golden and held-out extraction sets, LLM-judge evaluation |
| `demo/` | Local demo console |
| `infra/`, `docs/release/`, `docs/operations/` | AWS pilot (CDK), release gates, runbooks, costs (beyond the brief) |
| `artifacts/fde-demo/`, `presentation/` | Demo deck, diagrams, presenter material |
