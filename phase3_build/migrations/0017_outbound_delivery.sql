-- 0017 · B-WF-3: durable outbound notifications (review R2.0 S4-R2-07; DEPLOYMENT_GATES.md OUTBOUND_DELIVERY).
-- Forward-only; 0001–0016 untouched. Builds on the notification state machine of 0001 (re-review C).
--
--   agent reply row (chat_messages, AGENT) ──trigger──▶ outbox AgentReplyStored ─┐
--   ApprovalDecided / policy BookingConfirmed (outbox) ───────────────────────────┼─▶ enqueue_notifications()
--        one notification per (event, channel), id = md5(event:channel:recipient) — stable; consumed in the same tx
--   claim_notifications() ─▶ worker sends ─▶ record_send_result()
--        SENT | RETRY (backoff) | FAILED (+ escalation) | UNKNOWN (timeout: the provider MAY have it)
--   provider status webhook ─▶ record_delivery_status()  (by provider id, or by our notification id echoed back)
--   reconcile_notifications(): stuck SENDING → UNKNOWN; UNKNOWN past its deadline → one resend with the SAME id, then FAILED
--
-- S4-R2-07: the worker sends the STORED reply. A notification for an agent reply carries only the message id and its
-- content hash; claim_notifications() returns the stored body and refuses (FAILED, CANONICAL_MISMATCH) if it no longer
-- matches. Nothing is regenerated.
-- Delivery is at least once with at most one same-id resend per notification. Never exactly once: with a provider
-- that honours an idempotency key the resend is absorbed; with one that does not (WhatsApp Cloud API) the driver may
-- see one identical repeat. Tests prove both cases.
SET search_path = setuhaul, public;

-- ================================================================ columns and policies
ALTER TABLE notifications ALTER COLUMN facility_id DROP NOT NULL;           -- a reply in a thread with no shipment yet
ALTER TABLE notifications
  ADD COLUMN chat_message_id bigint REFERENCES chat_messages,
  ADD COLUMN content_hash    text,
  ADD COLUMN thread_id       text,
  ADD CONSTRAINT notification_reply_is_bound CHECK ((chat_message_id IS NULL) = (content_hash IS NULL));
CREATE UNIQUE INDEX ux_one_notification_per_reply ON notifications (chat_message_id, channel) WHERE chat_message_id IS NOT NULL;
CREATE POLICY p_notifications_central ON notifications
  USING (facility_id IS NULL AND scope_allows('CENTRAL')) WITH CHECK (facility_id IS NULL AND scope_allows('CENTRAL'));

-- ================================================================ every stored agent reply becomes an outbox event
CREATE FUNCTION trg_agent_reply_outbox() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; fac text; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities) || ',CENTRAL', true);
  SELECT * INTO t FROM chat_threads WHERE thread_id = NEW.thread_id;
  SELECT destination_facility_id INTO fac FROM shipments WHERE shipment_id = t.shipment_id;
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'chat_message', NEW.message_id::text, 'AgentReplyStored',
          jsonb_build_object('message_id', NEW.message_id, 'thread_id', NEW.thread_id, 'driver_id', t.driver_id,
                             'content_hash', NEW.content_hash, 'channel', NEW.channel));
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;
CREATE TRIGGER agent_reply_outbox AFTER INSERT ON chat_messages
  FOR EACH ROW WHEN (NEW.sender_type = 'AGENT' AND NEW.channel = 'WHATSAPP')
  EXECUTE FUNCTION trg_agent_reply_outbox();

-- ================================================================ enqueue: one notification per event and channel
CREATE FUNCTION notification_id_for(p_event bigint, p_channel text, p_recipient text) RETURNS uuid LANGUAGE sql IMMUTABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT md5('setuhaul-notification:' || p_event || ':' || p_channel || ':' || p_recipient)::uuid
$$;

CREATE FUNCTION enqueue_notifications(p_limit integer DEFAULT 200) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE e record; drv text; sid text; tid text; n integer := 0; ins integer;
BEGIN
  FOR e IN SELECT * FROM pending_outbox_events('notification-enqueue',
                                               ARRAY['AgentReplyStored','ApprovalDecided','BookingConfirmed'], p_limit) LOOP
    drv := NULL; sid := NULL; tid := NULL;
    IF e.event_type = 'AgentReplyStored' THEN
      drv := e.payload ->> 'driver_id'; tid := e.payload ->> 'thread_id';
      SELECT shipment_id INTO sid FROM chat_threads WHERE thread_id = tid;
      INSERT INTO notifications (notification_id, facility_id, source_event_id, shipment_id, channel, recipient_ref,
                                 template_id, payload, chat_message_id, content_hash, thread_id)
      SELECT notification_id_for(e.event_id, 'WHATSAPP', 'driver:' || drv), o.facility_id, e.event_id, sid, 'WHATSAPP',
             'driver:' || drv, 'agent_reply',
             jsonb_build_object('message_id', (e.payload ->> 'message_id')::bigint),    -- never the text itself
             (e.payload ->> 'message_id')::bigint, e.payload ->> 'content_hash', tid
        FROM outbox o WHERE o.event_id = e.event_id
      ON CONFLICT DO NOTHING;
    ELSIF e.event_type = 'ApprovalDecided' OR (e.event_type = 'BookingConfirmed' AND e.payload ->> 'basis' = 'POLICY') THEN
      sid := e.payload ->> 'shipment_id';
      SELECT driver_id INTO drv FROM shipments WHERE shipment_id = sid;
      SELECT thread_id INTO tid FROM chat_threads WHERE shipment_id = sid ORDER BY opened_at DESC LIMIT 1;
      INSERT INTO notifications (notification_id, facility_id, source_event_id, shipment_id, channel, recipient_ref,
                                 template_id, payload, thread_id)
      SELECT notification_id_for(e.event_id, 'WHATSAPP', 'driver:' || drv), o.facility_id, e.event_id, sid, 'WHATSAPP',
             'driver:' || drv,
             'booking_' || lower(coalesce(e.payload ->> 'outcome', 'CONFIRMED')),
             jsonb_build_object('appointment_id', e.payload ->> 'appointment_id', 'outcome', coalesce(e.payload ->> 'outcome', 'CONFIRMED'),
                                'dock_id', e.payload ->> 'dock_id', 'start', e.payload ->> 'start',
                                'still_valid_booking', e.payload ->> 'still_valid_booking'),
             tid
        FROM outbox o WHERE o.event_id = e.event_id AND drv IS NOT NULL
      ON CONFLICT DO NOTHING;
    END IF;
    GET DIAGNOSTICS ins = ROW_COUNT;
    n := n + ins;
    PERFORM consume_outbox_event('notification-enqueue', e.event_id);       -- same transaction as the insert (§17)
  END LOOP;
  RETURN n;
END $$;

-- ================================================================ claim due sends
-- PENDING, RETRY that is due, and UNKNOWN past its deadline that still has its one resend. The agent reply's body is
-- read from the stored row here and checked against its hash.
CREATE FUNCTION claim_notifications(p_limit integer DEFAULT 20)
RETURNS TABLE (notification_id uuid, attempt integer, channel text, recipient_ref text, template_id text, payload jsonb,
               body text, is_resend boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE r notifications; m chat_messages; t chat_threads; clk timestamptz := clock_timestamp(); resend boolean;
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
      SELECT * INTO t FROM chat_threads WHERE thread_id = m.thread_id;
      IF t.human_active AND r.status = 'PENDING' THEN                     -- a person took over before it went out
        UPDATE notifications SET status = 'FAILED', last_error = 'HUMAN_TOOK_OVER' WHERE notifications.notification_id = r.notification_id;
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

-- a terminal failure is never silent: a person is told (escalation NOTIFICATION_FAILED)
CREATE FUNCTION notification_failed(p_notification uuid, p_reason text) RETURNS void LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r notifications;
BEGIN
  SELECT * INTO r FROM notifications WHERE notification_id = p_notification;
  INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, thread_id, handoff, ack_due_at)
  SELECT gen_random_uuid(), 'NOTIFICATION_FAILED', 'DECIDE', coalesce(r.facility_id, s.destination_facility_id), r.shipment_id,
         r.thread_id, jsonb_build_object('notification_id', r.notification_id, 'template_id', r.template_id, 'reason', p_reason,
                                         'attempts', r.attempts),
         clock_timestamp() + interval '10 minutes'
    FROM (SELECT 1) one LEFT JOIN shipments s ON s.shipment_id = r.shipment_id
   WHERE coalesce(r.facility_id, s.destination_facility_id) IS NOT NULL;
END $$;

-- ================================================================ the result of one send attempt
-- Fenced by the attempt number: a worker whose claim was reaped (now UNKNOWN or resent) changes nothing.
CREATE FUNCTION record_send_result(p_notification uuid, p_attempt integer, p_outcome text, p_provider_message_id text,
                                   p_error text DEFAULT NULL) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r notifications; backoff interval;
BEGIN
  IF p_outcome NOT IN ('ACCEPTED','RETRYABLE','PERMANENT','AMBIGUOUS') THEN
    RAISE EXCEPTION 'BAD_OUTCOME: %', p_outcome USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO r FROM notifications WHERE notification_id = p_notification FOR UPDATE;
  IF NOT FOUND THEN RETURN 'UNKNOWN_NOTIFICATION'; END IF;
  IF r.status <> 'SENDING' OR r.attempts <> p_attempt THEN
    -- a stale worker: record the provider id if we learnt one, but never move the state
    IF p_outcome = 'ACCEPTED' AND p_provider_message_id IS NOT NULL AND r.provider_message_id IS NULL
       AND r.status IN ('UNKNOWN','SENDING') THEN
      UPDATE notifications SET provider_message_id = p_provider_message_id WHERE notification_id = r.notification_id;
    END IF;
    RETURN 'STALE';
  END IF;
  IF p_outcome = 'ACCEPTED' THEN
    UPDATE notifications SET status = 'SENT', provider_message_id = coalesce(p_provider_message_id, r.provider_message_id),
                             last_error = NULL WHERE notification_id = r.notification_id;
  ELSIF p_outcome = 'RETRYABLE' AND r.attempts < r.max_attempts THEN
    backoff := make_interval(secs => 30 * power(2, r.attempts - 1));
    UPDATE notifications SET status = 'RETRY', next_retry_at = clock_timestamp() + backoff, last_error = p_error
     WHERE notification_id = r.notification_id;
  ELSIF p_outcome = 'AMBIGUOUS' THEN
    UPDATE notifications SET status = 'UNKNOWN', last_error = coalesce(p_error, 'NO_PROVIDER_ANSWER')
     WHERE notification_id = r.notification_id;
  ELSE                                                                         -- permanent, or retries used up
    UPDATE notifications SET status = 'FAILED', last_error = coalesce(p_error, p_outcome) WHERE notification_id = r.notification_id;
    PERFORM notification_failed(r.notification_id, coalesce(p_error, p_outcome));
  END IF;
  RETURN (SELECT status FROM notifications WHERE notification_id = r.notification_id);
END $$;

-- ================================================================ provider status webhooks
-- Matched by the provider's message id, or by our notification id echoed back (WhatsApp biz_opaque_callback_data)
-- when the send's answer was lost. Repeats and out-of-order receipts are harmless.
CREATE FUNCTION record_delivery_status(p_provider_message_id text, p_correlation uuid, p_status text, p_error text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r notifications; old_scope text := current_setting('app.facility_scope', true); result text;
BEGIN
  IF p_status NOT IN ('SENT','DELIVERED','READ','FAILED') THEN
    RAISE EXCEPTION 'BAD_DELIVERY_STATUS: %', p_status USING ERRCODE = 'P0001'; END IF;
  -- the inbound path has no facility scope; this trusted function looks across facilities for one row
  PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities) || ',CENTRAL', true);
  SELECT * INTO r FROM notifications
   WHERE (p_provider_message_id IS NOT NULL AND channel = 'WHATSAPP' AND provider_message_id = p_provider_message_id)
      OR (p_correlation IS NOT NULL AND notification_id = p_correlation)
   ORDER BY (provider_message_id = p_provider_message_id) DESC NULLS LAST LIMIT 1 FOR UPDATE;
  IF NOT FOUND THEN PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true); RETURN 'IGNORED'; END IF;
  IF r.provider_message_id IS NULL AND p_provider_message_id IS NOT NULL THEN
    UPDATE notifications SET provider_message_id = p_provider_message_id WHERE notification_id = r.notification_id;
  END IF;
  IF p_status IN ('DELIVERED','READ') AND r.status IN ('SENDING','SENT','UNKNOWN') THEN
    UPDATE notifications SET status = 'DELIVERED' WHERE notification_id = r.notification_id;
  ELSIF p_status = 'SENT' AND r.status IN ('SENDING','UNKNOWN') THEN
    UPDATE notifications SET status = 'SENT' WHERE notification_id = r.notification_id;
  ELSIF p_status = 'FAILED' AND r.status IN ('SENDING','SENT','UNKNOWN') THEN
    UPDATE notifications SET status = 'FAILED', last_error = coalesce(p_error, 'PROVIDER_FAILED') WHERE notification_id = r.notification_id;
    PERFORM notification_failed(r.notification_id, coalesce(p_error, 'PROVIDER_FAILED'));
  END IF;
  SELECT status INTO result FROM notifications WHERE notification_id = r.notification_id;   -- before the scope is restored
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN result;
END $$;

-- ================================================================ reconciliation
-- Stuck SENDING (a worker died mid-send) → UNKNOWN; UNKNOWN whose one resend is spent and whose deadline passed → FAILED.
-- (UNKNOWN with its resend unused is picked up by claim_notifications, with the same notification id.)
CREATE FUNCTION reconcile_notifications(p_stuck_after interval DEFAULT interval '2 minutes') RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r notifications; reaped integer := 0; failed integer := 0;
BEGIN
  FOR r IN SELECT * FROM notifications WHERE status = 'SENDING' AND sending_started_at < clock_timestamp() - p_stuck_after
            FOR UPDATE SKIP LOCKED LOOP
    UPDATE notifications SET status = 'UNKNOWN', last_error = 'WORKER_LOST' WHERE notification_id = r.notification_id;
    reaped := reaped + 1;
  END LOOP;
  FOR r IN SELECT * FROM notifications WHERE status = 'UNKNOWN' AND unknown_resend_count = 1
              AND unknown_deadline <= clock_timestamp() FOR UPDATE SKIP LOCKED LOOP
    UPDATE notifications SET status = 'FAILED', last_error = 'UNCONFIRMED_AFTER_RESEND' WHERE notification_id = r.notification_id;
    PERFORM notification_failed(r.notification_id, 'UNCONFIRMED_AFTER_RESEND');
    failed := failed + 1;
  END LOOP;
  RETURN jsonb_build_object('reaped', reaped, 'failed', failed);
END $$;

-- ================================================================ ownership and grants
DO $$ DECLARE f text; BEGIN
  FOREACH f IN ARRAY ARRAY['trg_agent_reply_outbox()', 'notification_id_for(bigint,text,text)', 'enqueue_notifications(integer)',
    'claim_notifications(integer)', 'notification_failed(uuid,text)', 'record_send_result(uuid,integer,text,text,text)',
    'record_delivery_status(text,uuid,text,text)', 'reconcile_notifications(interval)'] LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO setuhaul_rpc', f);
  END LOOP;
END $$;
GRANT INSERT ON notifications TO setuhaul_rpc;
GRANT UPDATE (status, attempts, next_retry_at, provider_message_id, last_error, unknown_deadline) ON notifications TO setuhaul_rpc;
GRANT SELECT ON chat_messages, chat_threads, drivers TO setuhaul_rpc;
GRANT INSERT ON outbox TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION enqueue_notifications(integer)                     TO setuhaul_system;
GRANT EXECUTE ON FUNCTION claim_notifications(integer)                       TO setuhaul_system;
GRANT EXECUTE ON FUNCTION record_send_result(uuid, integer, text, text, text) TO setuhaul_system;
GRANT EXECUTE ON FUNCTION reconcile_notifications(interval)                  TO setuhaul_system;
GRANT EXECUTE ON FUNCTION record_delivery_status(text, uuid, text, text)     TO setuhaul_ingest;   -- the signed webhook path
