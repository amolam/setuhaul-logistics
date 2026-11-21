-- 0010 · Review R0.9 findings B2-16 and B2-17 (gate live booking). Forward-only; 0001–0009 untouched.
--
-- B2-16  bind_thread_shipment() trusted the service to have checked that the conversation belongs to the calling
--        driver. It now checks it itself: the transaction must be a driver session (app.driver_id) and the
--        conversation must belong to that driver. A staff session whose facility scope happens to cover the thread
--        can no longer move it.
-- B2-17  BookingService._shipment() decided whether to switch from a read taken BEFORE the conversation lock, then
--        bound in a second transaction. Two turns could each read the old binding, and a turn could go on to compute
--        options for a shipment the conversation had just left. Now:
--          * resolve_thread_shipment() reads the thread, picks the shipment (requested, else bound, else the driver's
--            only live one) and binds it, all under the conversation lock, and returns the AUTHORITATIVE shipment;
--          * publishing options re-checks the binding under the same lock (trigger on shown_options): options for a
--            shipment the conversation is no longer bound to are refused with STALE_SHIPMENT.
SET search_path = setuhaul, public;

-- ---------------------------------------------------------------- B2-16
CREATE OR REPLACE FUNCTION bind_thread_shipment(p_thread text, p_shipment text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; prev text; me text := NULLIF(current_setting('app.driver_id', true), '');
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'FORBIDDEN: a driver session is required' USING ERRCODE = 'P0001'; END IF;
  PERFORM thread_lock(p_thread);
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;            -- re-read under the lock
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.driver_id IS DISTINCT FROM me THEN                                  -- B2-16: the caller's own conversation only
    RAISE EXCEPTION 'FORBIDDEN: not this driver''s conversation' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM shipments s WHERE s.shipment_id = p_shipment AND s.driver_id = me
                                AND s.current_status NOT IN ('COMPLETED','CANCELLED');
  IF NOT FOUND THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  prev := t.shipment_id;
  IF prev IS NOT DISTINCT FROM p_shipment THEN
    RETURN jsonb_build_object('shipment_id', p_shipment, 'changed', false);
  END IF;
  IF prev IS NOT NULL THEN
    -- the hold / request checks below read facility-scoped rows: refuse rather than miss them
    IF NOT scope_allows((SELECT destination_facility_id FROM shipments WHERE shipment_id = prev)) THEN
      RAISE EXCEPTION 'SCOPE_INCOMPLETE: the previous shipment''s facility must be in scope' USING ERRCODE = 'P0001'; END IF;
    PERFORM 1 FROM dock_occupancy d
     WHERE d.shipment_id = prev AND d.kind = 'HOLD' AND d.active AND d.expires_at > now();
    IF FOUND THEN RAISE EXCEPTION 'FINISH_CURRENT_BOOKING_FIRST: a hold is active' USING ERRCODE = 'P0001'; END IF;
    PERFORM 1 FROM appointments a WHERE a.shipment_id = prev AND a.status = 'PENDING_CONFIRMATION';
    IF FOUND THEN RAISE EXCEPTION 'FINISH_CURRENT_BOOKING_FIRST: a request is with the warehouse' USING ERRCODE = 'P0001'; END IF;
  END IF;
  UPDATE chat_threads SET shipment_id = p_shipment WHERE thread_id = p_thread;
  INSERT INTO thread_flow (thread_id, state) VALUES (p_thread, 'NO_OPTIONS')
  ON CONFLICT (thread_id) DO UPDATE SET state = 'NO_OPTIONS', selected_option = NULL, yes_message_id = NULL,
         hold_option = NULL, attestation_id = NULL, appointment_id = NULL,
         version = thread_flow.version + 1, updated_at = now();
  UPDATE shown_options SET expires_at = least(expires_at, now()) WHERE thread_id = p_thread;
  UPDATE confirmation_attestations SET expires_at = least(expires_at, now()) WHERE thread_id = p_thread;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, reason)
  SELECT p_principal, 'agent', 'BIND_THREAD_SHIPMENT', s.destination_facility_id, p_shipment,
         jsonb_build_object('previous_shipment', prev), 'driver moved the conversation to this shipment'
    FROM shipments s WHERE s.shipment_id = p_shipment;
  RETURN jsonb_build_object('shipment_id', p_shipment, 'changed', true, 'previous', prev);
END $$;

-- ---------------------------------------------------------------- B2-17: resolve + bind in one locked step
CREATE FUNCTION resolve_thread_shipment(p_thread text, p_requested text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; me text := NULLIF(current_setting('app.driver_id', true), '');
        live jsonb; n int; sid text; fac text; b jsonb;
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'FORBIDDEN: a driver session is required' USING ERRCODE = 'P0001'; END IF;
  PERFORM thread_lock(p_thread);
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND OR t.driver_id IS DISTINCT FROM me THEN
    RAISE EXCEPTION 'FORBIDDEN: not this driver''s conversation' USING ERRCODE = 'P0001'; END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_array(shipment_id, destination_facility_id) ORDER BY shipment_id), '[]'::jsonb),
         count(*)
    INTO live, n
    FROM shipments WHERE driver_id = me AND current_status NOT IN ('COMPLETED','CANCELLED');
  sid := coalesce(p_requested, t.shipment_id);
  IF sid IS NULL AND n = 1 THEN sid := live->0->>0; END IF;
  IF sid IS NULL THEN
    RETURN jsonb_build_object('shipment_id', NULL, 'facility_id', NULL, 'changed', false, 'live', live);
  END IF;
  SELECT destination_facility_id INTO fac FROM shipments WHERE shipment_id = sid AND driver_id = me;
  IF NOT FOUND THEN RAISE EXCEPTION 'FORBIDDEN: not this driver''s shipment' USING ERRCODE = 'P0001'; END IF;
  IF sid IS DISTINCT FROM t.shipment_id THEN
    b := bind_thread_shipment(p_thread, sid, p_principal);               -- same lock, already held (re-entrant)
  END IF;
  RETURN jsonb_build_object('shipment_id', sid, 'facility_id', fac, 'changed', coalesce((b->>'changed')::boolean, false),
                            'previous', t.shipment_id, 'live', live);
END $$;

-- ---------------------------------------------------------------- B2-17: re-check the binding when publishing options
CREATE FUNCTION trg_shown_options_bound() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  PERFORM thread_lock(NEW.thread_id);                                     -- serialize with bind / hold / request
  PERFORM 1 FROM chat_threads t WHERE t.thread_id = NEW.thread_id AND t.shipment_id = NEW.shipment_id;
  IF NOT FOUND THEN                                                       -- moved on, or not visible in this scope
    RAISE EXCEPTION 'STALE_SHIPMENT: the conversation is no longer bound to this shipment' USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER shown_options_bound BEFORE INSERT ON shown_options
  FOR EACH ROW EXECUTE FUNCTION trg_shown_options_bound();

ALTER FUNCTION resolve_thread_shipment(text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_shown_options_bound()                 OWNER TO setuhaul_rpc;
GRANT SELECT ON chat_threads TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_thread_shipment(text, text, text) TO setuhaul_agent;
