# Renewal evidence schema

## 1. Renewal report (`docs/release/evidence/renewal_gates.json`)

Written by `scripts/step6_gate_evidence.py --mode renewal`. Account ids masked.

| Field | Meaning |
|---|---|
| `mode` | `"renewal"` (an activation report has no `mode`, `booking_enabled: false`) |
| `collected_utc` | collection time; the writer refuses a report older than 24 h |
| `stack`, `region`, `environment` | `SetuHaulPilot`, `ap-southeast-2`, `pilot-ap-southeast-2` |
| `booking_enabled` | `true` (the pilot is active) |
| `release_sha` | the commit the pilot runs |
| `approval_ref` | the owner approval this renewal is made under; must equal the activation run's |
| `artifact_sha256` | the deployed api/agent code hash; must equal the deployment manifest's artifact |
| `expected_kill_switch` | the state the gate required |
| `approved_exceptions` | the three pilot exceptions, by code |
| `gates.<GATE>.status` | the activation-rule status, unchanged (the exception conditions read it) |
| `gates.<GATE>.renewal_status` | PASS / PASS WITH APPROVED EXCEPTION / PARTIAL WITH APPROVED EXCEPTION / BLOCKED WITH APPROVED EXCEPTION / FAIL / UNRESOLVED |
| `gates.BOOKING_WORKFLOW` | `mode: renewal`, `status`, `checks`, `diagnostics`, `not_shown` (= diagnostics unless PASS), `evidence` below |

`gates.BOOKING_WORKFLOW.evidence`:

| Field | Meaning |
|---|---|
| `booking_configuration` | per function: booking / env / data class; booking-on set; kill switch; /health; deployed and renewal approval refs |
| `approval_executions` | one entry per execution: id, short id, status, start, category (A/B/C/null), verdict, reasons, the verified record (ids, states, times) |
| `attribution_summary` | short ids per category, unresolved, failed |
| `fresh_smoke` | smoke id, release SHA, environment, time, code hash, pass, its executions |
| `activation_facts` | state machine, alias version, live and repository definition hashes |
| `executions_total` | how many executions were judged |
| `record_access_blocker` | present only when 0029 could not be read |

## 2. Smoke record: see RENEWAL_SMOKE_TEST.md §2

## 3. Owner attribution record (`docs/release/evidence/owner_attribution_20261123.json`)

`category: "C"`, `owner_confirmation {confirmed_by, confirmed_at_utc, channel, statement}`, `environment`, and per
execution: `approval_id`, `execution_arn`, `label`, `step_functions {input, started_utc, …, execution_status,
stopped_utc}`, `cloudwatch {log_group, requests[{t_utc, request_id, line}], http_outcome}`, `expected_record
{appointment_id, approval_status[], decided_by_type[], decider_kind, decided_between_utc, appointment_not_in[]}`.

## 4. Database rows (unchanged tables)

Evidence is written exactly as for activation:
* `deployment_manifests`: one per deployment.
* `deployment_evidence`: one row per PASS gate, append-only (trigger 0013), freshness and validity enforced
  (trigger 0014: 7 days for AWS_QUEUE, RECOVERY_WORKER, OUTBOUND_DELIVERY, BOOKING_WORKFLOW; 30 days for the others). It
  carries `artifact_uri` (the committed report at the run's commit), `evidence_sha256` (of the report bytes) and
  `config_fingerprint` (live).
* `deployment_exceptions`: the three owner-approved exceptions, `approval_ref`, reason, 7 days.

Traceability: the row's `evidence_sha256` → the committed `renewal_gates.json` → its `fresh_smoke.smoke_id` → the
committed smoke file → its execution ids → `approval_executions` entries → the 0029 records and Step Functions history.

## 5. Rules

* Fresh for the current release: report ≤ 24 h, smoke ≤ 24 h, smoke on the target release and the deployed code.
* Never copied from a previous release: the report's `artifact_sha256` must be this deployment's.
* Never marked passed by hand: only the collector writes `status`, and the writer re-checks the bindings.
* Exceptions explicit and scoped: only the three, only from their own activation status, 7 days, pilot only.
* Failed checks stay visible: every check and diagnostic is kept in the report.
