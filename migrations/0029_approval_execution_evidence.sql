-- 0029 · Booking evidence renewal: read-only, RLS-safe access to the approval records behind Step Functions executions.
-- Forward-only and additive: 0001–0028 untouched; no table, column, policy, booking, approval, kill-switch or role change.
--
--   approval_execution_evidence(p_approval_ids uuid[])
--     One row per approval id the CALLER'S facility scope can see (RLS still applies: the function runs as setuhaul_rpc,
--     which is NOBYPASSRLS, and the approval_requests / appointments policies read the caller's app.facility_scope).
--     An id outside the scope, or unknown, is simply absent: the renewal gate reports it as UNRESOLVED.
--
-- Why it exists: the renewal gate must attribute every approval-workflow execution to evidence (a Step 6 drill, a
-- release smoke test, or an owner-confirmed manual test) and verify the record behind it: synthetic shipment, facility,
-- the approval's outcome and the appointment's state. Before 0029 only the database owner could read these rows for the
-- collector; the pipeline's evidence identity could not read them at all.
--
-- Narrow by construction:
--   * EXECUTE only for setuhaul_deploy_evidence (the pipeline's evidence identity, NOBYPASSRLS, insert-only on the
--     evidence tables). Not the console, agent, system or ingest roles; ordinary operations users never reach it.
--   * Only identifiers, states and times. No proposed_action, evidence, decision note, thread, driver or phone. The person
--     who decided is reduced to a kind: 'system' (workflow expiry), 'drill_staff' (a `staff:` drill identity),
--     'console_user' (a signed-in person) or NULL (undecided).
--   * A caller with no facility scope gets an error, not an empty answer, so "nothing visible" is never mistaken for
--     "nothing there". At most 200 ids per call. STABLE, pinned search_path, no dynamic SQL.
SET search_path = setuhaul, public;

CREATE FUNCTION approval_execution_evidence(p_approval_ids uuid[])
RETURNS TABLE (approval_id uuid, kind text, facility_id text, shipment_id text, shipment_is_synthetic boolean,
               appointment_id text, appointment_status text, status text, decided_by_type text, decided_by_role text,
               decider_kind text, outcome_reason text, created_at timestamptz, decided_at timestamptz,
               expires_at timestamptz, task_token_recorded boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF coalesce(current_setting('app.facility_scope', true), '') = '' THEN
    RAISE EXCEPTION 'approval_execution_evidence: the caller has no facility scope' USING ERRCODE = '42501';
  END IF;
  IF p_approval_ids IS NULL OR cardinality(p_approval_ids) = 0 OR cardinality(p_approval_ids) > 200 THEN
    RAISE EXCEPTION 'approval_execution_evidence: between 1 and 200 approval ids' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
    SELECT r.approval_id, r.kind, r.facility_id, r.shipment_id, coalesce(r.shipment_id LIKE 'SYN-%', false),
           r.appointment_id, a.status, r.status, r.decided_by_type, r.decided_by_role,
           CASE WHEN r.decided_by IS NULL THEN NULL
                WHEN r.decided_by_type = 'SYSTEM' THEN 'system'
                WHEN r.decided_by LIKE 'staff:%' THEN 'drill_staff' ELSE 'console_user' END,
           r.outcome_reason, r.created_at, r.decided_at, r.expires_at, r.task_token IS NOT NULL
      FROM approval_requests r
      LEFT JOIN appointments a ON a.appointment_id = r.appointment_id
     WHERE r.approval_id = ANY (p_approval_ids)
     ORDER BY r.created_at;
END $$;
ALTER FUNCTION approval_execution_evidence(uuid[]) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION approval_execution_evidence(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION approval_execution_evidence(uuid[]) TO setuhaul_deploy_evidence;
