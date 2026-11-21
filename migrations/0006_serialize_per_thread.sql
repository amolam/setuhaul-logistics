-- 0006 · Per-conversation serialization (review R0.7, P0). Forward-only; 0001–0005 untouched.
--
-- Problem: the freshness check ("has the driver written anything since this YES?") only sees COMMITTED messages.
-- A driver message being stored at the same instant as a hold or request was invisible, so the booking could win.
--
-- Fix: every operation that orders a conversation takes the same per-thread transaction lock first:
--   storing an inbound DRIVER message (ingest), writing the read-back, attesting a reply, verifying an attestation
--   (hold and request), human takeover, and escalation. The lock is held until the transaction commits, so either
--   the new message commits first and the old YES is rejected, or the booking commits first and the new message is
--   processed afterwards (and supersedes the YES for any later step).
--
-- Mechanism: a transaction-scoped advisory lock keyed on the thread id (thread_lock()). It needs no UPDATE privilege
-- on chat_threads, so the ingest role stays read-only on threads. Lock order is always thread → dock (place_hold), and
-- no path takes a dock lock before a thread lock, so this cannot deadlock with the dock serialization.
SET search_path = setuhaul, public;

CREATE OR REPLACE FUNCTION thread_lock(p_thread text) RETURNS void LANGUAGE sql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT pg_advisory_xact_lock(hashtext('setuhaul.thread:' || p_thread))
$$;
ALTER FUNCTION thread_lock(text) OWNER TO setuhaul_rpc;

-- verify_attestation runs inside hold_option and submit_booking_request; locking here covers both.
CREATE OR REPLACE FUNCTION verify_attestation(p_option uuid, p_thread text, p_attestation uuid)
RETURNS bigint LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a confirmation_attestations; latest bigint;
BEGIN
  PERFORM thread_lock(p_thread);              -- wait for any inbound message on this thread to commit first
  SELECT * INTO a FROM confirmation_attestations WHERE attestation_id = p_attestation;
  IF NOT FOUND OR a.thread_id <> p_thread OR a.option_id <> p_option THEN
    RAISE EXCEPTION 'ATTESTATION_INVALID' USING ERRCODE = 'P0001'; END IF;
  IF a.verdict <> 'YES' THEN RAISE EXCEPTION 'NO_DRIVER_CONFIRMATION' USING ERRCODE = 'P0001'; END IF;
  IF a.selection_id <> (SELECT max(selection_id) FROM option_selections WHERE thread_id = p_thread) THEN
    RAISE EXCEPTION 'SELECTION_CHANGED' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM chat_messages m
   WHERE m.thread_id = p_thread AND m.sender_type = 'DRIVER' AND m.message_id > a.confirmation_message_id;
  IF FOUND THEN RAISE EXCEPTION 'CONFIRMATION_SUPERSEDED' USING ERRCODE = 'P0001'; END IF;
  SELECT latest_reply_message_id INTO latest FROM option_selections WHERE selection_id = a.selection_id;
  IF latest IS DISTINCT FROM a.confirmation_message_id THEN
    RAISE EXCEPTION 'CONFIRMATION_SUPERSEDED' USING ERRCODE = 'P0001'; END IF;
  IF a.expires_at < now() THEN RAISE EXCEPTION 'ATTESTATION_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  RETURN a.confirmation_message_id;
END $$;

-- The other ordering operations: wrap the existing function (renamed *_v1) behind a locking entry point with the
-- same name and signature, so callers and grants by name are unchanged.
ALTER FUNCTION record_read_back(uuid, text, text, text) RENAME TO record_read_back_v1;
ALTER FUNCTION attest_confirmation(text, bigint, text, text, text) RENAME TO attest_confirmation_v1;
ALTER FUNCTION take_over_thread(text, text, text) RENAME TO take_over_thread_v1;
ALTER FUNCTION escalate_thread(text, text, text, text, text, text, text) RENAME TO escalate_thread_v1;

CREATE FUNCTION record_read_back(p_option uuid, p_thread text, p_text text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM thread_lock(p_thread); RETURN record_read_back_v1(p_option, p_thread, p_text, p_principal); END $$;

CREATE FUNCTION attest_confirmation(p_thread text, p_message bigint, p_verdict text, p_classifier text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM thread_lock(p_thread); RETURN attest_confirmation_v1(p_thread, p_message, p_verdict, p_classifier, p_principal); END $$;

CREATE FUNCTION take_over_thread(p_thread text, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM thread_lock(p_thread); RETURN take_over_thread_v1(p_thread, p_principal, p_reason); END $$;

CREATE FUNCTION escalate_thread(p_thread text, p_reason text, p_mode text, p_driver_last text, p_told text,
                                p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  PERFORM thread_lock(p_thread);
  RETURN escalate_thread_v1(p_thread, p_reason, p_mode, p_driver_last, p_told, p_idem_key, p_principal);
END $$;

ALTER FUNCTION record_read_back(uuid, text, text, text)                                OWNER TO setuhaul_rpc;
ALTER FUNCTION attest_confirmation(text, bigint, text, text, text)                     OWNER TO setuhaul_rpc;
ALTER FUNCTION take_over_thread(text, text, text)                                      OWNER TO setuhaul_rpc;
ALTER FUNCTION escalate_thread(text, text, text, text, text, text, text)               OWNER TO setuhaul_rpc;

-- Atomic enqueue claim (review R0.7, P1 for step 4): one of two simultaneous duplicate webhooks wins the claim.
ALTER TABLE inbound_receipts ADD COLUMN enqueued_at timestamptz;
GRANT UPDATE (enqueued_at) ON inbound_receipts TO setuhaul_ingest;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION record_read_back_v1(uuid, text, text, text) FROM setuhaul_agent;
REVOKE EXECUTE ON FUNCTION attest_confirmation_v1(text, bigint, text, text, text) FROM setuhaul_ingest;
REVOKE EXECUTE ON FUNCTION take_over_thread_v1(text, text, text) FROM setuhaul_console;
REVOKE EXECUTE ON FUNCTION escalate_thread_v1(text, text, text, text, text, text, text) FROM setuhaul_agent;
GRANT EXECUTE ON FUNCTION record_read_back(uuid, text, text, text)                    TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION escalate_thread(text, text, text, text, text, text, text)   TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION attest_confirmation(text, bigint, text, text, text)         TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION thread_lock(text)                                           TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION take_over_thread(text, text, text)                          TO setuhaul_console;
