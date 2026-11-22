-- 0024 · Phase E review E4-01 (22 Nov 2026): retry-safe completion tracking for Step Functions callbacks.
-- Forward-only; 0001–0023 untouched, including every 0015 function and the rule that a decided approval row is
-- permanent (trg_approval_transition). No booking, approval-outcome, expiry or capacity rule changes: the new table
-- records only whether the waiting execution has been told about an outcome PostgreSQL has already recorded.
--
-- Before: the one-minute reconciler re-sent SendTaskSuccess for every closed request with a token decided in the last
-- 24 hours, every minute, because nothing recorded that a callback had been answered.
-- Now, in approval_callbacks (one row per approval, written only by the definer functions below):
--   claim_task_callbacks()     picks closed requests whose callback is not confirmed and is due, and leases each one
--                              by counting the attempt and setting the next due time (backoff 1, 2, 4 ... 30 minutes)
--                              BEFORE anything is sent. Nothing is cleared: a failed, throttled or ambiguous send, or
--                              a crashed worker, simply becomes due again after the backoff.
--   complete_task_callback()   records a definite answer from Step Functions (DELIVERED: SendTaskSuccess returned;
--                              TASK_FINISHED: the task had already finished, timed out or was unknown). Only then is
--                              the request left alone. Repeats are no-ops.
--   record_late_task_token()   keeps a token that arrives after the decision (approval_requests.task_token can only
--                              change while OPEN), so an answer that fails at that moment is retried, not lost.
SET search_path = setuhaul, public;

CREATE TABLE approval_callbacks (
  approval_id   uuid PRIMARY KEY REFERENCES approval_requests,
  late_token    text,                                            -- a token that arrived after the decision
  attempts      integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_at       timestamptz,                                     -- not before this time (the lease and backoff)
  completed_at  timestamptz,
  outcome       text CHECK (outcome IN ('DELIVERED', 'TASK_FINISHED')),
  updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT callback_completion_is_whole CHECK ((completed_at IS NULL) = (outcome IS NULL))
);
CREATE INDEX approval_callbacks_open ON approval_callbacks (next_at) WHERE completed_at IS NULL;

CREATE FUNCTION record_late_task_token(p_approval uuid, p_token text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF p_token IS NULL OR p_token = '' THEN RAISE EXCEPTION 'BAD_TOKEN' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM approval_requests WHERE approval_id = p_approval AND status <> 'OPEN';
  IF NOT FOUND THEN RETURN false; END IF;                       -- unknown, or still OPEN (record_task_token's case)
  INSERT INTO approval_callbacks (approval_id, late_token) VALUES (p_approval, p_token)
  ON CONFLICT (approval_id) DO UPDATE SET late_token = EXCLUDED.late_token, updated_at = clock_timestamp()
    WHERE approval_callbacks.completed_at IS NULL;
  RETURN FOUND;
END $$;

CREATE FUNCTION claim_task_callbacks(p_within_hours integer DEFAULT 24, p_limit integer DEFAULT 50)
RETURNS TABLE (approval_id uuid, task_token text, attempt integer) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r record; n integer;
BEGIN
  IF p_within_hours IS NULL OR p_within_hours < 1 OR p_limit IS NULL OR p_limit < 1 THEN
    RAISE EXCEPTION 'BAD_CLAIM' USING ERRCODE = 'P0001'; END IF;
  FOR r IN SELECT ar.approval_id AS aid, coalesce(ar.task_token, cb.late_token) AS tok
             FROM approval_requests ar LEFT JOIN approval_callbacks cb ON cb.approval_id = ar.approval_id
            WHERE ar.status <> 'OPEN' AND coalesce(ar.task_token, cb.late_token) IS NOT NULL
              AND cb.completed_at IS NULL
              AND ar.decided_at > clock_timestamp() - make_interval(hours => p_within_hours)
              AND (cb.next_at IS NULL OR cb.next_at <= clock_timestamp())
            ORDER BY ar.decided_at LIMIT p_limit LOOP
    -- The lease. A concurrent claimer re-checks the row under its lock and finds it not due, so it skips it.
    -- Backoff 1, 2, 4, 8, 16, then 30 minutes. The exponent is capped BEFORE the integer cast (review E4-02): with
    -- power(2, attempts)::integer, attempts >= 31 overflowed integer before least() could cap it.
    INSERT INTO approval_callbacks AS c (approval_id, attempts, next_at)
    VALUES (r.aid, 1, clock_timestamp() + interval '1 minute')
    ON CONFLICT ON CONSTRAINT approval_callbacks_pkey DO UPDATE
       SET attempts = c.attempts + 1,
           next_at = clock_timestamp() + make_interval(mins => least(30, power(2, least(c.attempts, 5))::integer)),
           updated_at = clock_timestamp()
     WHERE c.completed_at IS NULL AND (c.next_at IS NULL OR c.next_at <= clock_timestamp())
    RETURNING c.attempts INTO n;
    IF FOUND THEN
      approval_id := r.aid; task_token := r.tok; attempt := n;
      RETURN NEXT;
    END IF;
  END LOOP;
END $$;

CREATE FUNCTION complete_task_callback(p_approval uuid, p_outcome text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF p_outcome NOT IN ('DELIVERED', 'TASK_FINISHED') THEN
    RAISE EXCEPTION 'BAD_CALLBACK_OUTCOME: %', p_outcome USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM approval_requests WHERE approval_id = p_approval AND status <> 'OPEN';
  IF NOT FOUND THEN RETURN false; END IF;                       -- an OPEN request has nothing to complete
  INSERT INTO approval_callbacks AS c (approval_id, completed_at, outcome) VALUES (p_approval, clock_timestamp(), p_outcome)
  ON CONFLICT ON CONSTRAINT approval_callbacks_pkey DO UPDATE
     SET completed_at = clock_timestamp(), outcome = p_outcome, next_at = NULL, updated_at = clock_timestamp()
   WHERE c.completed_at IS NULL;
  RETURN FOUND;
END $$;

ALTER FUNCTION record_late_task_token(uuid, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION claim_task_callbacks(integer, integer) OWNER TO setuhaul_rpc;
ALTER FUNCTION complete_task_callback(uuid, text) OWNER TO setuhaul_rpc;
GRANT SELECT, INSERT, UPDATE ON approval_callbacks TO setuhaul_rpc;       -- no application role touches the table

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_late_task_token(uuid, text)      TO setuhaul_system;
GRANT EXECUTE ON FUNCTION claim_task_callbacks(integer, integer)  TO setuhaul_system;
GRANT EXECUTE ON FUNCTION complete_task_callback(uuid, text)      TO setuhaul_system;
