-- 0007 · Round 3 review (B2-13 P1, P2 hardening). Forward-only; 0001–0006 untouched.
--
-- B2-13  Moving a conversation to another of the driver's shipments changed chat_threads.shipment_id and the flow
--        state without the conversation lock, from a check-then-update in the service. It is now one function,
--        bind_thread_shipment(), that takes the lock, re-reads the thread and decides from DATABASE facts (an active
--        hold or a pending warehouse request for the current shipment blocks the switch; the agent-writable flow row
--        is not trusted). Shipment and flow change in the same transaction; the old options and confirmations are
--        voided. The agent loses direct UPDATE on chat_threads.shipment_id.
-- Same convention extended to release_hold() and hand_back_thread(): every operation that changes a conversation's
-- booking state takes the conversation lock.
-- P2     thread_lock() uses a 64-bit key (hashtextextended) instead of a 32-bit one, so unrelated conversations
--        practically never share a lock.
SET search_path = setuhaul, public;

CREATE OR REPLACE FUNCTION thread_lock(p_thread text) RETURNS void LANGUAGE sql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT pg_advisory_xact_lock(hashtextextended('setuhaul.thread:' || p_thread, 0))
$$;

CREATE FUNCTION bind_thread_shipment(p_thread text, p_shipment text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; prev text;
BEGIN
  PERFORM thread_lock(p_thread);
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;            -- re-read under the lock
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM shipments s WHERE s.shipment_id = p_shipment AND s.driver_id = t.driver_id
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
ALTER FUNCTION bind_thread_shipment(text, text, text) OWNER TO setuhaul_rpc;
GRANT UPDATE (shipment_id) ON chat_threads TO setuhaul_rpc;
GRANT UPDATE (appointment_id) ON thread_flow TO setuhaul_rpc;

-- lock convention for the remaining state-changing operations
ALTER FUNCTION release_hold(uuid, text, text) RENAME TO release_hold_v1;
ALTER FUNCTION hand_back_thread(text, text, text) RENAME TO hand_back_thread_v1;
CREATE FUNCTION release_hold(p_option uuid, p_thread text, p_principal text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM thread_lock(p_thread); RETURN release_hold_v1(p_option, p_thread, p_principal); END $$;
CREATE FUNCTION hand_back_thread(p_thread text, p_principal text, p_summary text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM thread_lock(p_thread); RETURN hand_back_thread_v1(p_thread, p_principal, p_summary); END $$;
ALTER FUNCTION release_hold(uuid, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION hand_back_thread(text, text, text) OWNER TO setuhaul_rpc;

-- the agent can no longer move a conversation between shipments directly
REVOKE UPDATE (shipment_id) ON chat_threads FROM setuhaul_agent;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION release_hold_v1(uuid, text, text) FROM setuhaul_agent;
REVOKE EXECUTE ON FUNCTION hand_back_thread_v1(text, text, text) FROM setuhaul_console;
GRANT EXECUTE ON FUNCTION bind_thread_shipment(text, text, text) TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION release_hold(uuid, text, text) TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION hand_back_thread(text, text, text) TO setuhaul_console;
