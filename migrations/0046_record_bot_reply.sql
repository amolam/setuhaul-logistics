-- 0046 · Demo release: the bot's immediate Telegram replies are part of the stored conversation.
--
-- Found in the 26 Nov 2026 E2E test: the planner's conversation view showed the driver's messages and the agent's
-- replies, but not the replies the webhook itself sends at once (an offer answer such as "Request sent ...", a typed
-- yes/no, a location acknowledgement, "please contact your dispatcher"): those went straight to Telegram and were
-- never stored, so the dashboard missed messages the driver had received.
--
-- record_bot_reply stores one such reply as a SYSTEM message on the TELEGRAM channel in the driver's open
-- conversation. Forward-only and additive; no existing row or function changes. Rules:
--   * SECURITY DEFINER, owned by setuhaul_rpc, callable only by the ingest login (the webhook), like 0004's writers;
--   * only for the transaction's own driver (app.driver_id, set by Db.tx(driver=...)): no other conversation;
--   * only into an existing open conversation (a link reply before any conversation is not stored);
--   * idempotent on (channel, external id): a Telegram webhook retry stores nothing twice (tgr:<update id>);
--   * SYSTEM, never AGENT: the agent_reply_outbox trigger (AGENT + WHATSAPP + reply_to) cannot fire, so storing a
--     reply never sends it again; the outbound relay never sees it;
--   * long digit runs masked like every stored message.
-- record_notice_message stores a fixed notice the relay has SENT (e.g. booking_approved: "Your dock booking is
-- confirmed by the warehouse.") as a SYSTEM message in the notification's conversation, once per notification
-- (notice:<id>). Callable only by the system login (the outbound worker), only for a SENT or DELIVERED notification
-- that is a template (no stored agent reply: chat_message_id IS NULL) and belongs to a conversation. The system
-- login keeps no INSERT privilege on chat_messages; this function is its only way to write one, and only this kind.
SET search_path = setuhaul, public;

CREATE FUNCTION record_bot_reply(p_driver text, p_external_id text, p_text text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE th text; mid bigint; body text;
BEGIN
  IF p_driver IS NULL OR p_driver <> coalesce(nullif(current_setting('app.driver_id', true), ''), '-') THEN
    RAISE EXCEPTION 'BOT_REPLY_REFUSED: only the transaction''s own driver' USING ERRCODE = '42501';
  END IF;
  IF p_text IS NULL OR length(p_text) NOT BETWEEN 1 AND 2000 THEN
    RAISE EXCEPTION 'BOT_REPLY_REFUSED: text must be 1 to 2000 characters' USING ERRCODE = '22023';
  END IF;
  IF p_external_id IS NULL OR p_external_id !~ '^tgr:[A-Za-z0-9:_-]{1,90}$' THEN
    RAISE EXCEPTION 'BOT_REPLY_REFUSED: external id must be tgr:<update ref>' USING ERRCODE = '22023';
  END IF;
  SELECT thread_id INTO th FROM chat_threads
   WHERE driver_id = p_driver AND status NOT IN ('CLOSED', 'RESOLVED') ORDER BY opened_at DESC LIMIT 1;
  IF th IS NULL THEN
    RETURN NULL;
  END IF;
  body := left(regexp_replace(p_text, '(?<!ORD-)\d{6,}', '######', 'g'), 2000);
  INSERT INTO chat_messages (thread_id, sender_type, channel, external_message_id, content_hash, body_masked)
  VALUES (th, 'SYSTEM', 'TELEGRAM', p_external_id, encode(sha256(convert_to(body, 'UTF8')), 'hex'), body)
  ON CONFLICT (channel, external_message_id) DO NOTHING
  RETURNING message_id INTO mid;
  RETURN mid;
END $$;

ALTER FUNCTION record_bot_reply(text, text, text) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION record_bot_reply(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_bot_reply(text, text, text) TO setuhaul_ingest;

CREATE FUNCTION record_notice_message(p_notification uuid, p_channel text, p_text text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n notifications; mid bigint; body text;
BEGIN
  IF p_channel NOT IN ('TELEGRAM', 'WHATSAPP') THEN
    RAISE EXCEPTION 'NOTICE_REFUSED: channel must be TELEGRAM or WHATSAPP' USING ERRCODE = '22023';
  END IF;
  IF p_text IS NULL OR length(p_text) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION 'NOTICE_REFUSED: text must be 1 to 1000 characters' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO n FROM notifications WHERE notification_id = p_notification;
  IF NOT FOUND OR n.thread_id IS NULL OR n.chat_message_id IS NOT NULL OR n.template_id IS NULL
     OR n.status NOT IN ('SENT', 'DELIVERED') THEN
    RETURN NULL;                                   -- not a sent fixed notice of a conversation: nothing to store
  END IF;
  body := left(regexp_replace(p_text, '(?<!ORD-)\d{6,}', '######', 'g'), 1000);
  INSERT INTO chat_messages (thread_id, sender_type, channel, external_message_id, content_hash, body_masked)
  VALUES (n.thread_id, 'SYSTEM', p_channel, 'notice:' || n.notification_id::text,
          encode(sha256(convert_to(body, 'UTF8')), 'hex'), body)
  ON CONFLICT (channel, external_message_id) DO NOTHING
  RETURNING message_id INTO mid;
  RETURN mid;
END $$;

ALTER FUNCTION record_notice_message(uuid, text, text) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION record_notice_message(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_notice_message(uuid, text, text) TO setuhaul_system;
