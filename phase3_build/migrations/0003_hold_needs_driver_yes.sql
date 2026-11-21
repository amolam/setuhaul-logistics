-- 0003 · Build step 2: the hold itself requires the driver's explicit yes; conversation flow state.
--
-- Found while building the API: rule R1 ("hold only after an explicit yes to the read-back") was enforced in the
-- application only. place_hold() checked the option, but not that the driver had said yes. Under the Build
-- invariant (the agent requests, the app and DB decide) the database must refuse a hold without that evidence too.
--   * hold_option(): same evidence rule as submit_booking_request (a real DRIVER message in this thread, sent after
--     the options were shown), human-takeover check, idempotency key; then the existing place_hold() logic.
--   * The agent loses EXECUTE on place_hold(). So does the console (it has no reason to hold slots).
--   * thread_flow: the booking-flow state per thread, readable for audit. The agent may write it, but nothing in it
--     is trusted by the database: hold_option and submit_booking_request re-check the evidence themselves.
SET search_path = setuhaul, public;

CREATE TABLE thread_flow (
  thread_id        text PRIMARY KEY REFERENCES chat_threads,
  state            text NOT NULL DEFAULT 'NO_OPTIONS'
                     CHECK (state IN ('NO_OPTIONS','OPTIONS_SHOWN','SELECTED','CONFIRMED_BY_DRIVER','HELD','SUBMITTED',
                                      'BOOKED','CLOSED','HUMAN')),
  selected_option  uuid,
  yes_message_id   bigint,
  hold_option      uuid,
  appointment_id   text,
  version          integer NOT NULL DEFAULT 1,      -- optimistic concurrency: two turns cannot both advance the flow
  updated_at       timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE thread_flow ENABLE ROW LEVEL SECURITY; ALTER TABLE thread_flow FORCE ROW LEVEL SECURITY;
CREATE POLICY p_thread_flow ON thread_flow
  USING (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = thread_flow.thread_id))
  WITH CHECK (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = thread_flow.thread_id));
GRANT SELECT ON thread_flow TO setuhaul_console, setuhaul_system;
-- the inbound webhook ingest (system role, verified driver id set) opens a thread for a driver's first message
GRANT INSERT ON chat_threads TO setuhaul_system;
GRANT SELECT, INSERT ON thread_flow TO setuhaul_agent;
GRANT UPDATE (state, selected_option, yes_message_id, hold_option, appointment_id, version, updated_at)
  ON thread_flow TO setuhaul_agent;

CREATE OR REPLACE FUNCTION hold_option(p_option uuid, p_thread text, p_confirmation_message bigint,
                                       p_idem_key text, p_principal text, p_ttl interval DEFAULT interval '3 minutes')
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o shown_options; t chat_threads; h record; resp jsonb; k text := 'hold:' || p_idem_key;
BEGIN
  IF p_ttl > interval '10 minutes' OR p_ttl < interval '30 seconds' THEN
    RAISE EXCEPTION 'BAD_TTL' USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_option, p_thread, p_confirmation_message)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  SELECT * INTO o FROM shown_options WHERE option_id = p_option AND thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPTION_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE: a person has taken over this conversation' USING ERRCODE = 'P0001'; END IF;
  IF t.shipment_id IS DISTINCT FROM o.shipment_id THEN RAISE EXCEPTION 'OPTION_NOT_FOR_THREAD' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM chat_messages m
   WHERE m.message_id = p_confirmation_message AND m.thread_id = p_thread
     AND m.sender_type = 'DRIVER' AND m.created_at >= o.created_at;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_DRIVER_CONFIRMATION' USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO h FROM place_hold(p_option, p_thread, p_principal, p_ttl);   -- lock, lazy expiry, exclusion
  resp := jsonb_build_object('status', 'HELD', 'occupancy_id', h.occupancy_id, 'expires_at', h.expires_at,
                             'option_id', p_option);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  SELECT p_principal, 'agent', 'HOLD_OPTION', d.facility_id, o.shipment_id, resp,
         'driver confirmation message ' || p_confirmation_message
    FROM docks d WHERE d.dock_id = o.dock_id;
  RETURN idem_finish(k, resp);
END $$;
ALTER FUNCTION hold_option(uuid, text, bigint, text, text, interval) SECURITY DEFINER;
ALTER FUNCTION hold_option(uuid, text, bigint, text, text, interval) OWNER TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION place_hold(uuid, text, text, interval) FROM setuhaul_agent, setuhaul_console;
GRANT EXECUTE ON FUNCTION hold_option(uuid, text, bigint, text, text, interval) TO setuhaul_agent;
