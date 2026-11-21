-- 0005 · Final step 2 corrections (external review of 0004, 17 Nov 2026). Forward-only; 0001–0004 untouched.
--
-- P0-1  Confirmation freshness. A driver's later message overrides an earlier YES:
--         * only the thread's newest DRIVER message can be attested (a retried older message never wins);
--         * attesting a reply expires every earlier attestation for the same selection;
--         * option_selections.latest_reply_message_id records the newest reply;
--         * verify_attestation() rejects an attestation if ANY later DRIVER message exists in the thread
--           (attested or not, e.g. when its attestation failed) or if it is not the latest reply.
--       Consequence, accepted on purpose: after "yes", any further driver message (even "thanks") needs a fresh yes.
-- P0-2  Separate inbound authority. New NOBYPASSRLS roles:
--         setuhaul_ingest       verified inbound path: find driver and thread, store DRIVER messages, call
--                               attest_confirmation(), keep receipts. Nothing else.
--         setuhaul_feasibility  read the feasibility inputs; its only write is publishing computed options.
--       setuhaul_system loses inbound, option-publishing and attestation duties.
-- P1-1  inbound_receipts: per-message processing record, so a retried webhook resumes an attestation that failed.
-- P1-2  CENTRAL escalations emit a durable outbox event (facility_id NULL, visible only with CENTRAL in scope).
SET search_path = setuhaul, public;

-- ---------------------------------------------------------------- roles
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_ingest')      THEN CREATE ROLE setuhaul_ingest NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_feasibility') THEN CREATE ROLE setuhaul_feasibility NOLOGIN; END IF;
END $$;
ALTER ROLE setuhaul_ingest      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE setuhaul_feasibility NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT USAGE ON SCHEMA setuhaul TO setuhaul_ingest, setuhaul_feasibility;

-- ---------------------------------------------------------------- P1-1 inbound receipts
CREATE TABLE inbound_receipts (
  message_id          bigint PRIMARY KEY REFERENCES chat_messages,
  thread_id           text NOT NULL REFERENCES chat_threads,
  attestation_status  text NOT NULL DEFAULT 'PENDING' CHECK (attestation_status IN ('PENDING','DONE','NOT_NEEDED','FAILED')),
  attempts            integer NOT NULL DEFAULT 0,
  last_error          text,
  enqueued            boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_inbound_receipts_open ON inbound_receipts (created_at) WHERE attestation_status IN ('PENDING','FAILED');
ALTER TABLE inbound_receipts ENABLE ROW LEVEL SECURITY; ALTER TABLE inbound_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY p_inbound_receipts ON inbound_receipts
  USING (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = inbound_receipts.thread_id))
  WITH CHECK (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = inbound_receipts.thread_id));

-- ---------------------------------------------------------------- P0-1 freshness
ALTER TABLE option_selections ADD COLUMN latest_reply_message_id bigint REFERENCES chat_messages;
GRANT UPDATE (latest_reply_message_id) ON option_selections TO setuhaul_rpc;

-- CREATE OR REPLACE resets the security attribute, so SECURITY DEFINER is stated again; owner and grants are kept.
CREATE OR REPLACE FUNCTION attest_confirmation(p_thread text, p_message bigint, p_verdict text, p_classifier text,
                                               p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE s option_selections; t chat_threads; oexp timestamptz; a confirmation_attestations;
BEGIN
  IF p_verdict NOT IN ('YES','NO','UNCLEAR') THEN RAISE EXCEPTION 'BAD_VERDICT' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM option_selections WHERE thread_id = p_thread ORDER BY selection_id DESC LIMIT 1;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RETURN NULL; END IF;
  PERFORM 1 FROM chat_messages m
   WHERE m.message_id = p_message AND m.thread_id = p_thread AND m.sender_type = 'DRIVER'
     AND m.message_id > s.read_back_message_id;
  IF NOT FOUND THEN RETURN NULL; END IF;                           -- not a reply to the read-back
  -- only the newest driver message speaks for the driver; a late retry of an older one never overrides it
  PERFORM 1 FROM chat_messages m2
   WHERE m2.thread_id = p_thread AND m2.sender_type = 'DRIVER' AND m2.message_id > p_message;
  IF FOUND THEN RETURN NULL; END IF;
  SELECT expires_at INTO oexp FROM shown_options WHERE option_id = s.option_id;
  IF oexp < now() THEN RETURN NULL; END IF;
  INSERT INTO confirmation_attestations (attestation_id, selection_id, thread_id, option_id, confirmation_message_id,
                                         verdict, classifier_version, attested_by, expires_at)
  VALUES (gen_random_uuid(), s.selection_id, p_thread, s.option_id, p_message, p_verdict, p_classifier, p_principal,
          now() + interval '10 minutes')
  ON CONFLICT (selection_id, confirmation_message_id) DO NOTHING;
  -- a newer reply supersedes every earlier attestation for this selection (YES → NO, YES → UNCLEAR, …)
  UPDATE confirmation_attestations SET expires_at = least(expires_at, now())
   WHERE selection_id = s.selection_id AND confirmation_message_id < p_message;
  UPDATE option_selections SET latest_reply_message_id = greatest(coalesce(latest_reply_message_id, 0), p_message)
   WHERE selection_id = s.selection_id;
  SELECT * INTO a FROM confirmation_attestations WHERE selection_id = s.selection_id AND confirmation_message_id = p_message;
  RETURN jsonb_build_object('attestation_id', a.attestation_id, 'verdict', a.verdict, 'option_id', a.option_id,
                            'selection_id', a.selection_id, 'confirmation_message_id', a.confirmation_message_id);
END $$;

CREATE OR REPLACE FUNCTION verify_attestation(p_option uuid, p_thread text, p_attestation uuid)
RETURNS bigint LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a confirmation_attestations; latest bigint;
BEGIN
  SELECT * INTO a FROM confirmation_attestations WHERE attestation_id = p_attestation;
  IF NOT FOUND OR a.thread_id <> p_thread OR a.option_id <> p_option THEN
    RAISE EXCEPTION 'ATTESTATION_INVALID' USING ERRCODE = 'P0001'; END IF;
  IF a.verdict <> 'YES' THEN RAISE EXCEPTION 'NO_DRIVER_CONFIRMATION' USING ERRCODE = 'P0001'; END IF;
  IF a.selection_id <> (SELECT max(selection_id) FROM option_selections WHERE thread_id = p_thread) THEN
    RAISE EXCEPTION 'SELECTION_CHANGED' USING ERRCODE = 'P0001'; END IF;
  -- freshness: the driver has said nothing since this YES (attested or not)
  PERFORM 1 FROM chat_messages m
   WHERE m.thread_id = p_thread AND m.sender_type = 'DRIVER' AND m.message_id > a.confirmation_message_id;
  IF FOUND THEN RAISE EXCEPTION 'CONFIRMATION_SUPERSEDED' USING ERRCODE = 'P0001'; END IF;
  SELECT latest_reply_message_id INTO latest FROM option_selections WHERE selection_id = a.selection_id;
  IF latest IS DISTINCT FROM a.confirmation_message_id THEN
    RAISE EXCEPTION 'CONFIRMATION_SUPERSEDED' USING ERRCODE = 'P0001'; END IF;
  IF a.expires_at < now() THEN RAISE EXCEPTION 'ATTESTATION_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  RETURN a.confirmation_message_id;
END $$;

-- ---------------------------------------------------------------- P1-2 central escalation events
ALTER TABLE outbox ALTER COLUMN facility_id DROP NOT NULL;
CREATE POLICY p_outbox_central ON outbox FOR SELECT USING (facility_id IS NULL AND scope_allows('CENTRAL'));
CREATE POLICY p_outbox_central_insert ON outbox FOR INSERT WITH CHECK (facility_id IS NULL);
CREATE POLICY p_outbox_central_update ON outbox FOR UPDATE
  USING (facility_id IS NULL AND scope_allows('CENTRAL')) WITH CHECK (facility_id IS NULL AND scope_allows('CENTRAL'));

CREATE OR REPLACE FUNCTION escalate_thread(p_thread text, p_reason text, p_mode text, p_driver_last text, p_told text,
                                           p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; s shipments; fac text; eid uuid; resp jsonb; k text := 'esc:' || p_idem_key; h jsonb;
BEGIN
  IF p_mode NOT IN ('DECIDE','TAKE_OVER') THEN RAISE EXCEPTION 'BAD_MODE' USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_thread, p_reason, p_mode)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = t.shipment_id;
  fac := s.destination_facility_id;                                  -- NULL → central queue, never a default facility
  SELECT escalation_id INTO eid FROM escalations
   WHERE thread_id = p_thread AND reason_code = p_reason AND status IN ('OPEN','ACKNOWLEDGED') LIMIT 1;
  IF eid IS NULL THEN
    eid := gen_random_uuid();
    h := jsonb_build_object('thread_id', p_thread, 'driver_id', t.driver_id, 'shipment_id', t.shipment_id,
                            'reason', p_reason, 'mode', p_mode, 'priority', s.priority_code,
                            'driver_last_message', left(p_driver_last, 500), 'what_driver_was_told', left(p_told, 500),
                            'thread_status', t.status, 'built_by', 'database', 'queue', COALESCE(fac, 'CENTRAL'),
                            'ack_sla_min', CASE WHEN p_reason = 'SAFETY' THEN 5 ELSE 10 END);
    INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, thread_id, priority_code, handoff, ack_due_at)
    VALUES (eid, p_reason, p_mode, fac, t.shipment_id, p_thread, s.priority_code, h,
            now() + CASE WHEN p_reason = 'SAFETY' THEN interval '5 minutes' ELSE interval '10 minutes' END);
    INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
    VALUES (p_principal, 'agent', 'ESCALATE', fac, t.shipment_id, h, p_reason);
    -- every escalation, facility or CENTRAL, gets a durable event for the notification relay
    INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
    VALUES (fac, 'escalation', eid::text, 'EscalationOpened', h);
  END IF;
  IF p_mode = 'TAKE_OVER' THEN
    UPDATE chat_threads SET human_active = true, status = 'WAITING_FOR_HUMAN' WHERE thread_id = p_thread;
    INSERT INTO thread_flow (thread_id, state) VALUES (p_thread, 'HUMAN')
    ON CONFLICT (thread_id) DO UPDATE SET state = 'HUMAN', version = thread_flow.version + 1, updated_at = now();
    UPDATE confirmation_attestations SET expires_at = now() WHERE thread_id = p_thread AND expires_at > now();
  ELSE
    UPDATE chat_threads SET status = 'ESCALATED' WHERE thread_id = p_thread;
  END IF;
  resp := jsonb_build_object('escalation_id', eid, 'queue', COALESCE(fac, 'CENTRAL'),
                             'mode', p_mode, 'human_active', p_mode = 'TAKE_OVER');
  RETURN idem_finish(k, resp);
END $$;

-- ---------------------------------------------------------------- P0-2 grants
-- ingest: exactly what the verified inbound path needs
GRANT SELECT ON drivers, shipments, chat_threads, chat_messages TO setuhaul_ingest;
GRANT INSERT ON chat_threads, chat_messages TO setuhaul_ingest;
GRANT SELECT, INSERT ON inbound_receipts TO setuhaul_ingest;
GRANT UPDATE (attestation_status, attempts, last_error, enqueued, updated_at) ON inbound_receipts TO setuhaul_ingest;
CREATE POLICY p_chat_messages_ingest_driver_only ON chat_messages AS RESTRICTIVE FOR INSERT TO setuhaul_ingest
  WITH CHECK (sender_type = 'DRIVER');
-- feasibility: read the inputs; publish computed options
GRANT SELECT ON shipments, docks, facilities, facility_config, appointment_slots, appointments, dock_events,
                dock_occupancy, eta_signals TO setuhaul_feasibility;
GRANT INSERT ON shown_options TO setuhaul_feasibility;
-- system: no longer the inbound path, the option publisher or the attester
REVOKE INSERT ON chat_threads, chat_messages, shown_options FROM setuhaul_system;
GRANT SELECT ON inbound_receipts TO setuhaul_system, setuhaul_console;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION attest_confirmation(text, bigint, text, text, text) FROM setuhaul_system;
GRANT EXECUTE ON FUNCTION attest_confirmation(text, bigint, text, text, text) TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION scope_allows(text), driver_or_staff(text, text) TO setuhaul_ingest, setuhaul_feasibility;
