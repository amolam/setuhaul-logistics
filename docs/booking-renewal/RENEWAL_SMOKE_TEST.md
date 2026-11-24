# Renewal smoke test (`scripts/smoke_option_d.py`)

The established Option D smoke test, unchanged in what it does, now bound to a release. **Not run in this PR**: it
writes to the live pilot (a synthetic booking, a kill-switch drill), and this task must not change the pilot.

## 1. What it proves (unchanged, 11 checks)

1. /health: booking ON, kill switch disengaged, the three exceptions listed.
2. a. A synthetic driver (SYN- ids, +999 number) reports a delay, picks option 1, says YES; the deployed agent holds the
   slot and submits a request (PENDING, approval OPEN, no automatic approval).
   b. The relay starts exactly one execution, waiting for a person.
   c. A planner (drill identity `staff:option-d-smoke-planner`, FAC-JAI-01) approves: CONFIRMED, decided by the planner,
   execution SUCCEEDED.
   d. An audit trail is written.
   e. The notice goes to the internal test sink.
3. Idempotency: a duplicate YES (same WhatsApp id) and a repeated decision change nothing; a second decision is refused.
4. Double booking: the same dock time can't be claimed twice (exclusion constraint, in a rolled-back transaction).
5. Authorization: the agent role, a planner of another facility and a decision with no staff identity are all refused.
6. Kill switch (owner path): engaged → the deployed agent's booking is refused BOOKING_KILLED, escalated, nothing booked;
   then disengaged; audited both ways.
7. Isolation: no non-synthetic identity; no delivery outside the test sink.

It creates one approval execution (step 2) and no other booking. Concurrency of the booking functions is covered by the
regression suite (`tests/test_concurrency.py`); the smoke doesn't add bookings to prove it.

## 2. Release binding (new)

`--release-sha` (40 hex, required). Each run writes a new file
`docs/release/evidence/smoke/option_d_smoke_<UTC stamp>.json`. The 23 Nov record is never overwritten. New fields:

| Field | Meaning |
|---|---|
| `smoke_id` | `SMOKE-<UTC stamp>-<random>` |
| `environment` | `pilot-ap-southeast-2` |
| `release_sha` | the commit the pilot runs, as given |
| `deployed_code_sha256` | the api's and agent's Lambda CodeSha256 in hex (null if they differ, which fails the gate) |
| `deployed_code_by_function` | both values |
| `facility_id` | FAC-JAI-01 |
| `executions` | `[{approval_id, execution_arn (masked), shipment_id, appointment_id, facility_id}]` |
| `collected_utc`, `checks`, `all_pass`, `evidence`, `log` | as before |

The renewal gate requires all 11 checks present and passing, this environment, the target `release_sha`, the deployed
code hash, ≤ 24 h old, and its own execution attributed to it (B).

## 3. Procedure (weekly or release-specific renewal of the RUNNING release)

```
python scripts/smoke_option_d.py --release-sha <commit the pilot runs>
python scripts/step6_gate_evidence.py --mode renewal --approval-ref OWNER-APPROVAL-YYYYMMDD-<ref> \
       --release-sha <same commit> --smoke docs/release/evidence/smoke/option_d_smoke_<stamp>.json
# BOOKING_WORKFLOW PASS? commit renewal_gates.json + the smoke file, then:
# activate-booking (release branch): approval_ref, confirm=activate-synthetic-pilot-booking, mode=renewal
```

```mermaid
sequenceDiagram
    participant O as owner (present: kill-switch drill)
    participant S as smoke_option_d.py
    participant P as deployed pilot (booking ON)
    participant G as collector --mode renewal
    participant W as activate-booking (mode=renewal)
    O->>S: --release-sha S
    S->>P: synthetic booking, approval, idempotency, isolation, kill switch on/off
    S-->>O: smoke/option_d_smoke_<stamp>.json (S, code hash, execution ids)
    O->>G: --approval-ref R --release-sha S --smoke file
    G->>P: read config, /health, executions, records (0029)
    G-->>O: renewal_gates.json (PASS / FAIL / UNRESOLVED + diagnostics)
    O->>W: commit both files; dispatch with R and mode=renewal
    W->>W: gate_evidence re-checks code hash, freshness, approval, PASS
    W-->>P: evidence rows (or nothing); booking state unchanged
```

Retention: smoke records are committed evidence and are kept. The synthetic rows the smoke creates stay in the pilot
database as audit history, as with the 23 Nov smoke. Nothing is deleted.
