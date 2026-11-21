-- 0019 · Complete-build review (R3) fixes. Forward-only; 0001–0018 untouched.
--
-- R3-P1-01 Every final outcome respects the deadline after its locks. close_approval() now takes the appointment and
--          claim locks FIRST, then reads clock_timestamp(). Past the deadline, a person's reject or withdraw is refused
--          (PENDING_EXPIRED, like confirm: the sweep records EXPIRED), and a system-side outcome (invalidation,
--          supersession) is recorded as EXPIRED instead, so the change that triggered it still commits. The
--          PENDING_EXPIRED escalation is raised in the same place, for every path that ends in EXPIRED.
-- R3-P1-02 Shipment changes that affect dock compatibility (destination, weight, dock type, refrigeration) invalidate
--          open requests; decide_booking_request re-proves compatibility under its locks; the hold→claim conversion
--          at submission re-runs the occupancy compatibility guard (the trigger now also fires on a change of kind).
-- R3-P2-01 A person taking over stops RETRY and UNKNOWN replies stored before the takeover, not only PENDING ones, and
--          begin_send() re-checks just before the provider call. An UNKNOWN reply may already be on the phone: it is
--          not resent, and the coordinator is told (NOTIFICATION_FAILED, reason HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND).
-- R3-P2-03 All console mutations use the trusted staff context: set_automation_paused, take_over_thread and
--          hand_back_thread check the role and record the principal from staff_context; block_dock checks the role.
--          The console role loses its direct writes to the audit log, the outbox and the thread's human flag.
SET search_path = setuhaul, public;

-- ================================================================ R3-P1-01 the deadline, after the locks, in one place
CREATE OR REPLACE FUNCTION close_approval(p_approval uuid, p_status text, p_reason text, p_by text, p_by_type text, p_role text,
                                          p_withdraw_code text DEFAULT NULL, p_superseded_by text DEFAULT NULL,
                                          p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; a appointments; clk timestamptz; resp jsonb; appt_status text; ev text;
  v_status text := p_status; v_reason text := p_reason; v_by text := p_by; v_by_type text := p_by_type; v_role text := p_role;
  requested text;
BEGIN
  IF p_status NOT IN ('REJECTED','EXPIRED','INVALIDATED','WITHDRAWN','SUPERSEDED') THEN
    RAISE EXCEPTION 'BAD_OUTCOME: %', p_status USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO r FROM approval_requests WHERE approval_id = p_approval;     -- the caller holds this row's lock
  IF r.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_DECIDED: %', r.status USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id FOR UPDATE;          -- lock order 3
  PERFORM 1 FROM dock_occupancy WHERE occupancy_id = r.claim_occupancy_id FOR UPDATE;            -- lock order 4
  clk := clock_timestamp();                                                  -- §14: the deadline, after every lock
  IF p_status <> 'EXPIRED' AND r.expires_at <= clk THEN
    IF p_by_type = 'HUMAN' AND p_status IN ('REJECTED','WITHDRAWN') THEN
      RAISE EXCEPTION 'PENDING_EXPIRED' USING ERRCODE = 'P0001'; END IF;
    requested := p_status;                                                   -- invalidation or supersession: too late
    v_status := 'EXPIRED'; v_reason := 'NO_DECISION_BY_DEADLINE';
    v_by := 'system:expiry'; v_by_type := 'SYSTEM'; v_role := 'system';
  END IF;
  appt_status := CASE v_status WHEN 'REJECTED' THEN 'REJECTED' WHEN 'EXPIRED' THEN 'EXPIRED' ELSE 'CANCELLED' END;
  ev := CASE v_status WHEN 'REJECTED' THEN 'BookingRejected' WHEN 'EXPIRED' THEN 'BookingExpired'
                      WHEN 'INVALIDATED' THEN 'BookingInvalidated' WHEN 'WITHDRAWN' THEN 'BookingWithdrawn'
                      ELSE 'BookingSuperseded' END;
  UPDATE dock_occupancy SET active = false WHERE occupancy_id = r.claim_occupancy_id;
  UPDATE appointments SET status = appt_status, updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id;
  UPDATE approval_requests SET status = v_status, decided_at = clk, decided_by = v_by, decided_by_type = v_by_type,
                               decided_by_role = v_role, outcome_reason = v_reason,
                               decision_note = CASE WHEN requested IS NULL THEN p_note
                                                    ELSE concat_ws(' · ', 'deadline passed before ' || lower(requested), p_note) END,
                               withdraw_reason_code = CASE WHEN v_status = 'WITHDRAWN' THEN p_withdraw_code END,
                               superseded_by = CASE WHEN v_status = 'SUPERSEDED' THEN p_superseded_by END
   WHERE approval_id = r.approval_id;
  resp := jsonb_build_object('approval_id', r.approval_id, 'appointment_id', a.appointment_id, 'shipment_id', a.shipment_id,
                             'outcome', v_status, 'status', appt_status, 'reason', v_reason, 'decided_by_type', v_by_type,
                             'decided_at', clk, 'still_valid_booking', a.replaced_appointment_id,
                             'withdraw_reason_code', CASE WHEN v_status = 'WITHDRAWN' THEN p_withdraw_code END,
                             'superseded_by', CASE WHEN v_status = 'SUPERSEDED' THEN p_superseded_by END,
                             'requested_outcome', requested);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (r.facility_id, 'appointment', a.appointment_id, ev, resp),
         (r.facility_id, 'approval', r.approval_id::text, 'ApprovalDecided', resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (v_by, v_role, 'APPROVAL_' || v_status, r.facility_id, a.shipment_id, to_jsonb(a), resp, coalesce(p_note, v_reason));
  IF v_status = 'EXPIRED' THEN                                               -- every path to EXPIRED tells a person
    INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, handoff, ack_due_at)
    VALUES (gen_random_uuid(), 'PENDING_EXPIRED', 'DECIDE', r.facility_id, r.shipment_id,
            jsonb_build_object('appointment_id', r.appointment_id, 'approval_id', r.approval_id, 'expired_at', clk,
                               'still_valid_booking', r.proposed_action ->> 'replaces', 'requested_outcome', requested),
            clk + interval '10 minutes');
  END IF;
  RETURN resp;
END $$;

-- the escalation now comes from close_approval; these only find what is due
CREATE OR REPLACE FUNCTION expire_pending_requests() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; n integer := 0;
BEGIN
  FOR r IN SELECT * FROM approval_requests WHERE status = 'OPEN' AND expires_at <= clock_timestamp()
            ORDER BY approval_id FOR UPDATE SKIP LOCKED LOOP
    IF r.status <> 'OPEN' OR r.expires_at > clock_timestamp() THEN CONTINUE; END IF;
    PERFORM close_approval(r.approval_id, 'EXPIRED', 'NO_DECISION_BY_DEADLINE', 'system:expiry', 'SYSTEM', 'system');
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION expire_approval(p_approval uuid) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests;
BEGIN
  SELECT * INTO r FROM approval_requests WHERE approval_id = p_approval FOR UPDATE;
  IF NOT FOUND THEN RETURN 'UNKNOWN'; END IF;
  IF r.status <> 'OPEN' THEN RETURN r.status; END IF;
  IF r.expires_at > clock_timestamp() THEN RETURN 'NOT_DUE'; END IF;
  PERFORM close_approval(r.approval_id, 'EXPIRED', 'NO_DECISION_BY_DEADLINE', 'system:expiry', 'SYSTEM', 'system');
  RETURN 'EXPIRED';
END $$;

-- ================================================================ R3-P1-02 shipment compatibility
-- The database's hard dock rules, the same ones trg_occupancy_compat (0001) enforces on every claim of dock time:
-- the dock is at the shipment's destination, takes its weight, is refrigerated if the load needs it, and is a heavy
-- dock if the load needs one. (The engine's exact dock-type match is a preference for options, not a hard rule.)
CREATE FUNCTION booking_incompatibility(s shipments, d docks) RETURNS text LANGUAGE plpgsql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE heavy_kg integer;
BEGIN
  SELECT heavy_dock_required_kg INTO heavy_kg FROM facility_config WHERE facility_id = d.facility_id AND active;
  IF s.destination_facility_id IS DISTINCT FROM d.facility_id THEN
    RETURN format('DOCK_WRONG_FACILITY: %s is not at %s', d.dock_id, s.destination_facility_id); END IF;
  IF s.load_weight_kg > d.max_vehicle_weight_kg THEN
    RETURN format('DOCK_WEIGHT_EXCEEDED: %s kg > %s kg on %s', s.load_weight_kg, d.max_vehicle_weight_kg, d.dock_code); END IF;
  IF s.temperature_control_required AND NOT d.supports_refrigerated THEN
    RETURN format('DOCK_NOT_REFRIGERATED: %s', d.dock_code); END IF;
  IF (s.required_dock_type = 'HEAVY' OR (heavy_kg IS NOT NULL AND s.load_weight_kg > heavy_kg)) AND d.dock_type <> 'HEAVY' THEN
    RETURN format('DOCK_NOT_HEAVY: %s', d.dock_code); END IF;
  RETURN NULL;
END $$;

-- converting a hold into a request's claim (or any change of kind) re-proves compatibility with today's shipment data
DROP TRIGGER occupancy_compat ON dock_occupancy;
CREATE TRIGGER occupancy_compat BEFORE INSERT OR UPDATE OF dock_id, during, shipment_id, active, kind ON dock_occupancy
  FOR EACH ROW EXECUTE FUNCTION trg_occupancy_compat();

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
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  END IF;
  RETURN NULL;
END $$;
DROP TRIGGER invalidate_on_shipment ON shipments;
CREATE TRIGGER invalidate_on_shipment
  AFTER UPDATE OF current_status, vehicle_id, destination_facility_id, load_weight_kg, required_dock_type,
                  temperature_control_required ON shipments
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_on_shipment();

-- decide: unchanged from 0015 except the compatibility re-proof after the locks (marked R3-P1-02)
CREATE OR REPLACE FUNCTION decide_booking_request(p_appointment text, p_decision text, p_idem_key text, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  ctx staff_context := staff_ctx();                                           -- UNTRUSTED_CONTEXT without one
  r approval_requests; a appointments; old appointments; claim dock_occupancy; s shipments; d docks; cfg facility_config;
  role text; clk timestamptz; resp jsonb; k text := 'decide:' || p_idem_key; why text;
BEGIN
  IF p_decision NOT IN ('CONFIRM','REJECT') THEN RAISE EXCEPTION 'BAD_DECISION: %', p_decision USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_appointment, p_decision, ctx.principal)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  SELECT * INTO r FROM approval_requests WHERE appointment_id = p_appointment FOR UPDATE;     -- lock order 2
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

  SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id FOR UPDATE;      -- lock order 3
  SELECT * INTO old FROM appointments
   WHERE shipment_id = a.shipment_id AND is_current AND status IN ('CONFIRMED','IN_PROGRESS') FOR UPDATE;
  SELECT * INTO claim FROM dock_occupancy WHERE occupancy_id = r.claim_occupancy_id FOR UPDATE;   -- lock order 4
  clk := clock_timestamp();                                                   -- §14: the deadline, after the locks
  IF r.expires_at <= clk THEN RAISE EXCEPTION 'PENDING_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF booking_hold_active(r.facility_id, a.dock_id) THEN
    RAISE EXCEPTION 'OPERATIONAL_HOLD: approvals are on hold at this facility or dock' USING ERRCODE = 'P0001'; END IF;
  -- §5: the database re-proves the booking; the application's feasibility check was advisory
  IF claim.occupancy_id IS NULL OR NOT claim.active OR claim.kind <> 'APPOINTMENT' OR claim.ref_id <> a.appointment_id
     OR claim.dock_id <> a.dock_id OR NOT (claim.during @> a.during) THEN
    RAISE EXCEPTION 'CAPACITY_LOST' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO cfg FROM facility_config WHERE facility_id = r.facility_id AND active;
  IF lower(a.during) - make_interval(mins => cfg.confirm_lead_min) < clk THEN
    RAISE EXCEPTION 'SLOT_TOO_LATE' USING ERRCODE = 'P0001'; END IF;
  IF r.kind = 'STANDARD' AND slot_is_after_hours(r.facility_id, a.during) THEN
    RAISE EXCEPTION 'OUTSIDE_HOURS' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'SHIPMENT_NOT_VISIBLE' USING ERRCODE = 'P0001'; END IF;
  IF s.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: %', s.current_status USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO d FROM docks WHERE dock_id = a.dock_id;
  why := booking_incompatibility(s, d);                                       -- R3-P1-02
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

-- ================================================================ R3-P2-01 a person taking over stops stale replies
CREATE FUNCTION reply_superseded_by_takeover(p_message bigint) RETURNS boolean LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT coalesce(t.human_active AND m.created_at < t.human_active_since, false)
    FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id WHERE m.message_id = p_message
$$;

CREATE OR REPLACE FUNCTION claim_notifications(p_limit integer DEFAULT 20)
RETURNS TABLE (notification_id uuid, attempt integer, channel text, recipient_ref text, template_id text, payload jsonb,
               body text, is_resend boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE r notifications; m chat_messages; clk timestamptz := clock_timestamp(); resend boolean;
BEGIN
  FOR r IN SELECT * FROM notifications x
            WHERE x.status = 'PENDING'
               OR (x.status = 'RETRY' AND x.next_retry_at <= clk)
               OR (x.status = 'UNKNOWN' AND x.unknown_deadline <= clk AND x.unknown_resend_count = 0)
            ORDER BY x.created_at FOR UPDATE SKIP LOCKED LIMIT p_limit LOOP
    resend := r.status = 'UNKNOWN';
    body := NULL;
    IF r.chat_message_id IS NOT NULL THEN
      SELECT * INTO m FROM chat_messages WHERE message_id = r.chat_message_id;
      -- a person took over AFTER this reply was stored: it is not sent (PENDING), not retried (RETRY: known unsent),
      -- and not resent (UNKNOWN: it may already be on the phone, so the coordinator is told). A reply stored after the
      -- takeover (the "a coordinator will call you" hand-over message) is sent.
      IF reply_superseded_by_takeover(r.chat_message_id) THEN
        IF r.status = 'UNKNOWN' THEN
          UPDATE notifications SET status = 'FAILED', last_error = 'HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND'
           WHERE notifications.notification_id = r.notification_id;
          PERFORM notification_failed(r.notification_id, 'HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND');
        ELSE
          UPDATE notifications SET status = 'FAILED', last_error = 'HUMAN_TOOK_OVER'
           WHERE notifications.notification_id = r.notification_id;
        END IF;
        CONTINUE;
      END IF;
      IF m.message_id IS NULL OR encode(sha256(convert_to(m.body_masked, 'UTF8')), 'hex') <> r.content_hash THEN
        UPDATE notifications SET status = 'FAILED', last_error = 'CANONICAL_MISMATCH' WHERE notifications.notification_id = r.notification_id;
        PERFORM notification_failed(r.notification_id, 'CANONICAL_MISMATCH');
        CONTINUE;
      END IF;
      body := m.body_masked;
    END IF;
    IF r.attempts >= r.max_attempts THEN
      UPDATE notifications SET status = 'FAILED', last_error = coalesce(r.last_error, 'MAX_ATTEMPTS')
       WHERE notifications.notification_id = r.notification_id;
      PERFORM notification_failed(r.notification_id, 'MAX_ATTEMPTS');
      CONTINUE;
    END IF;
    UPDATE notifications SET status = 'SENDING', attempts = r.attempts + 1 WHERE notifications.notification_id = r.notification_id;
    notification_id := r.notification_id; attempt := r.attempts + 1; channel := r.channel; recipient_ref := r.recipient_ref;
    template_id := r.template_id; payload := r.payload; is_resend := resend;
    RETURN NEXT;
  END LOOP;
END $$;

-- Called by the relay immediately before the provider call. False = do not send: the claim is stale (fenced by the
-- attempt number) or a person took over since the claim. A takeover between this check and the provider call cannot
-- be fenced (the send is outside the database); that window is the duration of one function return.
CREATE FUNCTION begin_send(p_notification uuid, p_attempt integer) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r notifications;
BEGIN
  SELECT * INTO r FROM notifications WHERE notification_id = p_notification FOR UPDATE;
  IF NOT FOUND OR r.status <> 'SENDING' OR r.attempts <> p_attempt THEN RETURN false; END IF;
  IF r.chat_message_id IS NOT NULL AND reply_superseded_by_takeover(r.chat_message_id) THEN
    IF r.unknown_resend_count > 0 THEN                                         -- the first send may be on the phone
      UPDATE notifications SET status = 'FAILED', last_error = 'HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND'
       WHERE notification_id = r.notification_id;
      PERFORM notification_failed(r.notification_id, 'HUMAN_TOOK_OVER_AFTER_AMBIGUOUS_SEND');
    ELSE
      UPDATE notifications SET status = 'FAILED', last_error = 'HUMAN_TOOK_OVER' WHERE notification_id = r.notification_id;
    END IF;
    RETURN false;
  END IF;
  RETURN true;
END $$;

-- ================================================================ R3-P2-03 every console mutation uses the staff identity
CREATE FUNCTION staff_role_for(ctx staff_context, p_allowed text[], p_what text) RETURNS text LANGUAGE plpgsql IMMUTABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE role text;
BEGIN
  SELECT x INTO role FROM unnest(ctx.roles) x WHERE x = ANY (p_allowed)
   ORDER BY array_position(ARRAY['operations','planner','ops_lead'], x) DESC LIMIT 1;
  IF role IS NULL THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: % needs one of %', p_what, p_allowed USING ERRCODE = 'P0001'; END IF;
  RETURN role;
END $$;

CREATE FUNCTION check_principal(ctx staff_context, p_principal text) RETURNS void LANGUAGE plpgsql IMMUTABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF p_principal IS DISTINCT FROM ctx.principal THEN
    RAISE EXCEPTION 'PRINCIPAL_MISMATCH: the audit principal comes from the staff identity' USING ERRCODE = 'P0001'; END IF;
END $$;

CREATE OR REPLACE FUNCTION set_automation_paused(p_facility text, p_paused boolean, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); role text;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['ops_lead'], 'pausing automation');
  IF NOT (p_facility = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  UPDATE facilities SET automation_paused = p_paused WHERE facility_id = p_facility;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason, after_state)
  VALUES (ctx.principal, role, CASE WHEN p_paused THEN 'PAUSE_AUTOMATION' ELSE 'RESUME_AUTOMATION' END,
          p_facility, p_reason, jsonb_build_object('automation_paused', p_paused));
  RETURN p_paused;
END $$;

-- take over / hand back: the 0006/0007 wrappers (thread lock first) now check identity, role and facility
CREATE OR REPLACE FUNCTION take_over_thread(p_thread text, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  PERFORM staff_role_for(ctx, ARRAY['operations','ops_lead'], 'taking over a conversation');
  PERFORM thread_lock(p_thread);
  SELECT s.destination_facility_id INTO fac FROM chat_threads t LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
   WHERE t.thread_id = p_thread;
  IF fac IS NOT NULL AND NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  RETURN take_over_thread_v1(p_thread, ctx.principal, p_reason);
END $$;

CREATE OR REPLACE FUNCTION hand_back_thread(p_thread text, p_principal text, p_summary text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  PERFORM staff_role_for(ctx, ARRAY['operations','ops_lead'], 'handing back a conversation');
  PERFORM thread_lock(p_thread);
  SELECT s.destination_facility_id INTO fac FROM chat_threads t LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
   WHERE t.thread_id = p_thread;
  IF fac IS NOT NULL AND NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'THREAD_NOT_IN_HUMAN_HANDS' USING ERRCODE = 'P0001'; END IF;
  RETURN hand_back_thread_v1(p_thread, ctx.principal, p_summary);
END $$;

CREATE OR REPLACE FUNCTION block_dock(p_event_id text, p_dock text, p_from timestamptz, p_to timestamptz,
                                      p_type text, p_reason text, p_principal text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text; occ bigint; role text;
BEGIN
  IF p_type NOT IN ('MAINTENANCE','BREAKDOWN','MANUAL_BLOCK') THEN
    RAISE EXCEPTION 'BAD_BLOCK_TYPE: %', p_type USING ERRCODE = 'P0001'; END IF;
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['planner','ops_lead'], 'blocking a dock');              -- R3-P2-03
  SELECT facility_id INTO fac FROM docks WHERE dock_id = p_dock;
  IF NOT FOUND OR NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dock_events WHERE dock_event_id = p_event_id) THEN       -- idempotent by event id
    SELECT occupancy_id INTO occ FROM dock_occupancy WHERE kind = 'BLOCK' AND ref_id = p_event_id;
    RETURN occ;
  END IF;
  PERFORM invalidate_open_requests('dock', p_dock, 'CAPACITY_LOST', tstzrange(p_from, p_to, '[)'));
  INSERT INTO dock_events (dock_event_id, dock_id, event_type, during, reason, created_by)
  VALUES (p_event_id, p_dock, p_type, tstzrange(p_from, p_to, '[)'), p_reason, ctx.principal);
  INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
  VALUES (p_dock, fac, tstzrange(p_from, p_to, '[)'), 'BLOCK', p_event_id, ctx.principal)
  RETURNING occupancy_id INTO occ;
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'dock', p_dock, 'CapacityChanged', jsonb_build_object('dock_event_id', p_event_id, 'type', p_type));
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason)
  VALUES (ctx.principal, role, 'BLOCK_DOCK', fac, p_reason);
  RETURN occ;
END $$;

-- ================================================================ ownership and grants
DO $$ DECLARE f text; BEGIN
  FOREACH f IN ARRAY ARRAY[
    'close_approval(uuid,text,text,text,text,text,text,text,text)', 'expire_pending_requests()', 'expire_approval(uuid)',
    'booking_incompatibility(shipments,docks)', 'trg_invalidate_on_shipment()',
    'decide_booking_request(text,text,text,text)', 'reply_superseded_by_takeover(bigint)',
    'claim_notifications(integer)', 'begin_send(uuid,integer)', 'staff_role_for(staff_context,text[],text)',
    'check_principal(staff_context,text)', 'set_automation_paused(text,boolean,text,text)',
    'take_over_thread(text,text,text)', 'hand_back_thread(text,text,text)',
    'block_dock(text,text,timestamptz,timestamptz,text,text,text)'] LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO setuhaul_rpc', f);
  END LOOP;
END $$;

-- the trusted functions above now write these as setuhaul_rpc; the console role writes none of them directly
GRANT EXECUTE ON FUNCTION take_over_thread_v1(text, text, text), hand_back_thread_v1(text, text, text) TO setuhaul_rpc;
GRANT UPDATE (human_active, status) ON chat_threads TO setuhaul_rpc;
GRANT INSERT ON booking_events TO setuhaul_rpc;
REVOKE INSERT ON booking_events, outbox FROM setuhaul_console;
REVOKE UPDATE (human_active, status) ON chat_threads FROM setuhaul_console;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION set_automation_paused(text, boolean, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION take_over_thread(text, text, text)               TO setuhaul_console;
GRANT EXECUTE ON FUNCTION hand_back_thread(text, text, text)               TO setuhaul_console;
GRANT EXECUTE ON FUNCTION begin_send(uuid, integer)                        TO setuhaul_system;
