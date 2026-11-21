-- 0008 · Step 3: facts a driver gave in earlier turns (G3 golden-set finding 4). Forward-only; 0001–0007 untouched.
--
-- A conversation often spreads one report over several messages ("late by 45 minutes" → "which order?" →
-- "ORD-…-020"). The model sees only the current message, so earlier facts must come from the database, not from the
-- model's memory. thread_facts holds the last unresolved facts and the question the agent asked about them.
--
-- The agent can read its own conversation's row (RLS) but never writes the table: note_turn_facts() checks that the
-- thread belongs to the calling driver, that the message is that driver's, takes the conversation lock, accepts only
-- a fixed set of keys and a bounded size, and merges. Facts older than two hours are not returned.
SET search_path = setuhaul, public;

CREATE TABLE thread_facts (
  thread_id   text PRIMARY KEY REFERENCES chat_threads (thread_id),
  facts       jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(facts) = 'object' AND pg_column_size(facts) <= 2048),
  asked       text CHECK (asked IN ('declared_eta_local', 'delay_kind', 'latest_finish_kind', 'shipment_id')),
  message_id  bigint NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE thread_facts ENABLE ROW LEVEL SECURITY; ALTER TABLE thread_facts FORCE ROW LEVEL SECURITY;
CREATE POLICY p_thread_facts ON thread_facts
  USING (driver_or_staff((SELECT t.driver_id FROM chat_threads t WHERE t.thread_id = thread_facts.thread_id), NULL));

CREATE FUNCTION note_turn_facts(p_thread text, p_message bigint, p_facts jsonb, p_asked text, p_reset boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; bad text; merged jsonb;
BEGIN
  PERFORM thread_lock(p_thread);
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'THREAD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF t.driver_id IS DISTINCT FROM NULLIF(current_setting('app.driver_id', true), '') THEN
    RAISE EXCEPTION 'FORBIDDEN: not this driver''s conversation' USING ERRCODE = 'P0001'; END IF;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM chat_messages m WHERE m.message_id = p_message AND m.thread_id = p_thread AND m.sender_type = 'DRIVER';
  IF NOT FOUND THEN RAISE EXCEPTION 'MESSAGE_NOT_FROM_DRIVER' USING ERRCODE = 'P0001'; END IF;
  IF p_facts IS NULL OR jsonb_typeof(p_facts) <> 'object' THEN RAISE EXCEPTION 'BAD_FACTS' USING ERRCODE = 'P0001'; END IF;
  SELECT k INTO bad FROM jsonb_object_keys(p_facts) k
   WHERE k NOT IN ('intent', 'shipment_ref_mentioned', 'delay_kind', 'declared_eta_local', 'declared_eta_range_local',
                   'declared_delay_min', 'earliest_start_local', 'latest_finish_local', 'latest_finish_kind',
                   'exception_type', 'candidate_eta')
   LIMIT 1;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'BAD_FACTS: key %', bad USING ERRCODE = 'P0001'; END IF;
  IF p_reset THEN
    DELETE FROM thread_facts WHERE thread_id = p_thread;
    RETURN '{}'::jsonb;
  END IF;
  INSERT INTO thread_facts AS f (thread_id, facts, asked, message_id) VALUES (p_thread, p_facts, p_asked, p_message)
  ON CONFLICT (thread_id) DO UPDATE
     SET facts = CASE WHEN f.updated_at > now() - interval '2 hours' THEN f.facts ELSE '{}'::jsonb END || EXCLUDED.facts,
         asked = EXCLUDED.asked, message_id = EXCLUDED.message_id, updated_at = now()
  RETURNING facts INTO merged;
  RETURN merged;
END $$;
ALTER FUNCTION note_turn_facts(text, bigint, jsonb, text, boolean) OWNER TO setuhaul_rpc;

GRANT SELECT, INSERT, UPDATE, DELETE ON thread_facts TO setuhaul_rpc;
GRANT SELECT ON thread_facts TO setuhaul_agent, setuhaul_console;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION note_turn_facts(text, bigint, jsonb, text, boolean) TO setuhaul_agent;
