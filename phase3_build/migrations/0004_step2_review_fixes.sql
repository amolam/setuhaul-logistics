-- 0004 · Step 2 review corrections (internal review + external review, 17 Nov 2026). Forward-only.
--
-- P0-1  Database-enforced driver confirmation. A selection records a read-back message (written by the database,
--       so it cannot be backdated). The trusted inbound path classifies the driver's next stored message and writes
--       a confirmation ATTESTATION. The agent role can neither write nor change attestations. hold_option() and
--       submit_booking_request() now take an attestation and verify: same thread, same option, verdict YES, not
--       expired, and the selection it confirms is still the thread's latest selection.
-- P0-2  ETA provenance. record_driver_eta() requires a DRIVER message in the caller's thread, and the shipment must
--       be the thread's shipment and belong to the thread's driver. The agent loses direct INSERT on eta_signals and
--       driver_exceptions. One ETA per driver message (natural idempotency) plus an idempotency key.
-- P0-3  Real human takeover. escalate_thread(TAKE_OVER) and take_over_thread() atomically set human_active, the
--       thread status and the HUMAN flow state, and expire the thread's attestations. Every agent function refuses
--       while a person owns the thread.
-- P1-4  Escalations with no known shipment go to an explicit CENTRAL queue (facility_id NULL), visible only to
--       callers whose scope includes CENTRAL. No default facility.
-- P1-5  ETA recording and escalation are idempotent.
-- Also: the agent can no longer insert options, audit rows, outbox events, idempotency keys, threads or
--       escalations directly; those are written only by functions or by the trusted service/system paths.
SET search_path = setuhaul, public;

-- ---------------------------------------------------------------- A. selection, read-back, attestation
CREATE TABLE option_selections (
  selection_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  thread_id             text NOT NULL REFERENCES chat_threads,
  option_id             uuid NOT NULL REFERENCES shown_options,
  read_back_message_id  bigint NOT NULL REFERENCES chat_messages,
  selected_at           timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_selections_thread ON option_selections (thread_id, selection_id DESC);

CREATE TABLE confirmation_attestations (
  attestation_id           uuid PRIMARY KEY,
  selection_id             bigint NOT NULL REFERENCES option_selections,
  thread_id                text NOT NULL REFERENCES chat_threads,
  option_id                uuid NOT NULL REFERENCES shown_options,
  confirmation_message_id  bigint NOT NULL REFERENCES chat_messages,
  verdict                  text NOT NULL CHECK (verdict IN ('YES','NO','UNCLEAR')),
  classifier_version       text NOT NULL,
  attested_by              text NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at               timestamptz NOT NULL,
  UNIQUE (selection_id, confirmation_message_id)
);

ALTER TABLE option_selections ENABLE ROW LEVEL SECURITY; ALTER TABLE option_selections FORCE ROW LEVEL SECURITY;
CREATE POLICY p_option_selections ON option_selections
  USING (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = option_selections.thread_id))
  WITH CHECK (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = option_selections.thread_id));
ALTER TABLE confirmation_attestations ENABLE ROW LEVEL SECURITY; ALTER TABLE confirmation_attestations FORCE ROW LEVEL SECURITY;
CREATE POLICY p_confirmation_attestations ON confirmation_attestations
  USING (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = confirmation_attestations.thread_id))
  WITH CHECK (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = confirmation_attestations.thread_id));

ALTER TABLE thread_flow ADD COLUMN attestation_id uuid;

-- ---------------------------------------------------------------- B. central queue for unassigned escalations
ALTER TABLE escalations ALTER COLUMN facility_id DROP NOT NULL;
CREATE POLICY p_escalations_central ON escalations
  USING (facility_id IS NULL AND scope_allows('CENTRAL'))
  WITH CHECK (facility_id IS NULL AND scope_allows('CENTRAL'));
CREATE POLICY p_escalations_central_insert ON escalations FOR INSERT
  WITH CHECK (facility_id IS NULL);                      -- anyone allowed to escalate may hand over to central ops
CREATE POLICY p_booking_events_central ON booking_events FOR SELECT
  USING (facility_id IS NULL AND scope_allows('CENTRAL'));
CREATE POLICY p_booking_events_central_insert ON booking_events FOR INSERT
  WITH CHECK (facility_id IS NULL);
CREATE POLICY p_chat_threads_central ON chat_threads
  USING (shipment_id IS NULL AND scope_allows('CENTRAL'))
  WITH CHECK (shipment_id IS NULL AND scope_allows('CENTRAL'));

-- one ETA per driver message
CREATE UNIQUE INDEX ux_eta_driver_message ON eta_signals (shipment_id, raw_ref)
  WHERE source = 'DRIVER_DECLARED' AND raw_ref IS NOT NULL;

-- ---------------------------------------------------------------- C. functions
-- The database writes the read-back message itself, so its id and time are trustworthy.
CREATE OR REPLACE FUNCTION record_read_back(p_option uuid, p_thread text, p_text text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o shown_options; t chat_threads; mid bigint; sid bigint;
BEGIN
  IF p_text IS NULL OR length(p_text) NOT BETWEEN 10 AND 1000 THEN
    RAISE EXCEPTION 'BAD_READ_BACK_TEXT' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO o FROM shown_options WHERE option_id = p_option AND thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPTION_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF o.expires_at < now() THEN RAISE EXCEPTION 'OPTION_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE' USING ERRCODE = 'P0001'; END IF;
  IF t.shipment_id IS DISTINCT FROM o.shipment_id THEN RAISE EXCEPTION 'OPTION_NOT_FOR_THREAD' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO chat_messages (thread_id, sender_type, channel, content_hash, body_masked)
  VALUES (p_thread, 'AGENT', 'WHATSAPP', md5(p_text || clock_timestamp()::text), p_text)
  RETURNING message_id INTO mid;
  INSERT INTO option_selections (thread_id, option_id, read_back_message_id) VALUES (p_thread, p_option, mid)
  RETURNING selection_id INTO sid;
  RETURN jsonb_build_object('selection_id', sid, 'read_back_message_id', mid, 'option_id', p_option);
END $$;

-- Trusted path only (inbound webhook / system role): classify result for the driver's reply to the latest read-back.
-- Returns NULL when there is no read-back waiting for an answer.
CREATE OR REPLACE FUNCTION attest_confirmation(p_thread text, p_message bigint, p_verdict text, p_classifier text,
                                               p_principal text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE s option_selections; t chat_threads; oexp timestamptz; a confirmation_attestations;
BEGIN
  IF p_verdict NOT IN ('YES','NO','UNCLEAR') THEN RAISE EXCEPTION 'BAD_VERDICT' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM option_selections WHERE thread_id = p_thread ORDER BY selection_id DESC LIMIT 1;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RETURN NULL; END IF;                       -- a person owns it: no automated confirmations
  PERFORM 1 FROM chat_messages m
   WHERE m.message_id = p_message AND m.thread_id = p_thread AND m.sender_type = 'DRIVER'
     AND m.message_id > s.read_back_message_id;
  IF NOT FOUND THEN RETURN NULL; END IF;                           -- not a reply to the read-back
  SELECT expires_at INTO oexp FROM shown_options WHERE option_id = s.option_id;
  IF oexp < now() THEN RETURN NULL; END IF;
  INSERT INTO confirmation_attestations (attestation_id, selection_id, thread_id, option_id, confirmation_message_id,
                                         verdict, classifier_version, attested_by, expires_at)
  VALUES (gen_random_uuid(), s.selection_id, p_thread, s.option_id, p_message, p_verdict, p_classifier, p_principal,
          now() + interval '10 minutes')
  ON CONFLICT (selection_id, confirmation_message_id) DO NOTHING;
  SELECT * INTO a FROM confirmation_attestations WHERE selection_id = s.selection_id AND confirmation_message_id = p_message;
  RETURN jsonb_build_object('attestation_id', a.attestation_id, 'verdict', a.verdict, 'option_id', a.option_id,
                            'selection_id', a.selection_id, 'confirmation_message_id', a.confirmation_message_id);
END $$;

-- internal: returns the confirming message id, or raises
CREATE OR REPLACE FUNCTION verify_attestation(p_option uuid, p_thread text, p_attestation uuid)
RETURNS bigint LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a confirmation_attestations;
BEGIN
  SELECT * INTO a FROM confirmation_attestations WHERE attestation_id = p_attestation;
  IF NOT FOUND OR a.thread_id <> p_thread OR a.option_id <> p_option THEN
    RAISE EXCEPTION 'ATTESTATION_INVALID' USING ERRCODE = 'P0001'; END IF;
  IF a.verdict <> 'YES' THEN RAISE EXCEPTION 'NO_DRIVER_CONFIRMATION' USING ERRCODE = 'P0001'; END IF;
  IF a.expires_at < now() THEN RAISE EXCEPTION 'ATTESTATION_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF a.selection_id <> (SELECT max(selection_id) FROM option_selections WHERE thread_id = p_thread) THEN
    RAISE EXCEPTION 'SELECTION_CHANGED' USING ERRCODE = 'P0001'; END IF;
  RETURN a.confirmation_message_id;
END $$;

ALTER FUNCTION hold_option(uuid, text, bigint, text, text, interval) RENAME TO hold_option_core;
ALTER FUNCTION submit_booking_request(uuid, text, bigint, text, text) RENAME TO submit_booking_request_core;

CREATE OR REPLACE FUNCTION hold_option(p_option uuid, p_thread text, p_attestation uuid, p_idem_key text,
                                       p_principal text, p_ttl interval DEFAULT interval '3 minutes')
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  RETURN hold_option_core(p_option, p_thread, verify_attestation(p_option, p_thread, p_attestation),
                          p_idem_key, p_principal, p_ttl);
END $$;

CREATE OR REPLACE FUNCTION submit_booking_request(p_option uuid, p_thread text, p_attestation uuid, p_idem_key text,
                                                  p_principal text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  RETURN submit_booking_request_core(p_option, p_thread, verify_attestation(p_option, p_thread, p_attestation),
                                     p_idem_key, p_principal);
END $$;

-- P0-2: ETA provenance
CREATE OR REPLACE FUNCTION record_driver_eta(p_thread text, p_message bigint, p_shipment text, p_eta timestamptz,
                                             p_precision text, p_reason text, p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; resp jsonb; k text := 'eta:' || p_idem_key; loc timestamp; dk text; fresh boolean := false;
BEGIN
  resp := idem_begin(k, md5(concat_ws('|', p_thread, p_message, p_shipment, p_eta, p_precision, p_reason)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE' USING ERRCODE = 'P0001'; END IF;
  IF p_shipment IS NULL OR p_shipment IS DISTINCT FROM t.shipment_id THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_IN_THREAD' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM shipments s WHERE s.shipment_id = p_shipment AND s.driver_id = t.driver_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM chat_messages m WHERE m.message_id = p_message AND m.thread_id = p_thread AND m.sender_type = 'DRIVER';
  IF NOT FOUND THEN RAISE EXCEPTION 'MESSAGE_NOT_FROM_DRIVER' USING ERRCODE = 'P0001'; END IF;
  IF p_eta IS NOT NULL AND (p_eta < now() - interval '12 hours' OR p_eta > now() + interval '7 days') THEN
    RAISE EXCEPTION 'ETA_OUT_OF_RANGE' USING ERRCODE = 'P0001'; END IF;
  IF p_eta IS NOT NULL THEN
    INSERT INTO eta_signals (shipment_id, source, observed_at, derived_eta, uncertainty_min, confidence, raw_ref)
    VALUES (p_shipment, 'DRIVER_DECLARED', now(), p_eta,
            CASE WHEN p_precision = 'PRECISE' THEN 0 ELSE 30 END,
            CASE WHEN p_precision = 'PRECISE' THEN 'MEDIUM' ELSE 'LOW' END, 'msg:' || p_message)
    ON CONFLICT (shipment_id, raw_ref) WHERE source = 'DRIVER_DECLARED' AND raw_ref IS NOT NULL DO NOTHING;
    fresh := FOUND;
  END IF;
  IF p_eta IS NOT NULL OR p_reason IS NOT NULL THEN
    loc := p_eta AT TIME ZONE 'Asia/Kolkata';
    dk := concat_ws('-', t.driver_id, p_shipment, COALESCE(p_reason, 'DELAY'),
                    CASE WHEN loc IS NULL THEN NULL
                         ELSE to_char(loc, 'YYYYMMDDHH24') || lpad(((extract(minute FROM loc)::int / 15) * 15)::text, 2, '0') END);
    INSERT INTO driver_exceptions VALUES ('EXC-' || substr(md5(random()::text), 1, 12), p_shipment, t.driver_id, p_thread,
                                          COALESCE(p_reason, 'DELAY'), 'OPEN', dk, now())
    ON CONFLICT (dedupe_key) DO NOTHING;
  END IF;
  resp := jsonb_build_object('recorded', true, 'new_signal', fresh, 'shipment_id', p_shipment, 'message_id', p_message);
  RETURN idem_finish(k, resp);
END $$;

-- P0-3 / P1-4 / P1-5: durable, idempotent escalation; TAKE_OVER hands the thread to a person atomically
CREATE OR REPLACE FUNCTION escalate_thread(p_thread text, p_reason text, p_mode text, p_driver_last text, p_told text,
                                           p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
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
  -- one open escalation per thread and reason
  SELECT escalation_id INTO eid FROM escalations
   WHERE thread_id = p_thread AND reason_code = p_reason AND status IN ('OPEN','ACKNOWLEDGED') LIMIT 1;
  IF eid IS NULL THEN
    eid := gen_random_uuid();
    h := jsonb_build_object('thread_id', p_thread, 'driver_id', t.driver_id, 'shipment_id', t.shipment_id,
                            'reason', p_reason, 'mode', p_mode, 'priority', s.priority_code,
                            'driver_last_message', left(p_driver_last, 500), 'what_driver_was_told', left(p_told, 500),
                            'thread_status', t.status, 'built_by', 'database', 'queue', CASE WHEN fac IS NULL THEN 'CENTRAL' ELSE fac END);
    INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, thread_id, priority_code, handoff, ack_due_at)
    VALUES (eid, p_reason, p_mode, fac, t.shipment_id, p_thread, s.priority_code, h,
            now() + CASE WHEN p_reason = 'SAFETY' THEN interval '5 minutes' ELSE interval '10 minutes' END);
    INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
    VALUES (p_principal, 'agent', 'ESCALATE', fac, t.shipment_id, h, p_reason);
    IF fac IS NOT NULL THEN
      INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
      VALUES (fac, 'escalation', eid::text, 'EscalationOpened', h);
    END IF;
  END IF;
  IF p_mode = 'TAKE_OVER' THEN
    UPDATE chat_threads SET human_active = true, status = 'WAITING_FOR_HUMAN' WHERE thread_id = p_thread;
    INSERT INTO thread_flow (thread_id, state) VALUES (p_thread, 'HUMAN')
    ON CONFLICT (thread_id) DO UPDATE SET state = 'HUMAN', version = thread_flow.version + 1, updated_at = now();
    UPDATE confirmation_attestations SET expires_at = now() WHERE thread_id = p_thread AND expires_at > now();
  ELSE
    UPDATE chat_threads SET status = 'ESCALATED' WHERE thread_id = p_thread;
  END IF;
  resp := jsonb_build_object('escalation_id', eid, 'queue', CASE WHEN fac IS NULL THEN 'CENTRAL' ELSE fac END,
                             'mode', p_mode, 'human_active', p_mode = 'TAKE_OVER');
  RETURN idem_finish(k, resp);
END $$;

CREATE OR REPLACE FUNCTION take_over_thread(p_thread text, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer; fac text; sid text;
BEGIN
  UPDATE chat_threads SET human_active = true, status = 'WAITING_FOR_HUMAN' WHERE thread_id = p_thread
  RETURNING shipment_id INTO sid;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO thread_flow (thread_id, state) VALUES (p_thread, 'HUMAN')
  ON CONFLICT (thread_id) DO UPDATE SET state = 'HUMAN', version = thread_flow.version + 1, updated_at = now();
  UPDATE confirmation_attestations SET expires_at = now() WHERE thread_id = p_thread AND expires_at > now();
  SELECT destination_facility_id INTO fac FROM shipments WHERE shipment_id = sid;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, reason)
  VALUES (p_principal, 'operations', 'TAKE_OVER_THREAD', fac, sid, p_reason);
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION hand_back_thread(p_thread text, p_principal text, p_summary text)
RETURNS boolean LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer; fac text; sid text;
BEGIN
  IF p_summary IS NULL OR length(p_summary) < 10 THEN
    RAISE EXCEPTION 'HANDBACK_SUMMARY_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  UPDATE chat_threads SET human_active = false, status = 'OPEN' WHERE thread_id = p_thread AND human_active
  RETURNING shipment_id INTO sid;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN RAISE EXCEPTION 'THREAD_NOT_IN_HUMAN_HANDS' USING ERRCODE = 'P0001'; END IF;
  -- the agent starts clean: no selection or confirmation from before the takeover can be reused
  UPDATE thread_flow SET state = 'NO_OPTIONS', selected_option = NULL, yes_message_id = NULL, hold_option = NULL,
                         attestation_id = NULL, version = version + 1, updated_at = now() WHERE thread_id = p_thread;
  UPDATE shown_options SET expires_at = least(expires_at, now()) WHERE thread_id = p_thread;
  SELECT destination_facility_id INTO fac FROM shipments WHERE shipment_id = sid;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, reason)
  VALUES (p_principal, 'operations', 'HAND_BACK_THREAD', fac, sid, p_summary);
  RETURN true;
END $$;

-- ---------------------------------------------------------------- D. ownership
ALTER FUNCTION record_read_back(uuid, text, text, text)                                OWNER TO setuhaul_rpc;
ALTER FUNCTION attest_confirmation(text, bigint, text, text, text)                     OWNER TO setuhaul_rpc;
ALTER FUNCTION verify_attestation(uuid, text, uuid)                                    OWNER TO setuhaul_rpc;
ALTER FUNCTION hold_option(uuid, text, uuid, text, text, interval)                     OWNER TO setuhaul_rpc;
ALTER FUNCTION submit_booking_request(uuid, text, uuid, text, text)                    OWNER TO setuhaul_rpc;
ALTER FUNCTION record_driver_eta(text, bigint, text, timestamptz, text, text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION escalate_thread(text, text, text, text, text, text, text)               OWNER TO setuhaul_rpc;
ALTER FUNCTION take_over_thread(text, text, text)                                      OWNER TO setuhaul_rpc;
ALTER FUNCTION hand_back_thread(text, text, text)                                      OWNER TO setuhaul_rpc;
ALTER FUNCTION record_read_back(uuid, text, text, text)                                SECURITY DEFINER;
ALTER FUNCTION attest_confirmation(text, bigint, text, text, text)                     SECURITY DEFINER;
ALTER FUNCTION hold_option(uuid, text, uuid, text, text, interval)                     SECURITY DEFINER;
ALTER FUNCTION submit_booking_request(uuid, text, uuid, text, text)                    SECURITY DEFINER;
ALTER FUNCTION record_driver_eta(text, bigint, text, timestamptz, text, text, text, text) SECURITY DEFINER;
ALTER FUNCTION escalate_thread(text, text, text, text, text, text, text)               SECURITY DEFINER;
ALTER FUNCTION take_over_thread(text, text, text)                                      SECURITY DEFINER;
ALTER FUNCTION hand_back_thread(text, text, text)                                      SECURITY DEFINER;

-- what the function owner needs
GRANT SELECT, INSERT ON option_selections, confirmation_attestations TO setuhaul_rpc;
GRANT UPDATE (expires_at) ON confirmation_attestations TO setuhaul_rpc;
GRANT INSERT ON chat_messages, eta_signals, driver_exceptions TO setuhaul_rpc;
GRANT SELECT ON eta_signals, driver_exceptions, escalations TO setuhaul_rpc;
GRANT UPDATE (human_active, status) ON chat_threads TO setuhaul_rpc;
GRANT SELECT, INSERT ON thread_flow TO setuhaul_rpc;
GRANT UPDATE (state, selected_option, yes_message_id, hold_option, appointment_id, attestation_id, version, updated_at)
  ON thread_flow TO setuhaul_rpc;
GRANT UPDATE (expires_at) ON shown_options TO setuhaul_rpc;

-- ---------------------------------------------------------------- E. narrower app-role privileges
-- agent: no direct writes to options, signals, exceptions, escalations, audit, outbox, keys or threads
REVOKE INSERT ON chat_threads, driver_exceptions, eta_signals, shown_options, escalations, booking_events, outbox
  FROM setuhaul_agent;
REVOKE ALL ON idempotency_keys FROM setuhaul_agent;
REVOKE UPDATE ON chat_threads, driver_exceptions FROM setuhaul_agent;
GRANT UPDATE (shipment_id, clarification_turns) ON chat_threads TO setuhaul_agent;
GRANT SELECT ON option_selections, confirmation_attestations TO setuhaul_agent, setuhaul_console, setuhaul_system;
GRANT UPDATE (attestation_id) ON thread_flow TO setuhaul_agent;
-- console: takeover and hand-back only through functions; audit and outbox only through functions
REVOKE UPDATE ON chat_threads FROM setuhaul_console;
REVOKE INSERT ON booking_events, outbox FROM setuhaul_console;
-- system: the service publishes options it computed (feasibility runs under this role)
GRANT INSERT ON shown_options TO setuhaul_system;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION hold_option_core(uuid, text, bigint, text, text, interval) FROM setuhaul_agent;
REVOKE EXECUTE ON FUNCTION submit_booking_request_core(uuid, text, bigint, text, text) FROM setuhaul_agent;
GRANT EXECUTE ON FUNCTION hold_option(uuid, text, uuid, text, text, interval)        TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION submit_booking_request(uuid, text, uuid, text, text)       TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION record_read_back(uuid, text, text, text)                   TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION record_driver_eta(text, bigint, text, timestamptz, text, text, text, text) TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION escalate_thread(text, text, text, text, text, text, text)  TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION attest_confirmation(text, bigint, text, text, text)        TO setuhaul_system;
GRANT EXECUTE ON FUNCTION take_over_thread(text, text, text)                         TO setuhaul_console;
GRANT EXECUTE ON FUNCTION hand_back_thread(text, text, text)                         TO setuhaul_console;
