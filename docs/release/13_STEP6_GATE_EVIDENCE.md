# 13 — Step 6: release-gate evidence from the deployed pilot (22–23 Nov 2026)

> **Current result (23 Nov 2026, after the owner-approved drills and deployments):**
>
> | Gate | Result |
> |---|---|
> | BOOKING_WORKFLOW | **PASS** |
> | AURORA | **PASS WITH PILOT EXCEPTION (T33-P)** |
> | AWS_QUEUE | **PASS** (DLQ-depth alarm proven live) |
> | RECOVERY_WORKER | **PASS** (forced lease loss recovered live) |
> | WORKLOAD_IDENTITY | PARTIAL: all 5 acceptance checks pass live; only drift detection (AWS Config) is missing on the Free plan |
> | OUTBOUND_DELIVERY | BLOCKED: Meta test number and templates |
>
> Booking (Option D) stays **OFF**. No `deployment_evidence` row has been written: the pipeline step that writes them is not built.

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
| **AWS_QUEUE** | **PASS** (23 Nov) | <ul><li>FIFO queue, content-based deduplication off (the app sets the id).</li><li>FIFO DLQ: maxReceiveCount 5, retention 14 days against 4 for the queue.</li><li>Visibility 300 s, at least twice the 120 s lease.</li><li>Least privilege: only the API and recovery functions send, only the agent receives, no `sqs:*`.</li><li>Redrive procedure tested live (`evidence/drill_dlq_summary.json`).</li><li>**DLQ-depth alarm fired** on a held DLQ message and returned to OK (`evidence/drill_dlq_alarm.json`).</li><li>The oldest-message alarm fired.</li></ul> | none |
| **RECOVERY_WORKER** | **PASS** (23 Nov) | <ul><li>`rate(1 minute)`; 201 real scheduled runs in 3 days.</li><li>DLQ handling procedure drilled live.</li><li>**Forced lease loss live:** the expired worker's renew and finish were refused (`TURN_FENCED`); the deployed recovery worker re-drove the turn; one reply (`evidence/drill_lease_loss.json`).</li></ul> | Operating note, not a missing proof: the schedules are DISABLED while the pilot sleeps (cost control); they are re-enabled on wake. |
| **WORKLOAD_IDENTITY** | PARTIAL | Acceptance checks 1 to 4 (`DEPLOYMENT_GATES.md` §"Workload identity acceptance"):<ul><li>Each of the 6 logins belongs to exactly one application role.</li><li>Only the console login can use `setuhaul_console`.</li><li>No login can use `setuhaul_rpc`, the database owner, superuser or BYPASSRLS.</li><li>In a real Data API transaction, a second `begin_staff_context` was refused with *"already set in this transaction"*. That shows the staff identity survives from one statement to the next.</li><li>Each function's IAM role can read exactly the database secrets of its configured roles, and no others.</li></ul> | <ul><li>Check 5 **passed live (23 Nov)**: see the finalisation section.</li><li>Live drift detection (AWS Config) is not deployed on the Free plan; the IAM policy hash is recorded instead. Adding it is a cost decision.</li></ul> |
| **AURORA** | **PASS WITH PILOT EXCEPTION (T33-P)**: not an unconditional pass | <ul><li>`aurora-postgresql 17.9`.</li><li>24 of 24 migrations recorded, every hash equal to the repository.</li><li>Both evidence triggers enabled.</li><li>The Data API works behind `Db.tx()`.</li><li>**Verified on 17.9 (23 Nov):**<ul><li>Migration 0025 applied on the live cluster as the non-superuser owner, then rolled back.</li><li>A real move succeeded.</li><li>The refusals held: re-applying a move, an unknown proposal, and the agent role.</li><li>RLS isolated by facility.</li><li>Five forbidden direct writes were refused with error 42501 (`evidence/pg17_migration_0025.json`, 11 of 11 checks).</li><li>The whole suite passed in CI on `postgres:17.9`: run 45091795877, 1,136 passed and 426 in Data API mode (`evidence/ci_pg17.json`).</li></ul></li></ul> | Production still needs 18.3+ (T33). The exception covers only the synthetic Free-plan pilot, and it is never written as a PASS evidence row. |
| **BOOKING_WORKFLOW** | **PASS** (23 Nov) | Live on the deployed state machine (drill 2, 10 of 10):<ul><li>Each request starts one execution, through the relay.</li><li>No automatic approval.</li><li>A planner approves (CONFIRMED) or rejects (claim released).</li><li>A request with no decision expires by the state machine's timeout (`ExpireApproval`, `system:expiry`).</li><li>A duplicate decision is idempotent; a second decision is refused.</li><li>Refusals hold: the agent role, another facility, no staff identity.</li><li>One audit record and one test-sink notice per outcome.</li><li>The `live` alias routes to the published version, whose definition equals the repository's.</li><li>The 3 executions that failed on the defect were redriven safely.</li><li>The failed-execution alarm fired on the real failures.</li></ul> | Two defects were found and fixed on the way, both deployed: the relay's IAM grant, and the task functions requiring the state machine ARN. The requests were made through the production database roles because booking is OFF. |
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

1. **Owner decisions:** T33-P is accepted for the pilot only, so whether it may count for Option D is a separate decision. Also: whether to accept the Free-plan lack of drift detection as a pilot exception or pay for AWS Config. *(The redrive drill, the DLQ alarm and lease-loss drills, and the BOOKING_WORKFLOW evidence are done: 23 Nov.)*
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

**Evidence:** `evidence/drill_dlq_summary.json` (each check cites the run that proved it), plus `drill_dlq_run1–5.log` and `drill_dlq_run2/5.json`.

One tagged message, `STEP6-DLQ-DRILL-8436a4759a40`, was tracked; it is not JSON and holds no business data. The drill ran in five runs: the operator's AWS session expired twice, and the script resumed on the **same** message each time and never sent another.

| Step | Result |
|---|---|
| Before | The queue and the DLQ were empty; the consumer was enabled (batch 1, ReportBatchItemFailures); maxReceiveCount 5; visibility 300 s |
| Dead-lettering | Sent 01:28:14 UTC (`cf6ed815…`). **5 consumer failures**, all `JSONDecodeError` (01:28–01:48), then in the **DLQ**. The DLQ held exactly this message: body tag matched, same id, nothing else |
| Redrive | `StartMessageMoveTask` DLQ → source queue at 01:53:27: **1 message moved** (move task record) |
| Back through the consumer | New SQS id **`0bff4e2f…`** (SQS re-ids a redriven message). Received at 01:53:28, **5 more failures**, then dead-lettered again; the **body tag matched** in the DLQ: nothing lost, nothing processed |
| Clean-up | **Only the tracked message** was deleted from the DLQ, by receipt handle after matching its tag; no purge. Both queues empty afterwards |
| Business records | `inbound_receipts` write counters unchanged (27 inserts / 135 updates before and after); no business code ran |
| Monitoring | The **oldest-message alarm fired** (OK→ALARM at 01:35:18 and 02:00:18). The **DLQ-not-empty alarm did not**: its 5-minute maximum never saw the message, which stayed in the DLQ only seconds to minutes. **DLQ-depth monitoring is not demonstrated** |

**Result:** the redrive procedure works live, and the DLQ handling procedure (inspect, redrive, remove one message) is drilled. **AWS_QUEUE stays PARTIAL** on a single item: the DLQ-depth alarm. To demonstrate it, keep a drill message in the DLQ for 10 minutes or more before redriving it; that needs the owner's approval for another drill.

## Controlled pilot deployment, 23 Nov 2026 (owner-approved: relay permission fix and migration 0025)

**Evidence:** `evidence/deploy_20261123.json`, `evidence/deploy_check.json` and `evidence/pending_workflow_processed.json`.

**Before deploying:**
- CI on `2d7dffc` passed, pinned to PostgreSQL 17.9 (run 45101428259).
- A time-of-day test flake had failed run 45099842565. It was fixed in the test fixture; no product code changed.
- Restore point recorded: 02:48:59 UTC, with point-in-time restore available.
- Migration 0025 reviewed: schema-additive.
- Template diff against the live stack: **only** the relay's `states:StartExecution` resource list changed (the alias, plus the state machine), plus a `BookingMode` output.

**Deploy:** workflow run 45102351722 from `release/fde-sydney-pilot` at `2d7dffc`; the test, synth and deploy jobs all passed.

**After deploying:**
- 29 of 29 deploy checks pass.
- Booking is false on every function, and `BookingMode` is OFF.
- Notifications go to the test sink only.
- Migration 0025 is recorded, with its hash matching.
- The relay's live grant matches the template.

**Processing the pending events** (`scripts/process_pending_workflow.py`):

| Check | Result |
|---|---|
| The relay starts executions | **Yes.** 5 events consumed and 3 executions started; the IAM fix works |
| Decided requests unchanged | Yes: APPROVED and REJECTED keep the same `decided_by` and `decided_at` |
| No request decided twice | Yes |
| A second relay run | Did nothing (0 consumed) |
| The executions themselves | **All 3 FAILED.** The token and expiry task functions crash: `KeyError: 'SETUHAUL_APPROVAL_STATE_MACHINE_ARN'` |
| The overdue OPEN request | **Still OPEN.** Expiry depends on the expiry task |

**Second deployed defect:** `_workflow()` in `setuhaul/aws/lambdas.py` required the state machine ARN in every function. Only the relay has that ARN: the task functions cannot be given it, because the machine references them, which would be a CloudFormation cycle. They also don't need it.
- The IAM denial had hidden this until now.
- The local test set the variable for every handler, so it never caught the crash.
- **Fix, ready locally and not deployed:** the ARN is optional in `_workflow()`, and `start()` refuses with a clear configuration error when it is missing.
- **Regression test:** `test_the_state_machine_task_lambdas_run_without_the_state_machine_arn` fails with `KeyError` on the deployed code and passes with the fix.

**Recovery after the fix is deployed:** redrive the 3 failed executions (Step Functions `RedriveExecution` resumes from the failed `ExpireApproval` step).
- For the two decided requests, `expire_approval` returns their status unchanged.
- The overdue request expires through the designed path.
- No manual cleanup.

Deploying the fix and redriving need the owner's approval.

## Task-function fix, redrive and approval drill 2 (23 Nov 2026, under the owner's standing approval)

**The fix:** code only, `20180e7`. CI passed on `postgres:17.9` (run 45104618495): 1,139 passed and 1 skipped; Data API mode 428 passed. The template diff against the live stack was **code only**: no resource, IAM or output change.

**Deploy:** run 45105667259 from `release/fde-sydney-pilot` at `20180e7`; test, synth and deploy all passed. Afterwards, 29 of 29 deploy checks pass.

**Redrive of the 3 executions that failed on the defect** (`evidence/redrive_20261123.json`, 6 of 6):
- All three were redriven once and SUCCEEDED.
- The APPROVED and REJECTED requests are unchanged (same `decided_by`, `decided_at`, appointment and claim).
- The overdue OPEN request was **EXPIRED by `system:expiry`** through `expire_approval`, and its claim released.
- One decision record per request in the audit trail.
- Driver notices `booking_approved`, `booking_rejected` and `booking_expired` were SENT to the **test sink**.

**Approval drill 2** (`evidence/drill_approval_run2.json`, 10 of 10; three new `SYN-STEP6` requests 5 days ahead):

| Path | Execution states | Database result | Decided by |
|---|---|---|---|
| Request (×3) | started by the deployed relay; a second relay run started none | 3 PENDING; OPEN with a task token (**no automatic approval**) | agent role (request) |
| Approve | NotifyApprovers → WaitForDecision → **Done** | APPROVED; appointment CONFIRMED; claim kept | `staff:step6-drill-planner` (**planner**) |
| Reject | NotifyApprovers → WaitForDecision → **Done** | REJECTED; appointment REJECTED; claim released | same planner |
| Expiry | NotifyApprovers → WaitForDecision → **ExpireApproval** → Done (state machine timeout) | EXPIRED; claim released | `system:expiry` (**system**) |
| Duplicate decision | — | The same key twice gives the same decision; a second decision with a new key is refused (`ALREADY_DECIDED`) | — |
| Refusals | — | Agent role: 42501. GGN planner: `APPOINTMENT_UNKNOWN`. No staff identity: `UNTRUSTED_CONTEXT` | — |
| Notices | — | One per outcome, all to the **test sink** | — |

**Monitoring:** the `ApprovalFailed` (ExecutionsFailed) alarm and the token and expiry functions' error alarms **fired on the real failures** (OK→ALARM about 03:05 UTC) and returned to OK after the fix.

**A first attempt at drill 2** reused run 1's slot (D3, 26 Nov 09:00). That slot still holds run 1's CONFIRMED booking, so the database refused the hold with `ex_one_truck_per_dock`: the no-double-booking constraint working live (`evidence/drill_approval_run2_attempt1.log`). The rerun used free slots.

**Note on how the requests were made:** they were created through the production database roles (agent, ingest, feasibility), because the booking switch is OFF and the agent API refuses booking tools. The workflow, relay, task functions, decisions and expiry are the deployed ones.

## Step 6 finalisation drills (23 Nov 2026, owner's Finalization Authorization)

**DLQ-depth alarm** (`evidence/drill_dlq_alarm.json`, 6 of 6):
- Clean start: both queues empty, `DlqNotEmpty` OK.
- One tagged, non-JSON synthetic message (`STEP6-DLQ-ALARM-…`) failed 5 times at the JSON parse and was dead-lettered.
- It was held in the DLQ until `DlqNotEmpty` went to **ALARM** (04:36 UTC). Then only that message was removed, by receipt handle after matching its tag (no purge).
- The alarm returned to **OK**, with both queues empty. Alarm history: 10:05 UTC OK to ALARM; 10:13 UTC ALARM to OK.

**Forced lease loss** (`evidence/drill_lease_loss.json`, 11 of 11):
- A synthetic driver message was stored through the ingest role.
- A first worker claimed a 20-second lease and was then "crashed".
- After the lease expired, its renew and finish were refused, and its write was fenced (`TURN_FENCED`).
- The **deployed recovery worker** (`setuhaul-pilot-recovery`) re-drove the turn. The agent took it over and sent **exactly one** reply (to the test sink).

**Second staff identity (WORKLOAD_IDENTITY check 5)** (`evidence/identity_check5.json`):
- Cognito user `pilot-operations` was created in the `operations` and `facility-FAC-JAI-01` groups. The temporary password goes by Cognito e-mail to the owner; the operator never sees it.
- The deployed pre-token trigger, invoked with hostile client metadata asking for `ops_lead`, added only `facility_scope` and **did not add or change any role group**.
- A read-only `GET /console/me` shows the roles and permissions the server derived from the token. The console shows them, and the Docks tab has a read-only schedule-proposal view that an operations user is refused (`tests/test_console_me.py`).
- **Live sign-in (05:30 UTC, by the owner; Claude never saw the password or MFA code):** `pilot-operations` went to CONFIRMED (pool MFA ON, TOTP only). `GET /console/me` returned 200 and the console showed *server roles: operations*. `GET …/FAC-JAI-01/schedule-proposal` and `…/FAC-GGN-01/schedule-proposal` were both **refused, 403**. **Check 5 PASS.**
