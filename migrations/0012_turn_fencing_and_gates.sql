-- 0012 · Review R1.9 (Build step 4, round 1). Forward-only; 0001–0011 untouched.
--
-- S4-02/S4-03  Processing lease fencing. Every write a turn makes (agent and feasibility transactions) first calls
--              assert_turn_fence(): under the conversation lock it checks that this worker still owns a live lease
--              for the message's CURRENT generation. Claiming, renewing and the generation bump take the same lock, so
--              a worker whose lease expired, was taken over, or whose message got a new generation cannot commit any
--              further effect of its turn.
-- S4-08        One reply per inbound message (and generation): chat_messages.reply_to / reply_generation, unique for
--              AGENT rows. A turn re-run after a crash between storing the reply and finishing returns the stored reply.
-- S4-07        bind_thread_shipment() records the principal from the session (driver:<app.driver_id>), not the
--              caller's string; a different claimed principal is kept beside it in the audit row.
-- S4-05        deployment_gates: evidence that a deployment-level check was verified (queue, Aurora, recovery worker,
--              booking workflow). The application refuses to start with the booking switch on unless all are present.
--              No application role can write them; the deployment pipeline does, with its own credentials.
SET search_path = setuhaul, public;

-- ---------------------------------------------------------------- S4-02/S4-03 fencing
CREATE FUNCTION assert_turn_fence(p_message bigint, p_owner text, p_generation integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r inbound_receipts;
BEGIN
  SELECT * INTO r FROM inbound_receipts WHERE message_id = p_message;
  IF NOT FOUND THEN RAISE EXCEPTION 'TURN_FENCED: unknown message' USING ERRCODE = 'P0001'; END IF;
  PERFORM thread_lock(r.thread_id);                                       -- same lock as claim / renew / generation bump
  SELECT * INTO r FROM inbound_receipts WHERE message_id = p_message;    -- re-read under the lock
  IF r.processing_owner IS DISTINCT FROM p_owner OR r.generation <> p_generation OR r.processed_at IS NOT NULL
     OR r.processing_until IS NULL OR r.processing_until <= clock_timestamp() THEN
    RAISE EXCEPTION 'TURN_FENCED: this worker no longer holds the turn for message %', p_message USING ERRCODE = 'P0001';
  END IF;
END $$;
ALTER FUNCTION assert_turn_fence(bigint, text, integer) OWNER TO setuhaul_rpc;
GRANT SELECT ON inbound_receipts TO setuhaul_rpc;

-- ---------------------------------------------------------------- S4-08 reply idempotency
ALTER TABLE chat_messages ADD COLUMN reply_to bigint REFERENCES chat_messages, ADD COLUMN reply_generation integer;
CREATE UNIQUE INDEX ux_agent_reply_per_message ON chat_messages (reply_to, reply_generation)
  WHERE sender_type = 'AGENT' AND reply_to IS NOT NULL;

-- ---------------------------------------------------------------- S4-07 audit principal from the session
CREATE OR REPLACE FUNCTION bind_thread_shipment(p_thread text, p_shipment text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; prev text; me text := NULLIF(current_setting('app.driver_id', true), ''); who text;
BEGIN
  IF me IS NULL THEN RAISE EXCEPTION 'FORBIDDEN: a driver session is required' USING ERRCODE = 'P0001'; END IF;
  who := 'driver:' || me;                                                  -- S4-07: from the session, not the caller
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
  SELECT who, 'agent', 'BIND_THREAD_SHIPMENT', s.destination_facility_id, p_shipment,
         jsonb_build_object('previous_shipment', prev)
           || CASE WHEN p_principal IS DISTINCT FROM who THEN jsonb_build_object('claimed_principal', p_principal)
                   ELSE '{}'::jsonb END,
         'driver moved the conversation to this shipment'
    FROM shipments s WHERE s.shipment_id = p_shipment;
  RETURN jsonb_build_object('shipment_id', p_shipment, 'changed', true, 'previous', prev);
END $$;

-- ---------------------------------------------------------------- S4-05 deployment gates
CREATE TABLE deployment_gates (
  gate         text PRIMARY KEY CHECK (gate IN ('AWS_QUEUE','AURORA','RECOVERY_WORKER','BOOKING_WORKFLOW')),
  verified_at  timestamptz NOT NULL DEFAULT now(),
  verified_by  text NOT NULL,
  evidence     text NOT NULL CHECK (length(evidence) >= 10)       -- a test run id, report path or ticket
);
GRANT SELECT ON deployment_gates TO setuhaul_system;                 -- read at start-up; no application role writes

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION assert_turn_fence(bigint, text, integer) TO setuhaul_agent, setuhaul_feasibility;
