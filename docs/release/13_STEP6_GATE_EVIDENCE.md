# 13 — Step 6: release-gate evidence from the deployed pilot (22 Nov 2026, 18:47 UTC)

**What this is:** a read-only check of the six gates in `docs/operations/DEPLOYMENT_GATES.md`, run against the deployed synthetic pilot.
- **Stack:** `SetuHaulPilot`, deployed from `eaad598`. The code running is identical to `release/fde-sydney-pilot` at `9d2145c`.
- **Script and raw output:** `scripts/step6_gate_evidence.py` produced `docs/release/evidence/step6_gates.json`, with account ids masked.

**What the run did not touch:**
- It sent no message and started no workflow execution.
- It changed no schedule. The only database transactions it opened were rolled back.
- It wrote **no** `deployment_evidence` row, because no gate passed. `setuhaul/ops/gate_evidence.py` refuses anything that isn't PASS, and `tests/test_gate_evidence.py` shows it refuses this exact report.

**Status meanings:**
- **PASS:** every requirement was shown live.
- **PARTIAL:** everything that could be checked live passed; the rest is named.
- **FAIL:** a requirement is contradicted live.
- **BLOCKED:** cannot be shown without an outside dependency or an owner approval.

| Gate | Status | Shown live | Not shown / blocker |
|---|---|---|---|
| **AWS_QUEUE** | PARTIAL | <ul><li>FIFO queue, with content-based deduplication off (the app sets the deduplication id).</li><li>A FIFO dead-letter queue, with `maxReceiveCount` 5 and retention of 14 days, against 4 days for the queue.</li><li>Visibility timeout 300 s, at least twice the 120 s lease.</li><li>Only the API and recovery functions can send, and only the agent function can receive; no `sqs:*` permission.</li><li>27 messages sent and 27 deleted in the last 48 h; the dead-letter queue has received nothing and is empty.</li></ul> | A redrive drill (dead-letter queue → queue). It writes to the live queue, so it needs the owner's approval. |
| **RECOVERY_WORKER** | PARTIAL | <ul><li>The rule `setuhaul-pilot-recovery-every-minute` is `rate(1 minute)`.</li><li>201 real scheduled runs in the last 3 days; the last at 18:22 UTC on 22 Nov.</li></ul> | <ul><li>Schedules are DISABLED while the pilot sleeps, a cost control.</li><li>Lease renewal and generation fencing were not forced live (that needs a deliberately stuck message on the real queue). They are proven locally in `tests/test_inbound_delivery.py`.</li><li>The dead-letter procedure has not been drilled.</li></ul> |
| **WORKLOAD_IDENTITY** | PARTIAL | Acceptance checks 1 to 4 (`DEPLOYMENT_GATES.md` §"Workload identity acceptance"):<ul><li>Each of the 6 logins belongs to exactly one application role.</li><li>Only the console login can use `setuhaul_console`.</li><li>No login can use `setuhaul_rpc`, the database owner, superuser or BYPASSRLS.</li><li>In a real Data API transaction, a second `begin_staff_context` was refused with *"already set in this transaction"*. That shows the staff identity survives from one statement to the next.</li><li>Each function's IAM role can read exactly the database secrets of its configured roles, and no others.</li></ul> | <ul><li>Check 5 (an ordinary operations user cannot create an ops_lead context) needs a second Cognito user, which the owner creates. It is proven locally.</li><li>Live drift detection (AWS Config) is not deployed on the Free plan. The IAM policy hash is recorded instead.</li></ul> |
| **AURORA** | **FAIL (version only)** | <ul><li>`aurora-postgresql 17.9`.</li><li>24 of 24 migrations recorded, every hash equal to the repository.</li><li>Both evidence triggers enabled.</li><li>22 tables with forced row-level security.</li><li>The Data API works behind `Db.tx()`.</li></ul> | T33 asks for **18.3+**; see the next section. The RLS and allow-list test files have not been run against Aurora itself; the live end-to-end run did go through RLS. |
| **BOOKING_WORKFLOW** | BLOCKED | <ul><li>The `live` alias routes 100% to published version 1.</li><li>The published definition equals the repository ASL, once the three function ARNs are substituted.</li><li>No execution has ever started.</li><li>Booking is false on every function.</li></ul> | Request, approve, reject and expiry against the real state machine need booking ON (Option D), which needs the owner's separate approval. They are proven locally. |
| **OUTBOUND_DELIVERY** | BLOCKED | The internal test sink only. Delivery and refusal were shown in the Option A run (`evidence/option_a.json`, A3 and A4). | A **Meta WhatsApp Cloud API test number**, **Meta-approved templates** (en, hi; Hinglish is its own gate) and a real, permitted test delivery. Real WhatsApp stays disabled until the owner provides these. |

## The database version (T33): evidence and recommendation

- **T33 (Design):** "Aurora PostgreSQL 18 (minimum 18.3, the first 18.x the RDS Data API supports)". The reason given was the *same major version as every local test*, the longest support window and no downgrade. The challenge brief itself names no PostgreSQL version: it supplies a SQLite snapshot.
- **Live:** the account is on the AWS Free plan, where Aurora is available only as the express configuration (`docs/release/10_EXPRESS_AURORA.md`). That configuration runs **17.9**, and the probe found no 18.x option. Getting 18.x would need a plan upgrade, which is a paid change that is not authorised.
- **Compatibility evidence:**
  - All 24 migrations applied on 17.9 with matching hashes.
  - The Option A end-to-end run passed on 17.9: S1, A1–A6 and D1.
  - CI runs the whole suite on **PostgreSQL 17** (`postgres:17` services in `.github/workflows/ci.yml`, run 45039036387: 1070 passed). Local runs use **PostgreSQL 18.6**.

  So the same code is tested on both majors, which answers T33's main reason. Two things were fixed for Aurora specifics rather than for the version: a non-superuser owner, and the Data API not returning range types.
- **Recommendation:** keep T33 unchanged for production. Record a **pilot-only exception, T33-P**: "synthetic pilot on Aurora 17.9 (express configuration) while CI covers PostgreSQL 17 and 18". It would be valid until either the account leaves the Free plan or booking (Option D) is turned on, whichever comes first.

  The AURORA gate stays **FAIL** until the owner either accepts T33-P or approves a plan change. Nothing here changes T33 or marks the gate passed.

## What it would take to turn booking on (Option D), in order

1. **Owner decisions:** T33-P (or an 18.x cluster); approval of the redrive drill; a second Cognito user in the operations group for identity check 5.
2. **Outside dependency:** a Meta test number and approved templates, for OUTBOUND_DELIVERY.
3. **BOOKING_WORKFLOW evidence while booking is still OFF.** Run the deployed state machine directly with synthetic approval records: request, approve, reject and expiry. This starts executions on the live pilot, so it needs the owner's approval.

   Booking cannot come first. With the switch on, the workload functions refuse to start until all six gates already have evidence. So the workflow must be proven on its own.
4. **Re-run** `scripts/step6_gate_evidence.py` until every gate is PASS. Then **write the evidence rows** with `setuhaul/ops/gate_evidence.py`, from the pipeline's deploy-evidence identity and for PASS gates only. The pipeline step that runs it with that login is **not built yet**; the CLI refuses `--write`.
5. **A deployment with the booking switch on**, carrying the owner's written approval reference: `-c booking_approval=OWNER-APPROVAL-YYYYMMDD-<ref>`.
   - Without it, synthesis keeps booking OFF; CI never passes it (`tests/test_infra.py`).
   - Even with it, the workload functions refuse to start unless all six gates have current evidence for that exact deployment (`check_booking_preconditions`).
