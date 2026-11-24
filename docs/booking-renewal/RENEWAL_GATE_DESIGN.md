# Booking evidence renewal gate: design

Status: implemented and tested locally. **Not deployed; no workflow run.** Code: `setuhaul/ops/renewal.py`,
`scripts/step6_gate_evidence.py --mode renewal`, `setuhaul/ops/gate_evidence.py`, `scripts/smoke_option_d.py`,
`migrations/0029_approval_execution_evidence.sql`, `.github/workflows/activate-booking.yml` (`mode`).

## 1. Two modes, one gate

```mermaid
flowchart TB
    subgraph A[Mode A: activation (unchanged)]
      A1[pilot booking OFF] --> A2[collector --mode activation<br/>BOOKING_WORKFLOW: drills only, switch false everywhere]
      A2 --> A3[step6_gates.json<br/>booking_enabled false]
      A3 --> A4[activate-booking mode=activation<br/>rows for PASS gates + 3 exceptions]
    end
    subgraph B[Mode B: renewal (new)]
      B1[pilot booking ON] --> B2[smoke_option_d.py --release-sha S<br/>new release-bound record]
      B2 --> B3[collector --mode renewal --approval-ref R --release-sha S --smoke F]
      B3 --> B4[renewal_gates.json<br/>booking_enabled true, artifact hash, release, approval]
      B4 --> B5[activate-booking mode=renewal<br/>gate_evidence re-checks every binding]
      B5 -->|all bindings hold| B6[rows for PASS gates + 3 exceptions<br/>booking stays ON]
      B5 -->|any binding fails| B7[nothing written<br/>booking state unchanged; evidence expires on its own date]
    end
```

Renewal is not "activation with checks skipped". The two activation-only checks are **replaced** by stricter
renewal criteria, and the gate has its own tests (`tests/test_renewal_gate.py`).

## 2. Renewal acceptance criteria (BOOKING_WORKFLOW)

All must hold for PASS:

1. **Configuration (read from the deployed functions, not names):** booking `true` on exactly `setuhaul-pilot-api` and
   `setuhaul-pilot-agent`; every other function not `true` (unset or `false`); api and agent `SETUHAUL_ENV=pilot`,
   `SETUHAUL_DATA_CLASS=synthetic`; no function non-synthetic.
2. **/health:** `status ok`, `database ok`, `booking_enabled true`, environment `pilot-ap-southeast-2`, kill switch in
   the expected state (`disengaged` by default; `--expected-kill-switch ENGAGED` only if the owner left it engaged).
3. **Owner approval:** the renewal names a well-formed `OWNER-APPROVAL-YYYYMMDD-<ref>`. The deployed stack's
   `BookingMode` names an owner approval. When evidence is written, the run's approval must equal the report's.
4. **Carried activation facts:** live alias 100% to one version; published definition = repository ASL; no failed execution.
5. **Attribution:** every execution of the approval state machine (paginated, not the first 100) is attributed to
   exactly one of A, B, C. See APPROVAL_EXECUTION_ATTRIBUTION.md.
6. **Fresh smoke:** a renewal-format smoke record, all 11 checks present and true, this environment, `release_sha` =
   the target release, `deployed_code_sha256` = the deployed api/agent code hash, at most 24 h old. Its own executions
   are attributed to it (B).
7. **Owner attribution record** valid and limited to the two confirmed executions.

Statuses: **FAIL** if anything contradicts. **UNRESOLVED** if nothing contradicts but an execution has no evidence or
no visible record. **PASS** otherwise. Every non-PASS carries diagnostics such as
`BOOKING_WORKFLOW: UNRESOLVED — approval execution deadbeef has no attributable evidence: …`.

Other gates keep their activation `status` unchanged (so the exception rules keep their exact scope). The report adds a
`renewal_status`: PASS / PASS WITH APPROVED EXCEPTION (AURORA T33-P) / PARTIAL WITH APPROVED EXCEPTION
(WORKLOAD_IDENTITY, drift detection only) / BLOCKED WITH APPROVED EXCEPTION (OUTBOUND_DELIVERY, test sink) / FAIL.
An exception maps only from its own gate and its own activation status with every check passing.

## 3. Writing evidence (`gate_evidence`)

A booking-ON report is refused unless `mode == renewal` and:
* `booking_enabled true`, environment `pilot-ap-southeast-2`;
* collected ≤ 24 h ago;
* `artifact_sha256` = this deployment's manifest artifact (the report was collected on the very code being recorded);
* BOOKING_WORKFLOW is `PASS` in renewal mode;
* the fresh smoke's release SHA = the report's release SHA;
* the run's `--activate` approval = the report's approval.

Then the existing rules apply unchanged: only exact PASS gates become `deployment_evidence` rows, and the three exceptions
go to `deployment_exceptions` under their existing conditions. Rows stay append-only, with database-enforced validity
(7 days operational, 30 days otherwise). Nothing is ever marked passed by hand.

## 4. Weekly renewal (due by 30 Nov 2026) and release-specific renewal

Same implementation, same command sequence (RENEWAL_SMOKE_TEST.md §3):
1. `python scripts/smoke_option_d.py --release-sha <running commit>`. Synthetic. It engages and disengages the kill
   switch as in the 23 Nov smoke, so it must run with the owner present.
2. `python scripts/step6_gate_evidence.py --mode renewal --approval-ref <ref> --release-sha <running commit> --smoke <file>`.
3. If BOOKING_WORKFLOW is PASS: commit the two files and run `activate-booking` with `mode=renewal`. Because the code
   is unchanged the artifact matches, the evidence is re-recorded, and booking stays ON. Docs-only commits don't change
   the Lambda zip (built from `setuhaul/` deterministically).
4. If not PASS: nothing is recorded. Booking is not switched off by renewal. The existing evidence keeps its own expiry,
   after which the functions refuse to start (the existing, owner-approved fail-closed rule).

## 5. Failure and recovery

| Failure | Effect | Recovery |
|---|---|---|
| execution UNRESOLVED | gate UNRESOLVED, nothing written | attribute it with evidence (a smoke/drill record), or an owner decision + code change for C |
| record contradicts evidence | gate FAIL | investigate; never edit the record |
| 0029 not deployed / no scope | every execution UNRESOLVED, blocker reported | deploy 0029 (additive) |
| stale report / smoke, wrong SHA or code | refused at write | re-run smoke and collector on the running code |
| approval mismatch | refused at write | run with the approval the report names |

## 6. Open decision: the first renewal of a NEW release (not implemented)

The smoke drives the deployed agent with booking ON, so release-bound smoke evidence needs the new code running with
booking ON. That in turn needs evidence for the new deployment. Today the renewal path refuses a new release (the
artifact doesn't match). That's safe: nothing is recorded, and if deployed anyway the api and agent refuse to start.
Options for the owner:

| Option | How | Trade-off |
|---|---|---|
| **P: provisional window** (recommended) | A new release is recorded with **short** validity (e.g. 2 h), and only when every other renewal criterion passed on the previous release and the release diff passed CI. The smoke then runs on the new code and the full renewal must complete inside the window, or the evidence lapses and the functions refuse to start (fail closed). | Booking runs on new code for ≤ 2 h before its own smoke. Needs a migration for short validity rows and a reviewed workflow change. |
| **O: booking-OFF deploy, then re-activate** | `ci.yml` deploys the new code with booking OFF; the activation-mode gate is re-collected; `activate-booking mode=activation`. | The activation gate's "every execution is a Step 6 drill" can never pass again on this state machine (the B/C executions exist for ~90 days), so this needs that check changed too. Booking OFF for a while. |
| **S: staging stack** | Run the smoke on a second synthetic stack with the new code. | Extra AWS cost (paid service increase: owner approval). |

Phase G.1 cannot be deployed with booking ON under the owner's policy until one of these is chosen and built.
