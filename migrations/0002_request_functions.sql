-- 0002 · Request and decision functions on the T31 pattern (Build step 1, B-DB-4 / B-DB-5)
--
-- Build invariant (owner's instruction, 17 Nov 2026): the agent is never a privileged database operator.
-- It understands and REQUESTS actions; the application and the database decide whether they are allowed.
-- Concretely:
--   * The agent role can call exactly two write functions here: submit_booking_request and release_hold.
--   * Every decision (confirm, reject, block a dock, pause a facility) belongs to the console role.
--   * Expiry belongs to the system role.
--   * Every function re-checks state, RLS applies inside it (owner setuhaul_rpc is NOBYPASSRLS), and every
--     write is idempotent and audited.
--   * The agent cannot forge a driver's words: it may only insert AGENT messages, and a booking request needs a
--     real DRIVER message, sent after the options were shown, as evidence of the explicit yes.
SET search_path = setuhaul, public;

-- ---------------------------------------------------------------- 1. the agent cannot forge a driver's "yes"
CREATE POLICY p_chat_messages_agent_writes_agent_only ON chat_messages AS RESTRICTIVE FOR INSERT TO setuhaul_agent
  WITH CHECK (sender_type = 'AGENT');
GRANT INSERT ON chat_messages TO setuhaul_system;            -- the inbound webhook ingest writes DRIVER messages

-- one open warehouse request per shipment
CREATE UNIQUE INDEX ux_one_pending_request_per_shipment ON appointments (shipment_id)
  WHERE status = 'PENDING_CONFIRMATION';

-- ---------------------------------------------------------------- 2. internal helpers (no EXECUTE grants)
-- Which configured shift covers a moment, in the facility's local time. NULL outside every shift.
CREATE OR REPLACE FUNCTION facility_shift(p_fac text, p_at timestamptz) RETURNS text LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT s->>'name'
    FROM facility_config c
    JOIN facilities f ON f.facility_id = c.facility_id
    CROSS JOIN LATERAL jsonb_array_elements(c.shifts) s
    CROSS JOIN LATERAL (SELECT (p_at AT TIME ZONE f.timezone)::time AS t,
                               (s->>'start')::time AS a, (s->>'end')::time AS b) x
   WHERE c.facility_id = p_fac AND c.active
     AND CASE WHEN x.a <= x.b THEN x.t >= x.a AND x.t < x.b      -- same-day shift
              ELSE x.t >= x.a OR x.t < x.b END                   -- shift that crosses midnight
   LIMIT 1
$$;

-- Idempotency: the first call with a key does the work; a repeat returns the stored response.
-- The same key with different arguments is refused. Everything runs in the caller's transaction, so a failed
-- call leaves no key behind and can be retried.
CREATE OR REPLACE FUNCTION idem_begin(p_key text, p_request_hash text) RETURNS jsonb LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE k idempotency_keys;
BEGIN
  IF p_key IS NULL OR length(p_key) < 16 THEN
    RAISE EXCEPTION 'IDEMPOTENCY_KEY_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO idempotency_keys (idem_key, request_hash, status) VALUES (p_key, p_request_hash, 'IN_PROGRESS')
  ON CONFLICT (idem_key) DO NOTHING;          -- waits for a concurrent holder of the same key, then sees its row
  IF FOUND THEN RETURN NULL; END IF;
  SELECT * INTO k FROM idempotency_keys WHERE idem_key = p_key;
  IF k.request_hash <> p_request_hash THEN
    RAISE EXCEPTION 'IDEMPOTENCY_KEY_REUSED: the key was used for a different request' USING ERRCODE = 'P0001'; END IF;
  RETURN k.response;
END $$;

CREATE OR REPLACE FUNCTION idem_finish(p_key text, p_response jsonb) RETURNS jsonb LANGUAGE sql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  UPDATE idempotency_keys SET response = p_response, status = 'DONE' WHERE idem_key = p_key RETURNING response
$$;

-- ---------------------------------------------------------------- 3. agent: request a booking (R2 after R1)
-- Converts this thread's live hold into a warehouse request (or a confirmed booking where the facility allows
-- auto-confirmation on the current shift). Returns {status: PENDING | CONFIRMED, appointment_id, ...}.
CREATE OR REPLACE FUNCTION submit_booking_request(p_option uuid, p_thread text, p_confirmation_message bigint,
                                                  p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  o shown_options; t chat_threads; h dock_occupancy; cfg facility_config; cur appointments;
  shift text; ttl integer; auto boolean; exp timestamptz; appt text; resp jsonb; k text := 'submit:' || p_idem_key;
BEGIN
  resp := idem_begin(k, md5(concat_ws('|', p_option, p_thread, p_confirmation_message)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  SELECT * INTO o FROM shown_options WHERE option_id = p_option AND thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPTION_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE: a person has taken over this conversation' USING ERRCODE = 'P0001'; END IF;
  IF t.shipment_id IS DISTINCT FROM o.shipment_id THEN
    RAISE EXCEPTION 'OPTION_NOT_FOR_THREAD' USING ERRCODE = 'P0001'; END IF;

  -- evidence of the explicit yes: a real driver message in this thread, sent after the options were shown
  PERFORM 1 FROM chat_messages m
   WHERE m.message_id = p_confirmation_message AND m.thread_id = p_thread
     AND m.sender_type = 'DRIVER' AND m.created_at >= o.created_at;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_DRIVER_CONFIRMATION' USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO h FROM dock_occupancy
   WHERE shipment_id = o.shipment_id AND kind = 'HOLD' AND active AND ref_id = o.option_id::text
   FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'HOLD_NOT_FOUND' USING ERRCODE = 'P0001'; END IF;
  IF h.expires_at < now() THEN RAISE EXCEPTION 'HOLD_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF (SELECT automation_paused FROM facilities WHERE facility_id = h.facility_id) THEN
    RAISE EXCEPTION 'AUTOMATION_PAUSED' USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO cfg FROM facility_config WHERE facility_id = h.facility_id AND active;
  shift := facility_shift(h.facility_id, now());
  ttl   := (cfg.warehouse_confirm_ttl_min ->> shift)::integer;
  auto  := COALESCE((cfg.auto_confirm_chat_bookings ->> shift)::boolean, false) AND NOT o.needs_approval;
  exp   := LEAST(now() + make_interval(mins => COALESCE(ttl, 0)),
                 lower(o.during) - make_interval(mins => cfg.confirm_lead_min));
  IF NOT auto AND (ttl IS NULL OR ttl = 0 OR exp <= now()) THEN
    -- nobody can confirm in time (e.g. GGN night has no coordinator, A22): a person decides
    RAISE EXCEPTION 'NO_CONFIRMER_AVAILABLE: shift %, ttl %', shift, ttl USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO cur FROM appointments
   WHERE shipment_id = o.shipment_id AND is_current AND status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
   FOR UPDATE;
  IF FOUND AND cur.status = 'IN_PROGRESS' THEN
    RAISE EXCEPTION 'SHIPMENT_IN_DOCK' USING ERRCODE = 'P0001'; END IF;

  appt := 'APT-' || replace(gen_random_uuid()::text, '-', '');
  IF auto THEN
    IF cur.appointment_id IS NOT NULL THEN
      UPDATE appointments SET status = 'CANCELLED', is_current = false, updated_at = now(), row_version = row_version + 1
       WHERE appointment_id = cur.appointment_id;
      UPDATE dock_occupancy SET active = false WHERE occupancy_id = cur.occupancy_id;
    END IF;
    INSERT INTO appointments (appointment_id, shipment_id, dock_id, during, occupancy_id, status, is_current,
                              booking_source, replaced_appointment_id)
    VALUES (appt, o.shipment_id, o.dock_id, o.during, h.occupancy_id, 'CONFIRMED', true, 'DRIVER_CHAT', cur.appointment_id);
  ELSE
    -- the request is not current until the warehouse confirms; the old booking stays valid meanwhile
    INSERT INTO appointments (appointment_id, shipment_id, dock_id, during, occupancy_id, status, is_current,
                              booking_source, pending_expires_at, replaced_appointment_id, needs_approval_reason)
    VALUES (appt, o.shipment_id, o.dock_id, o.during, h.occupancy_id, 'PENDING_CONFIRMATION', false, 'DRIVER_CHAT',
            exp, cur.appointment_id, CASE WHEN o.needs_approval THEN 'AFTER_HOURS_START' END);
  END IF;
  -- the hold becomes the request's capacity claim (same row: no window where the slot is free)
  UPDATE dock_occupancy SET kind = 'APPOINTMENT', expires_at = NULL, ref_id = appt WHERE occupancy_id = h.occupancy_id;

  resp := jsonb_build_object('status', CASE WHEN auto THEN 'CONFIRMED' ELSE 'PENDING' END,
                             'appointment_id', appt, 'shipment_id', o.shipment_id, 'dock_id', o.dock_id,
                             'start', lower(o.during), 'end', upper(o.during),
                             'pending_expires_at', CASE WHEN auto THEN NULL ELSE exp END,
                             'needs_approval', o.needs_approval, 'replaces', cur.appointment_id, 'shift', shift);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (h.facility_id, 'appointment', appt, CASE WHEN auto THEN 'BookingConfirmed' ELSE 'BookingRequested' END, resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (p_principal, 'agent', 'SUBMIT_BOOKING_REQUEST', h.facility_id, o.shipment_id, to_jsonb(cur), resp,
          'driver confirmation message ' || p_confirmation_message);
  RETURN idem_finish(k, resp);
END $$;

-- ---------------------------------------------------------------- 4. agent: release its own hold
CREATE OR REPLACE FUNCTION release_hold(p_option uuid, p_thread text, p_principal text)
RETURNS integer LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o shown_options; n integer;
BEGIN
  SELECT * INTO o FROM shown_options WHERE option_id = p_option AND thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPTION_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  UPDATE dock_occupancy SET active = false
   WHERE active AND kind = 'HOLD' AND shipment_id = o.shipment_id AND ref_id = o.option_id::text;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
    INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, reason)
    SELECT p_principal, 'agent', 'RELEASE_HOLD', d.facility_id, o.shipment_id, 'driver changed their mind'
      FROM docks d WHERE d.dock_id = o.dock_id;
  END IF;
  RETURN n;                                   -- 0 when already released or expired: still a success (idempotent)
END $$;

-- ---------------------------------------------------------------- 5. console: the warehouse decides (T27)
-- Authenticated (console role + facility scope via RLS), expiring (pending_expires_at), idempotent (key),
-- re-validated (capacity claim still active). The console API also re-runs is_feasible before calling.
CREATE OR REPLACE FUNCTION decide_booking_request(p_appointment text, p_decision text, p_idem_key text,
                                                  p_principal text, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a appointments; old appointments; fac text; resp jsonb; k text := 'decide:' || p_idem_key;
BEGIN
  IF p_decision NOT IN ('CONFIRM','REJECT') THEN
    RAISE EXCEPTION 'BAD_DECISION: %', p_decision USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_appointment, p_decision)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;   -- includes other facilities (RLS)
  IF a.status <> 'PENDING_CONFIRMATION' THEN
    RAISE EXCEPTION 'ALREADY_DECIDED: %', a.status USING ERRCODE = 'P0001'; END IF;
  IF a.pending_expires_at <= now() THEN
    RAISE EXCEPTION 'PENDING_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;

  IF p_decision = 'CONFIRM' THEN
    PERFORM 1 FROM dock_occupancy WHERE occupancy_id = a.occupancy_id AND active;
    IF NOT FOUND THEN RAISE EXCEPTION 'CAPACITY_LOST' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO old FROM appointments
     WHERE shipment_id = a.shipment_id AND is_current AND status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
     FOR UPDATE;
    IF FOUND THEN
      IF old.status = 'IN_PROGRESS' THEN RAISE EXCEPTION 'SHIPMENT_IN_DOCK' USING ERRCODE = 'P0001'; END IF;
      UPDATE appointments SET status = 'CANCELLED', is_current = false, updated_at = now(), row_version = row_version + 1
       WHERE appointment_id = old.appointment_id;
      UPDATE dock_occupancy SET active = false WHERE occupancy_id = old.occupancy_id;
    END IF;
    UPDATE appointments SET status = 'CONFIRMED', is_current = true, warehouse_confirmation_ref = p_principal,
                            updated_at = now(), row_version = row_version + 1
     WHERE appointment_id = a.appointment_id;
  ELSE
    UPDATE appointments SET status = 'REJECTED', updated_at = now(), row_version = row_version + 1
     WHERE appointment_id = a.appointment_id;
    UPDATE dock_occupancy SET active = false WHERE occupancy_id = a.occupancy_id;
  END IF;

  resp := jsonb_build_object('status', CASE WHEN p_decision = 'CONFIRM' THEN 'CONFIRMED' ELSE 'REJECTED' END,
                             'appointment_id', a.appointment_id, 'shipment_id', a.shipment_id,
                             'replaced', CASE WHEN p_decision = 'CONFIRM' THEN old.appointment_id END,
                             'decided_by', p_principal);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'appointment', a.appointment_id,
          CASE WHEN p_decision = 'CONFIRM' THEN 'BookingConfirmed' ELSE 'BookingRejected' END, resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (p_principal, 'warehouse', 'DECIDE_BOOKING_REQUEST', fac, a.shipment_id, to_jsonb(a), resp, p_note);
  RETURN idem_finish(k, resp);
END $$;

-- ---------------------------------------------------------------- 6. system: expire unanswered requests (F6)
-- No auto-approval, ever: an unanswered request expires, capacity is released, and a person is told.
CREATE OR REPLACE FUNCTION expire_pending_requests() RETURNS integer LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a appointments; fac text; n integer := 0;
BEGIN
  FOR a IN SELECT * FROM appointments
            WHERE status = 'PENDING_CONFIRMATION' AND pending_expires_at <= now()
            FOR UPDATE SKIP LOCKED LOOP
    SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
    UPDATE appointments SET status = 'EXPIRED', updated_at = now(), row_version = row_version + 1
     WHERE appointment_id = a.appointment_id;
    UPDATE dock_occupancy SET active = false WHERE occupancy_id = a.occupancy_id;
    INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
    VALUES (fac, 'appointment', a.appointment_id, 'BookingExpired',
            jsonb_build_object('appointment_id', a.appointment_id, 'shipment_id', a.shipment_id));
    INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, handoff, ack_due_at)
    VALUES (gen_random_uuid(), 'PENDING_EXPIRED', 'DECIDE', fac, a.shipment_id,
            jsonb_build_object('appointment_id', a.appointment_id, 'expired_at', now(),
                               'still_valid_booking', a.replaced_appointment_id),
            now() + interval '10 minutes');
    INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, reason)
    VALUES ('system:expiry', 'system', 'EXPIRE_PENDING_REQUEST', fac, a.shipment_id, to_jsonb(a), 'warehouse did not answer in time');
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- ---------------------------------------------------------------- 7. console: pause switch and dock blocks
CREATE OR REPLACE FUNCTION set_automation_paused(p_facility text, p_paused boolean, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  -- facilities has no RLS (reference data), so the scope check is explicit here
  IF NOT scope_allows(p_facility) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  UPDATE facilities SET automation_paused = p_paused WHERE facility_id = p_facility;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason, after_state)
  VALUES (p_principal, 'operations', CASE WHEN p_paused THEN 'PAUSE_AUTOMATION' ELSE 'RESUME_AUTOMATION' END,
          p_facility, p_reason, jsonb_build_object('automation_paused', p_paused));
  RETURN p_paused;
END $$;

-- A breakdown / maintenance / manual block. If a booking already occupies the window the exclusion constraint
-- refuses it (23P01): the console must first move or mark those bookings AT_RISK, never silently.
CREATE OR REPLACE FUNCTION block_dock(p_event_id text, p_dock text, p_from timestamptz, p_to timestamptz,
                                      p_type text, p_reason text, p_principal text)
RETURNS bigint LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE fac text; occ bigint;
BEGIN
  IF p_type NOT IN ('MAINTENANCE','BREAKDOWN','MANUAL_BLOCK') THEN
    RAISE EXCEPTION 'BAD_BLOCK_TYPE: %', p_type USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = p_dock;
  IF NOT FOUND OR NOT scope_allows(fac) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dock_events WHERE dock_event_id = p_event_id) THEN       -- idempotent by event id
    SELECT occupancy_id INTO occ FROM dock_occupancy WHERE kind = 'BLOCK' AND ref_id = p_event_id;
    RETURN occ;
  END IF;
  INSERT INTO dock_events (dock_event_id, dock_id, event_type, during, reason, created_by)
  VALUES (p_event_id, p_dock, p_type, tstzrange(p_from, p_to, '[)'), p_reason, p_principal);
  INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
  VALUES (p_dock, fac, tstzrange(p_from, p_to, '[)'), 'BLOCK', p_event_id, p_principal)
  RETURNING occupancy_id INTO occ;
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'dock', p_dock, 'CapacityChanged', jsonb_build_object('dock_event_id', p_event_id, 'type', p_type));
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason)
  VALUES (p_principal, 'operations', 'BLOCK_DOCK', fac, p_reason);
  RETURN occ;
END $$;

-- ---------------------------------------------------------------- 8. ownership and grants (T31)
GRANT SELECT ON appointments, chat_threads, chat_messages, dock_events TO setuhaul_rpc;
GRANT SELECT, INSERT ON idempotency_keys TO setuhaul_rpc;
GRANT UPDATE (response, status) ON idempotency_keys TO setuhaul_rpc;
GRANT INSERT ON appointments, outbox, booking_events, escalations, dock_events TO setuhaul_rpc;
GRANT UPDATE (status, is_current, at_risk, updated_at, row_version, warehouse_confirmation_ref) ON appointments TO setuhaul_rpc;
GRANT UPDATE (kind, expires_at, ref_id) ON dock_occupancy TO setuhaul_rpc;
GRANT UPDATE (automation_paused) ON facilities TO setuhaul_rpc;

ALTER FUNCTION facility_shift(text, timestamptz)                       OWNER TO setuhaul_rpc;
ALTER FUNCTION idem_begin(text, text)                                  OWNER TO setuhaul_rpc;
ALTER FUNCTION idem_finish(text, jsonb)                                OWNER TO setuhaul_rpc;
ALTER FUNCTION submit_booking_request(uuid, text, bigint, text, text) SECURITY DEFINER;
ALTER FUNCTION submit_booking_request(uuid, text, bigint, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION release_hold(uuid, text, text) SECURITY DEFINER;
ALTER FUNCTION release_hold(uuid, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION decide_booking_request(text, text, text, text, text) SECURITY DEFINER;
ALTER FUNCTION decide_booking_request(text, text, text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION expire_pending_requests() SECURITY DEFINER;
ALTER FUNCTION expire_pending_requests() OWNER TO setuhaul_rpc;
ALTER FUNCTION set_automation_paused(text, boolean, text, text) SECURITY DEFINER;
ALTER FUNCTION set_automation_paused(text, boolean, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION block_dock(text, text, timestamptz, timestamptz, text, text, text) SECURITY DEFINER;
ALTER FUNCTION block_dock(text, text, timestamptz, timestamptz, text, text, text) OWNER TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION submit_booking_request(uuid, text, bigint, text, text) TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION release_hold(uuid, text, text)                         TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION decide_booking_request(text, text, text, text, text)   TO setuhaul_console;
GRANT EXECUTE ON FUNCTION set_automation_paused(text, boolean, text, text)       TO setuhaul_console;
GRANT EXECUTE ON FUNCTION block_dock(text, text, timestamptz, timestamptz, text, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION expire_pending_requests()                              TO setuhaul_system;
