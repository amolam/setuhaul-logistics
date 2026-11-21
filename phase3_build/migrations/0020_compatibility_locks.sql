-- 0020 · Review R3.1 fixes. Forward-only; 0001–0019 untouched.
--
-- R3.1-P1 A decision and a concurrent shipment or dock change are now serialised by one lock order:
--
--     thread_lock → shipments (row) → docks (row) → approval_requests → appointments → dock_occupancy
--
--   A shipment or dock UPDATE takes its row lock first, then its invalidation trigger locks the approval rows: steps 2/3
--   then 4. decide_booking_request now does the same: it SHARE-locks the shipment and the dock BEFORE the approval row,
--   then re-reads them under the lock. So either
--     (a) the change got there first: the decision waits for it, then finds the request INVALIDATED; or
--     (b) the decision got there first: the change waits for the decision to commit, so it is a change to a CONFIRMED
--         booking. If that booking is no longer compatible, the trigger raises a BOOKING_AT_RISK escalation for a
--         person (a confirmed booking is never silently cancelled).
--   No path takes these locks in the reverse order: expiry, withdraw, reject, block_dock and the sweeps never lock a
--   shipment or dock row (block_dock's dock_events insert takes only FOR KEY SHARE through its foreign key, which does
--   not conflict with SHARE).
-- R3.1-P2 planner inherits every operations permission (B-WF-2 §8): take_over_thread and hand_back_thread allow it,
--         and their audit rows record the staff role itself (the 0004 bodies wrote 'operations' for everyone).
SET search_path = setuhaul, public;

-- FOR SHARE needs UPDATE privilege on at least one column. setuhaul_rpc (NOLOGIN, owner of the definer functions)
-- gets it on a bookkeeping column only; no function writes these columns.
ALTER TABLE docks ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
GRANT UPDATE (updated_at) ON shipments, docks TO setuhaul_rpc;

ALTER TABLE escalations DROP CONSTRAINT escalations_reason_code_check;
ALTER TABLE escalations ADD CONSTRAINT escalations_reason_code_check CHECK (reason_code IN (
  'NO_FEASIBLE_SLOT','SAFETY','CONTRADICTORY_DATA','AFTER_HOURS_APPROVAL','CRITICAL_CONFLICT','CLARIFICATION_LIMIT',
  'PENDING_EXPIRED','DRIVER_REQUEST','OUT_OF_SCOPE','AGENT_FAILURE','NOTIFICATION_FAILED','BREAKDOWN','BOOKING_AT_RISK'));

-- ================================================================ confirmed bookings made incompatible after the decision
CREATE FUNCTION flag_bookings_at_risk(p_shipment text, p_dock text, p_cause text) RETURNS integer LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a appointments; s shipments; d docks; why text; n integer := 0;
BEGIN
  FOR a IN SELECT * FROM appointments WHERE is_current AND status = 'CONFIRMED'
              AND (p_shipment IS NULL OR shipment_id = p_shipment) AND (p_dock IS NULL OR dock_id = p_dock) LOOP
    SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id;
    SELECT * INTO d FROM docks WHERE dock_id = a.dock_id;
    why := booking_incompatibility(s, d);
    IF why IS NULL THEN CONTINUE; END IF;
    INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, handoff, ack_due_at)
    VALUES (gen_random_uuid(), 'BOOKING_AT_RISK', 'DECIDE', d.facility_id, a.shipment_id,
            jsonb_build_object('appointment_id', a.appointment_id, 'dock_id', a.dock_id, 'cause', p_cause, 'why', why),
            clock_timestamp() + interval '10 minutes');
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION trg_invalidate_on_shipment() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  IF NEW.current_status IN ('IN_DOCK','COMPLETED','CANCELLED')
     OR (NEW.vehicle_id, NEW.destination_facility_id, NEW.load_weight_kg, NEW.required_dock_type, NEW.temperature_control_required)
        IS DISTINCT FROM
        (OLD.vehicle_id, OLD.destination_facility_id, OLD.load_weight_kg, OLD.required_dock_type, OLD.temperature_control_required)
  THEN
    PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities), true);
    PERFORM invalidate_open_requests('shipment', NEW.shipment_id, 'SHIPMENT_CHANGED');
    IF NEW.current_status NOT IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
      PERFORM flag_bookings_at_risk(NEW.shipment_id, NULL, 'SHIPMENT_CHANGED');
    END IF;
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION trg_invalidate_on_dock() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', NEW.facility_id, true);
  PERFORM invalidate_open_requests('dock', NEW.dock_id, 'DOCK_UNAVAILABLE');
  PERFORM flag_bookings_at_risk(NULL, NEW.dock_id, 'DOCK_CHANGED');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;

-- ================================================================ decide: shipment and dock locked before the approval
CREATE OR REPLACE FUNCTION decide_booking_request(p_appointment text, p_decision text, p_idem_key text, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  ctx staff_context := staff_ctx();                                           -- UNTRUSTED_CONTEXT without one
  r approval_requests; a appointments; old appointments; claim dock_occupancy; s shipments; d docks; cfg facility_config;
  role text; clk timestamptz; resp jsonb; k text := 'decide:' || p_idem_key; why text; v_ship text; v_dock text;
BEGIN
  IF p_decision NOT IN ('CONFIRM','REJECT') THEN RAISE EXCEPTION 'BAD_DECISION: %', p_decision USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_appointment, p_decision, ctx.principal)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  -- which shipment and dock (immutable on the request and the appointment), read without a lock
  SELECT ar.shipment_id, ap.dock_id INTO v_ship, v_dock
    FROM approval_requests ar JOIN appointments ap ON ap.appointment_id = ar.appointment_id
   WHERE ar.appointment_id = p_appointment;
  IF v_ship IS NULL THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF p_decision = 'CONFIRM' THEN                     -- R3.1-P1: lock order steps 2 and 3, before the approval row
    SELECT * INTO s FROM shipments WHERE shipment_id = v_ship FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'SHIPMENT_NOT_VISIBLE' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO d FROM docks WHERE dock_id = v_dock FOR SHARE;
  END IF;

  SELECT * INTO r FROM approval_requests WHERE appointment_id = p_appointment FOR UPDATE;     -- lock order 4
  IF NOT FOUND OR NOT (r.facility_id = ANY (ctx.facilities)) THEN
    RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT x.role INTO role FROM approval_role_rules x WHERE x.kind = r.kind AND x.role = ANY (ctx.roles)
   ORDER BY array_position(ARRAY['operations','planner','ops_lead'], x.role) LIMIT 1;
  IF role IS NULL THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: % requests need one of %', r.kind,
    (SELECT array_agg(x.role) FROM approval_role_rules x WHERE x.kind = r.kind) USING ERRCODE = 'P0001'; END IF;
  IF r.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_DECIDED: %', r.status USING ERRCODE = 'P0001'; END IF;

  IF p_decision = 'REJECT' THEN                     -- close_approval takes the locks, then checks the deadline (R3-P1-01)
    resp := close_approval(r.approval_id, 'REJECTED', coalesce(p_note, 'rejected'), ctx.principal, 'HUMAN', role, NULL, NULL, p_note);
    RETURN idem_finish(k, resp || jsonb_build_object('decided_by', ctx.principal));
  END IF;

  SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id FOR UPDATE;      -- lock order 5
  SELECT * INTO old FROM appointments
   WHERE shipment_id = a.shipment_id AND is_current AND status IN ('CONFIRMED','IN_PROGRESS') FOR UPDATE;
  SELECT * INTO claim FROM dock_occupancy WHERE occupancy_id = r.claim_occupancy_id FOR UPDATE;   -- lock order 6
  clk := clock_timestamp();                                                   -- §14: the deadline, after the locks
  IF r.expires_at <= clk THEN RAISE EXCEPTION 'PENDING_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF booking_hold_active(r.facility_id, a.dock_id) THEN
    RAISE EXCEPTION 'OPERATIONAL_HOLD: approvals are on hold at this facility or dock' USING ERRCODE = 'P0001'; END IF;
  IF claim.occupancy_id IS NULL OR NOT claim.active OR claim.kind <> 'APPOINTMENT' OR claim.ref_id <> a.appointment_id
     OR claim.dock_id <> a.dock_id OR NOT (claim.during @> a.during) THEN
    RAISE EXCEPTION 'CAPACITY_LOST' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO cfg FROM facility_config WHERE facility_id = r.facility_id AND active;
  IF lower(a.during) - make_interval(mins => cfg.confirm_lead_min) < clk THEN
    RAISE EXCEPTION 'SLOT_TOO_LATE' USING ERRCODE = 'P0001'; END IF;
  IF r.kind = 'STANDARD' AND slot_is_after_hours(r.facility_id, a.during) THEN
    RAISE EXCEPTION 'OUTSIDE_HOURS' USING ERRCODE = 'P0001'; END IF;
  -- re-read under the SHARE locks: no change can commit between this read and our commit
  SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'SHIPMENT_NOT_VISIBLE' USING ERRCODE = 'P0001'; END IF;
  IF s.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: %', s.current_status USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO d FROM docks WHERE dock_id = a.dock_id;
  why := booking_incompatibility(s, d);
  IF why IS NOT NULL THEN RAISE EXCEPTION '%', why USING ERRCODE = 'P0001'; END IF;
  IF old.appointment_id IS NOT NULL AND old.status = 'IN_PROGRESS' THEN
    RAISE EXCEPTION 'SHIPMENT_IN_DOCK' USING ERRCODE = 'P0001'; END IF;

  IF old.appointment_id IS NOT NULL THEN
    UPDATE appointments SET status = 'CANCELLED', is_current = false, updated_at = clk, row_version = row_version + 1
     WHERE appointment_id = old.appointment_id;
    UPDATE dock_occupancy SET active = false WHERE occupancy_id = old.occupancy_id;
  END IF;
  UPDATE appointments SET status = 'CONFIRMED', is_current = true, warehouse_confirmation_ref = ctx.principal,
                          confirmation_basis = 'HUMAN', updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id;
  UPDATE approval_requests SET status = 'APPROVED', decided_at = clk, decided_by = ctx.principal, decided_by_type = 'HUMAN',
                               decided_by_role = role, outcome_reason = coalesce(p_note, 'approved'), decision_note = p_note
   WHERE approval_id = r.approval_id;
  resp := jsonb_build_object('approval_id', r.approval_id, 'appointment_id', a.appointment_id, 'shipment_id', a.shipment_id,
                             'outcome', 'APPROVED', 'status', 'CONFIRMED', 'replaced', old.appointment_id,
                             'decided_by', ctx.principal, 'decided_by_type', 'HUMAN', 'decided_by_role', role, 'decided_at', clk);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (r.facility_id, 'appointment', a.appointment_id, 'BookingConfirmed', resp),
         (r.facility_id, 'approval', r.approval_id::text, 'ApprovalDecided', resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'APPROVAL_APPROVED', r.facility_id, a.shipment_id, to_jsonb(a), resp, p_note);
  RETURN idem_finish(k, resp);
END $$;

-- ================================================================ R3.1-P2 planner inherits operations permissions (§8)
CREATE OR REPLACE FUNCTION take_over_thread(p_thread text, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text; role text; n integer; sid text;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['operations','planner','ops_lead'], 'taking over a conversation');
  PERFORM thread_lock(p_thread);
  SELECT s.destination_facility_id INTO fac FROM chat_threads t LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
   WHERE t.thread_id = p_thread;
  IF fac IS NOT NULL AND NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  -- the 0004 body, with the audit role taken from the staff identity
  UPDATE chat_threads SET human_active = true, status = 'WAITING_FOR_HUMAN' WHERE thread_id = p_thread
  RETURNING shipment_id INTO sid;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO thread_flow (thread_id, state) VALUES (p_thread, 'HUMAN')
  ON CONFLICT (thread_id) DO UPDATE SET state = 'HUMAN', version = thread_flow.version + 1, updated_at = now();
  UPDATE confirmation_attestations SET expires_at = now() WHERE thread_id = p_thread AND expires_at > now();
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, reason)
  VALUES (ctx.principal, role, 'TAKE_OVER_THREAD', fac, sid, p_reason);
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION hand_back_thread(p_thread text, p_principal text, p_summary text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text; role text; n integer; sid text;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['operations','planner','ops_lead'], 'handing back a conversation');
  IF p_summary IS NULL OR length(p_summary) < 10 THEN
    RAISE EXCEPTION 'HANDBACK_SUMMARY_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  PERFORM thread_lock(p_thread);
  SELECT s.destination_facility_id INTO fac FROM chat_threads t LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
   WHERE t.thread_id = p_thread;
  IF fac IS NOT NULL AND NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'THREAD_NOT_IN_HUMAN_HANDS' USING ERRCODE = 'P0001'; END IF;
  -- the 0004 body, with the audit role taken from the staff identity
  UPDATE chat_threads SET human_active = false, status = 'OPEN' WHERE thread_id = p_thread AND human_active
  RETURNING shipment_id INTO sid;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN RAISE EXCEPTION 'THREAD_NOT_IN_HUMAN_HANDS' USING ERRCODE = 'P0001'; END IF;
  UPDATE thread_flow SET state = 'NO_OPTIONS', selected_option = NULL, yes_message_id = NULL, hold_option = NULL,
                         attestation_id = NULL, version = version + 1, updated_at = now() WHERE thread_id = p_thread;
  UPDATE shown_options SET expires_at = least(expires_at, now()) WHERE thread_id = p_thread;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, reason)
  VALUES (ctx.principal, role, 'HAND_BACK_THREAD', fac, sid, p_summary);
  RETURN true;
END $$;

DO $$ DECLARE f text; BEGIN
  FOREACH f IN ARRAY ARRAY['flag_bookings_at_risk(text,text,text)', 'trg_invalidate_on_shipment()', 'trg_invalidate_on_dock()',
                           'decide_booking_request(text,text,text,text)', 'take_over_thread(text,text,text)',
                           'hand_back_thread(text,text,text)'] LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO setuhaul_rpc', f);
  END LOOP;
END $$;
GRANT INSERT ON escalations TO setuhaul_rpc;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
