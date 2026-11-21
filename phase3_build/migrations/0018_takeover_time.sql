-- 0018 · Found by the B-WF-3 flow tests (19 Nov 2026). Forward-only; 0001–0017 untouched.
--
-- 0017 suppressed any unsent agent reply in a conversation a person had taken over. But a safety report hands the
-- conversation over FIRST and stores the agent's "a coordinator will call you now" reply AFTER, so the one message the
-- driver most needs was suppressed. The takeover now records when it happened, and only replies stored before that
-- moment are suppressed.
--
-- Also found by the flow tests: two kinds of duplicate message to the driver.
--  * The read-back is written by record_read_back() as evidence (the attestation points at it) AND returned as the
--    turn's reply, which the orchestrator stores again with the same text. Only turn replies (reply_to set) are sent
--    now; the evidence row is never sent on its own. The orchestrator returns exactly the database's read-back text
--    (tests/test_flows.py F1 checks it).
--  * A policy confirmation happens inside the driver's own turn, so the agent's reply already tells the driver; the
--    template notification for it is no longer enqueued. Templates stay for outcomes that arrive later (ApprovalDecided).
SET search_path = setuhaul, public;

DROP TRIGGER agent_reply_outbox ON chat_messages;
CREATE TRIGGER agent_reply_outbox AFTER INSERT ON chat_messages
  FOR EACH ROW WHEN (NEW.sender_type = 'AGENT' AND NEW.channel = 'WHATSAPP' AND NEW.reply_to IS NOT NULL)
  EXECUTE FUNCTION trg_agent_reply_outbox();


ALTER TABLE chat_threads ADD COLUMN human_active_since timestamptz;
UPDATE chat_threads SET human_active_since = now() WHERE human_active;

CREATE FUNCTION trg_human_active_since() RETURNS trigger LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF NEW.human_active AND NOT coalesce(OLD.human_active, false) THEN NEW.human_active_since := clock_timestamp();
  ELSIF NOT NEW.human_active THEN NEW.human_active_since := NULL;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER human_active_since BEFORE INSERT OR UPDATE OF human_active ON chat_threads
  FOR EACH ROW EXECUTE FUNCTION trg_human_active_since();

CREATE OR REPLACE FUNCTION claim_notifications(p_limit integer DEFAULT 20)
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
      -- a person took over AFTER this reply was stored: do not send the stale reply. A reply stored after the takeover
      -- (e.g. "a coordinator will call you" right after a safety hand-over) is sent.
      IF t.human_active AND r.status = 'PENDING' AND m.created_at < t.human_active_since THEN
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

CREATE OR REPLACE FUNCTION enqueue_notifications(p_limit integer DEFAULT 200) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE e record; drv text; sid text; tid text; n integer := 0; ins integer;
BEGIN
  FOR e IN SELECT * FROM pending_outbox_events('notification-enqueue',
                                               ARRAY['AgentReplyStored','ApprovalDecided'], p_limit) LOOP
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
    ELSIF e.event_type = 'ApprovalDecided' THEN
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

ALTER FUNCTION enqueue_notifications(integer) OWNER TO setuhaul_rpc;
ALTER FUNCTION claim_notifications(integer) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_notifications(integer) TO setuhaul_system;
GRANT EXECUTE ON FUNCTION enqueue_notifications(integer) TO setuhaul_system;
