-- 0023 · Phase B (decision T52, 21 Nov 2026): model usage ledger and the application-level daily LLM budget.
-- Forward-only; 0001–0022 untouched. No booking, approval or capacity table changes.
--
-- Every paid model attempt is reserved BEFORE the request is sent and settled after it:
--   reserve_model_call()  inserts one ledger row per attempt (request_id is unique, so an attempt can never be
--                         reserved twice) and adds a conservative reservation to the day's counter, in one statement
--                         that only succeeds while spent + reserved + this reservation <= the cap. The counter row is
--                         locked by that UPDATE, so concurrent attempts cannot pass the cap together.
--   settle_model_call()   records the outcome (tokens, cost, actual provider, latency, error category) and moves the
--                         reservation into spent: the provider-reported cost when it is known, otherwise the whole
--                         reservation (conservative), or zero when the request provably never left the process.
-- The day is the UTC calendar day. The ledger never holds prompts, driver text, extracted data or keys.
-- A reservation whose settle never ran (a crashed process) stays counted as spent-in-waiting: conservative by design.
SET search_path = setuhaul, public;

CREATE TABLE model_budget_day (
  day           date PRIMARY KEY,                               -- UTC
  environment   text NOT NULL,
  reserved_usd  numeric(12,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
  spent_usd     numeric(12,6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE model_usage (
  request_id          uuid PRIMARY KEY,                         -- one row per attempt
  call_id             uuid NOT NULL,                            -- attempts of one logical call share it
  attempt             smallint NOT NULL CHECK (attempt BETWEEN 1 AND 5),
  day                 date NOT NULL,                            -- UTC
  environment         text NOT NULL,
  purpose             text NOT NULL CHECK (purpose IN ('extract', 'phrase')),
  model               text NOT NULL,
  requested_provider  text NOT NULL,
  actual_provider     text,
  status              text NOT NULL CHECK (status IN ('RESERVED', 'OK', 'ERROR', 'BUDGET_EXHAUSTED')),
  error_category      text,
  reserved_usd        numeric(12,6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
  cost_usd            numeric(12,6),                            -- what was charged to the budget at settlement
  cost_source         text CHECK (cost_source IN ('provider', 'reservation', 'not_sent')),
  input_tokens        integer,
  output_tokens       integer,
  latency_ms          integer,
  generation_id       text,                                     -- the provider's response id, for reconciliation
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  settled_at          timestamptz,
  UNIQUE (call_id, attempt)
);
CREATE INDEX model_usage_day ON model_usage (day, status);

CREATE FUNCTION reserve_model_call(p_request uuid, p_call uuid, p_attempt integer, p_environment text, p_purpose text,
                                   p_model text, p_provider text, p_reserve_usd numeric, p_cap_usd numeric)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE d date := (clock_timestamp() AT TIME ZONE 'UTC')::date; n integer; got integer;
BEGIN
  IF p_reserve_usd IS NULL OR p_reserve_usd <= 0 THEN RAISE EXCEPTION 'BAD_RESERVATION' USING ERRCODE = 'P0001'; END IF;
  IF p_cap_usd IS NULL OR p_cap_usd < 0 THEN RAISE EXCEPTION 'BAD_BUDGET' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM model_usage WHERE request_id = p_request OR (call_id = p_call AND attempt = p_attempt);
  IF FOUND THEN RETURN 'DUPLICATE'; END IF;                    -- never reserve the same attempt twice
  INSERT INTO model_budget_day (day, environment) VALUES (d, p_environment) ON CONFLICT (day) DO NOTHING;
  UPDATE model_budget_day SET reserved_usd = reserved_usd + p_reserve_usd, updated_at = clock_timestamp()
   WHERE day = d AND spent_usd + reserved_usd + p_reserve_usd <= p_cap_usd;   -- row lock: concurrent attempts queue here
  GET DIAGNOSTICS got = ROW_COUNT;
  n := got;
  INSERT INTO model_usage (request_id, call_id, attempt, day, environment, purpose, model, requested_provider, status,
                           reserved_usd, error_category, cost_usd, cost_source, settled_at)
  VALUES (p_request, p_call, p_attempt, d, p_environment, p_purpose, p_model, p_provider,
          CASE WHEN n = 1 THEN 'RESERVED' ELSE 'BUDGET_EXHAUSTED' END,
          CASE WHEN n = 1 THEN p_reserve_usd ELSE 0 END,
          CASE WHEN n = 1 THEN NULL ELSE 'budget_exhausted' END,
          CASE WHEN n = 1 THEN NULL ELSE 0 END,
          CASE WHEN n = 1 THEN NULL ELSE 'not_sent' END,
          CASE WHEN n = 1 THEN NULL ELSE clock_timestamp() END)
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 0 THEN                                                 -- a concurrent duplicate won the insert: undo ours
    IF got = 1 THEN
      UPDATE model_budget_day SET reserved_usd = greatest(reserved_usd - p_reserve_usd, 0) WHERE day = d;
    END IF;
    RETURN 'DUPLICATE';
  END IF;
  RETURN (SELECT status FROM model_usage WHERE request_id = p_request);
END $$;
ALTER FUNCTION reserve_model_call(uuid, uuid, integer, text, text, text, text, numeric, numeric) OWNER TO setuhaul_rpc;

CREATE FUNCTION settle_model_call(p_request uuid, p_status text, p_actual_provider text, p_input integer,
                                  p_output integer, p_cost_usd numeric, p_sent boolean, p_latency_ms integer,
                                  p_error text, p_generation text)
RETURNS numeric LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r model_usage; charged numeric; src text;
BEGIN
  IF p_status NOT IN ('OK', 'ERROR') THEN RAISE EXCEPTION 'BAD_STATUS' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO r FROM model_usage WHERE request_id = p_request FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'UNKNOWN_REQUEST' USING ERRCODE = 'P0001'; END IF;
  IF r.status <> 'RESERVED' THEN RETURN coalesce(r.cost_usd, 0); END IF;     -- settled once; a repeat changes nothing
  IF NOT p_sent THEN charged := 0; src := 'not_sent';                          -- provably never left the process
  ELSIF p_cost_usd IS NOT NULL AND p_cost_usd >= 0 THEN charged := p_cost_usd; src := 'provider';
  ELSE charged := r.reserved_usd; src := 'reservation';                        -- unknown cost: keep the reservation
  END IF;
  UPDATE model_budget_day SET reserved_usd = greatest(reserved_usd - r.reserved_usd, 0),
                              spent_usd = spent_usd + charged, updated_at = clock_timestamp()
   WHERE day = r.day;
  UPDATE model_usage SET status = p_status, actual_provider = p_actual_provider, input_tokens = p_input,
                         output_tokens = p_output, cost_usd = charged, cost_source = src, latency_ms = p_latency_ms,
                         error_category = p_error, generation_id = p_generation, settled_at = clock_timestamp()
   WHERE request_id = p_request;
  RETURN charged;
END $$;
ALTER FUNCTION settle_model_call(uuid, text, text, integer, integer, numeric, boolean, integer, text, text) OWNER TO setuhaul_rpc;

-- Only the definer functions touch the ledger. The agent (whose turn makes the model call) may reserve and settle;
-- the system role (monitoring, reconciliation) may read it. No role can edit it directly.
GRANT SELECT, INSERT, UPDATE ON model_budget_day, model_usage TO setuhaul_rpc;
GRANT SELECT ON model_budget_day, model_usage TO setuhaul_system;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reserve_model_call(uuid, uuid, integer, text, text, text, text, numeric, numeric) TO setuhaul_agent;
GRANT EXECUTE ON FUNCTION settle_model_call(uuid, text, text, integer, integer, numeric, boolean, integer, text, text) TO setuhaul_agent;
