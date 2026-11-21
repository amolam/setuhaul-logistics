-- 0022 · Human test H-5 (owner decision, 20 Nov 2026). Forward-only; 0001–0021 untouched.
--
-- After a coordinator hands a conversation back:
--   1. the driver is told the assistant is back, in the conversation's language, through the normal outbound path
--      (a stored AGENT message plus an AgentReplyStored outbox event, so it is retried, receipted and suppressed
--      exactly like an agent reply);
--   2. the coordinator's summary is kept on the conversation (chat_threads.handback_note / handback_at). The agent
--      reads it as BACKGROUND ONLY for 12 hours: it goes into the model's context, never into a tool argument, and
--      it cannot confirm or change a booking. Bookings still need the driver's explicit yes and the warehouse (or an
--      authorised rule), as before.
-- Before 0022 the summary was written only to the audit log (booking_events), the agent restarted with no idea what
-- the call settled, and the driver was not told it could write again.
--
-- Language: the same rule as the agent's replies (T43): Devanagari → Hindi; Hinglish markers in the driver's last
-- message of three or more words → Hinglish; otherwise English.
SET search_path = setuhaul, public;

ALTER TABLE chat_threads ADD COLUMN handback_note text, ADD COLUMN handback_at timestamptz;
GRANT UPDATE (handback_note, handback_at) ON chat_threads TO setuhaul_rpc;     -- written only by hand_back_thread

DROP FUNCTION hand_back_thread(text, text, text);
CREATE FUNCTION hand_back_thread(p_thread text, p_principal text, p_summary text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text; role text; n integer; sid text; drv text;
        last_text text; notice text; msg bigint; old_scope text;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['operations','planner','ops_lead'], 'handing back a conversation');
  IF p_summary IS NULL OR length(p_summary) < 10 THEN
    RAISE EXCEPTION 'HANDBACK_SUMMARY_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  PERFORM thread_lock(p_thread);
  SELECT s.destination_facility_id INTO fac FROM chat_threads t LEFT JOIN shipments s ON s.shipment_id = t.shipment_id
   WHERE t.thread_id = p_thread;
  IF fac IS NOT NULL AND NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'THREAD_NOT_IN_HUMAN_HANDS' USING ERRCODE = 'P0001'; END IF;
  UPDATE chat_threads SET human_active = false, status = 'OPEN', handback_note = p_summary, handback_at = clock_timestamp()
   WHERE thread_id = p_thread AND human_active
  RETURNING shipment_id, driver_id INTO sid, drv;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN RAISE EXCEPTION 'THREAD_NOT_IN_HUMAN_HANDS' USING ERRCODE = 'P0001'; END IF;
  UPDATE thread_flow SET state = 'NO_OPTIONS', selected_option = NULL, yes_message_id = NULL, hold_option = NULL,
                         attestation_id = NULL, version = version + 1, updated_at = now() WHERE thread_id = p_thread;
  UPDATE shown_options SET expires_at = least(expires_at, now()) WHERE thread_id = p_thread;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, reason)
  VALUES (ctx.principal, role, 'HAND_BACK_THREAD', fac, sid, p_summary);

  -- H-5: tell the driver, in the conversation's language
  SELECT body_masked INTO last_text FROM chat_messages
   WHERE thread_id = p_thread AND sender_type = 'DRIVER'
     AND (body_masked ~ '[ऀ-ॿ]' OR array_length(regexp_split_to_array(trim(body_masked), '\s+'), 1) >= 3)
   ORDER BY message_id DESC LIMIT 1;
  notice := CASE
    WHEN last_text ~ '[ऀ-ॿ]' THEN 'कोऑर्डिनेटर ने आपका मामला देख लिया है। अब आप यहाँ दोबारा मैसेज कर सकते हैं।'
    WHEN last_text ~* '\m(hai|nahi|haan|bhai|ji|kya|kab|der|late ho|pahunch\w*|raha|rahi|hoon|hun|baje|ghanta|ghante|minute|gaya|mein)\M'
      THEN 'Coordinator ne aapka case dekh liya hai. Ab aap yahan dobara message kar sakte hain.'
    ELSE 'Our coordinator has finished with your case. You can message me here again any time.' END;
  old_scope := current_setting('app.facility_scope', true);
  PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities) || ',CENTRAL', true);
  INSERT INTO chat_messages (thread_id, sender_type, channel, content_hash, body_masked)
  VALUES (p_thread, 'AGENT', 'WHATSAPP', encode(sha256(convert_to(notice, 'UTF8')), 'hex'), notice)
  RETURNING message_id INTO msg;
  -- the reply trigger (0018) sends only answers to a driver message; this notice is queued the same way, explicitly
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'chat_message', msg::text, 'AgentReplyStored',
          jsonb_build_object('message_id', msg, 'thread_id', p_thread, 'driver_id', drv,
                             'content_hash', encode(sha256(convert_to(notice, 'UTF8')), 'hex'), 'channel', 'WHATSAPP'));
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN true;
END $$;

ALTER FUNCTION hand_back_thread(text, text, text) OWNER TO setuhaul_rpc;
GRANT INSERT ON outbox TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION hand_back_thread(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hand_back_thread(text, text, text) TO setuhaul_console;
-- the agent reads the note on its own conversation (it already reads chat_threads)
