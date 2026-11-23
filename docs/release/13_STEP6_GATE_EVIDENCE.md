# 13 — Step 6: release-gate evidence from the deployed pilot (22 Nov 2026; AURORA updated 23 Nov under T33-P)

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
| **AURORA** | **PASS WITH PILOT EXCEPTION (T33-P)**: not an unconditional pass | <ul><li>`aurora-postgresql 17.9`.</li><li>24 of 24 migrations recorded, every hash equal to the repository.</li><li>Both evidence triggers enabled.</li><li>The Data API works behind `Db.tx()`.</li><li>**Verified on 17.9 (23 Nov):**<ul><li>Migration 0025 applied on the live cluster as the non-superuser owner, then rolled back.</li><li>A real move succeeded.</li><li>The refusals held: re-applying a move, an unknown proposal, and the agent role.</li><li>RLS isolated by facility.</li><li>Five forbidden direct writes were refused with error 42501 (`evidence/pg17_migration_0025.json`, 11 of 11 checks).</li><li>The whole suite passed in CI on `postgres:17.9`: run 45091795877, 1,136 passed and 426 in Data API mode (`evidence/ci_pg17.json`).</li></ul></li></ul> | Production still needs 18.3+ (T33). The exception covers only the synthetic Free-plan pilot, and it is never written as a PASS evidence row. |
| **BOOKING_WORKFLOW** | **FAIL (deployed IAM defect, found 23 Nov)** | Approved drill on synthetic requests (see the next section):<ul><li>No automatic approval: all three requests were PENDING.</li><li>The planner's decisions recorded correctly: APPROVED with the appointment CONFIRMED; REJECTED with the claim released.</li><li>Refusals held: the agent role (42501), another facility's planner, and a call with no staff identity.</li><li>Idempotent: the same decision key twice gave the same answer, and a second decision was refused.</li><li>The `live` alias routes to the published version, whose definition equals the repository's.</li></ul> | **The deployed relay cannot start any execution:** `AccessDeniedException ... states:StartExecution on resource ...:stateMachine:setuhaul-pilot-approval`. The role was granted the alias ARN only, and IAM also evaluates the state machine's own ARN. The fix is in `infra/setuhaul_stack.py` and is tested (`test_the_relay_may_start_executions_through_the_alias` fails on the old grant), but it is **not deployed**: that needs the owner's approval. After it is deployed, rerun the drill for the workflow-driven paths: wake-up after a decision, and expiry by timeout. |
| **OUTBOUND_DELIVERY** | BLOCKED | The internal test sink only. Delivery and refusal were shown in the Option A run (`evidence/option_a.json`, A3 and A4). | A **Meta WhatsApp Cloud API test number**, **Meta-approved templates** (en, hi; Hinglish is its own gate) and a real, permitted test delivery. Real WhatsApp stays disabled until the owner provides these. |

## The database version: T33 and T33-P (owner approval, 23 Nov 2026)

- **T33 is unchanged:** production requires PostgreSQL **18.3+**.
- **T33-P (`docs/2-design/01_decision_register.md`):** PostgreSQL **17.9** is permitted **exclusively for the synthetic AWS pilot on the AWS Free Plan**.
- **Justification:**
  - the Free Plan offers Aurora PostgreSQL only as the express configuration, whose engine is 17.9;
  - the compatibility evidence: the whole suite runs in CI on PostgreSQL 17 and locally on 18.
- **The verification required before the gate could be marked:**

| Verification | Result | Evidence |
|---|---|---|
| Migration 0025 on the live Aurora 17.9 cluster, as the non-superuser owner the deploy migrator uses | Applied, in one transaction, then rolled back | `evidence/pg17_migration_0025.json` |
| Catalogue: both proposal tables have forced RLS; both functions are SECURITY DEFINER owned by `setuhaul_rpc`; only `setuhaul_console` can execute `move_appointment` | 3 of 3 | same |
| Functional: a stored proposal and a real move on synthetic rows (old appointment CANCELLED, one current CONFIRMED, one active claim); re-apply refused with `ALREADY_APPLIED`; unknown proposal refused with `PROPOSAL_UNKNOWN`; agent role refused with 42501 | All passed | same |
| RLS live: a Jaipur shipment is visible in the Jaipur scope and not in the Gurugram scope | `{JAI: 1, GGN: 0}` | same |
| Role allow-list live: the agent inserts an appointment or writes evidence; the console updates a claim or writes a proposal row; the system calls `move_appointment` | All 5 refused with 42501 | same |
| Nothing persisted | Still 24 migrations; no 0025 objects; no probe rows | same |
| The whole suite on exactly 17.9 | CI run 45091795877 on `postgres:17.9`, both servers `17.9 (Debian 17.9-1.pgdg13+1)`: 1,136 passed and 1 skipped (by design); Data API mode 426 passed | `evidence/ci_pg17.json` |

- **Status:** the AURORA gate is reported as **PASS WITH PILOT EXCEPTION (T33-P)** by `scripts/step6_gate_evidence.py`, and never as an unconditional PASS.
  - `setuhaul/ops/gate_evidence.py` writes a row only for an exact `PASS`. This status is therefore not written as `deployment_evidence`, and it cannot open the booking switch (`tests/test_gate_evidence.py::test_a_pilot_exception_is_never_written_as_gate_evidence`).
  - Whether it may count towards Option D on the pilot is part of the owner's separate written activation decision.
- **Migration 0025 is still not deployed to the pilot.** Deploying it is a separate step that needs the owner's approval.

## What it would take to turn booking on (Option D), in order

1. **Owner decisions:** T33-P (or an 18.x cluster); approval of the redrive drill; a second Cognito user in the operations group for identity check 5.
2. **Outside dependency:** a Meta test number and approved templates, for OUTBOUND_DELIVERY.
3. **BOOKING_WORKFLOW evidence while booking is still OFF.** Run the deployed state machine directly with synthetic approval records: request, approve, reject and expiry. This starts executions on the live pilot, so it needs the owner's approval.

   Booking cannot come first. With the switch on, the workload functions refuse to start until all six gates already have evidence. So the workflow must be proven on its own.
4. **Re-run** `scripts/step6_gate_evidence.py` until every gate is PASS. Then **write the evidence rows** with `setuhaul/ops/gate_evidence.py`, from the pipeline's deploy-evidence identity and for PASS gates only. The pipeline step that runs it with that login is **not built yet**; the CLI refuses `--write`.
5. **A deployment with the booking switch on**, carrying the owner's written approval reference: `-c booking_approval=OWNER-APPROVAL-YYYYMMDD-<ref>`.
   - Without it, synthesis keeps booking OFF; CI never passes it (`tests/test_infra.py`).
   - Even with it, the workload functions refuse to start unless all six gates have current evidence for that exact deployment (`check_booking_preconditions`).

## Live drills (owner-approved 23 Nov 2026; synthetic data; booking OFF throughout)

### Approval workflow: `scripts/drill_approval_workflow.py`

**Evidence:** `evidence/drill_approval_run1.log` and `evidence/drill_approval_run1.json`.

Three synthetic requests were created:
- IDs `SYN-STEP6-SHP-4AB620D0`, `…ECCDE8C2` and `…9DEE8575`, on dock D3 on 26 Nov at 09:00, 10:00 and 11:00 IST;
- all `+999` phone numbers;
- each made by the role that makes it in production: ingest (thread, driver reply, attestation), feasibility (option), agent (read-back, hold, submit).

| Path | Who | Database result | Workflow result |
|---|---|---|---|
| Request (×3) | agent role | 3 PENDING requests: none automatically approved; claims held | **Not started:** the relay's `StartExecution` was denied (IAM defect above). **0 executions** |
| Approve | `staff:step6-drill-planner`, role **planner** | Approval **APPROVED**, appointment **CONFIRMED**, claim kept; audited `APPROVAL_APPROVED` | No execution to wake; the `ApprovalDecided` event is waiting for the relay |
| Reject | the same planner | Approval **REJECTED**, appointment **REJECTED**, claim **released**; audited `APPROVAL_REJECTED` | Same as approve |
| Expiry | nobody | Still **OPEN** past its deadline, claim held, because expiry is driven by the workflow's timeout | **Not exercised:** no execution |
| Refusals | agent role; a GGN planner; console with no staff identity | 42501; `APPOINTMENT_UNKNOWN`; refused | — |
| Idempotency | the same planner | The same decision key twice gave the same response; a second decision with a new key was refused (`ALREADY_DECIDED`) | — |

**Not completed:** the approval-workflow requirement in `DEPLOYMENT_GATES.md` is not met.

**What the drill left on the pilot** (all synthetic):
- one CONFIRMED synthetic appointment;
- one rejected request;
- one OPEN request past its deadline;
- two `ApprovalDecided` events PENDING in the outbox.

Nothing was cleaned up (no destructive cleanup). Once the IAM fix is deployed, the relay will start the open request's execution with a deadline already passed, so it will expire immediately through the normal path, and the two decided requests' executions will finish at once.

### Dead-letter queue: `scripts/drill_dlq.py`

See `evidence/drill_dlq_run2.json` and the runs' logs. One tagged message, `STEP6-DLQ-DRILL-8436a4759a40`, is not JSON and touches no business data:
- It was sent at 01:28:14 UTC (SQS id `cf6ed815…`).
- It failed **5 times** at the JSON parse (`JSONDecodeError`, 01:28–01:48), then was **dead-lettered**. The DLQ held exactly this message (tag matched, same id).
- **Redrive** (`StartMessageMoveTask`, DLQ → source queue) at 01:53:27: **1 message moved**.
- The redriven message got a **new SQS id, `0bff4e2f…`**, and reached the consumer again at 01:53:28. That one-second gap, its error type and the queue's send metrics (only 2 sends in the window: the original and the move) show it is the same message.
- `inbound_receipts` write counters unchanged: **no business record touched**.

**Not completed yet:** the second dead-lettering, the body-tag check in the DLQ, and removing that single message. The operator's AWS session expired twice during the drill; the message keeps retrying on its own meanwhile.
