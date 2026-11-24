# RLS-safe evidence access (migration 0029)

## 1. Problem

The renewal gate must read the approval record behind each execution. `approval_requests` and `appointments` have
FORCE ROW LEVEL SECURITY (`scope_allows(facility_id)` on the caller's `app.facility_scope`). Before 0029:
* the collector read drill ids with the **database owner** session and a self-set scope, which is far broader than needed;
* the pipeline's evidence identity (`setuhaul_deploy_evidence`) could not read approvals at all;
* the `system` login without a scope saw nothing, and an owner-secret read from the agent session was refused by its
  permission check (correctly).

## 2. Mechanism

`approval_execution_evidence(p_approval_ids uuid[])`:

| Property | Value |
|---|---|
| Kind | `SECURITY DEFINER`, `STABLE`, plpgsql, no dynamic SQL, pinned `search_path = setuhaul, pg_catalog, pg_temp` |
| Owner | `setuhaul_rpc`: NOLOGIN, **NOBYPASSRLS**, not superuser (tested), so RLS applies inside the function |
| Scope | the caller's `app.facility_scope`; **no scope is an error** (42501), so "nothing visible" is never read as "nothing there" |
| Input | 1–200 approval ids per call |
| Output | ids, states and times only: approval id, kind, facility, shipment, synthetic flag, appointment id and status, approval status, decided-by type and role, a **decider kind** (`system` / `drill_staff` / `console_user` / null: never the person's id), outcome reason, created/decided/expires, whether a task token was recorded |
| Never returned | `proposed_action`, `evidence`, decision note, thread, task token value, `decided_by` value, drivers, phones |
| EXECUTE | `setuhaul_deploy_evidence` only; revoked from PUBLIC. Console, agent, system, ingest and feasibility roles are refused (tested) |
| Privileges added | **none** on tables; `setuhaul_rpc` already had SELECT on both tables (0002, 0015, 0025). The evidence identity still cannot SELECT either table directly (tested) |

```mermaid
sequenceDiagram
    participant C as collector (renewal mode)
    participant DA as RDS Data API
    participant F as approval_execution_evidence (as setuhaul_rpc)
    participant T as approval_requests / appointments (FORCE RLS)
    C->>DA: begin (deploy_evidence login secret)
    C->>DA: SET TRANSACTION READ ONLY
    C->>DA: set role setuhaul_deploy_evidence; app.facility_scope = FAC-JAI-01,FAC-GGN-01
    C->>F: ids (≤ 200 per call)
    F->>T: SELECT … WHERE approval_id = ANY(ids)
    T-->>F: only rows scope_allows(facility_id)
    F-->>C: ids, states, times (no PII, no notes)
    C->>DA: rollback
```

## 3. Authorization boundary

* The function answers only the pilot facilities the caller names. Rows of other facilities are absent (tested: a JAI
  scope never returns the GGN approval).
* Ordinary operations users act through the `setuhaul_console` role, which cannot execute it. Console RLS on
  `approval_requests` is unchanged (tested: a GGN console scope sees GGN only).
* `setuhaul_deploy_evidence` is a pipeline identity whose secret lives with the deployment; its only other rights are
  inserting append-only evidence. It can set its own facility scope, exactly as the owner session did. Its read is now
  limited to these columns, instead of the owner's full access.

## 4. Tests (`tests/test_renewal_gate.py`, local PostgreSQL)

* `test_0029_the_evidence_identity_reads_only_its_facility_scope_and_only_the_needed_fields`
* `test_0029_without_a_facility_scope_is_an_error_not_an_empty_answer` (and the 200-id limit)
* `test_0029_no_application_role_may_call_it` (console, agent, system, ingest, feasibility)
* `test_0029_is_narrow_by_construction_and_rls_is_unchanged` (definer, stable, owner, no PUBLIC execute, rpc
  NOBYPASSRLS, RLS forced, no direct table SELECT for the evidence identity, console RLS unchanged)

## 5. Not done here

0029 is not deployed. Until it is, the renewal gate on the live pilot reports every execution UNRESOLVED with the
blocker "approval records could not be read through approval_execution_evidence". It fails closed.
