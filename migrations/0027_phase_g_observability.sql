-- 0027 · Phase G (G1 optimizer, G2 evaluation, G3 observability). Forward-only and additive: 0001–0026 untouched;
-- no booking, approval, capacity or kill-switch table or function changes. Nothing here can create, move or confirm a
-- booking.
--
--   optimization_runs      one row per optimizer run (G1): structured summary only (status, counts, timings, the top
--                          option, the input fingerprint). No driver, phone or message data. RLS by facility.
--   agent_turn_metrics     one row per orchestrator turn (G3): which tools ran with what status and latency, which
--                          reader (model or rules fallback), turn latency. Correlation id only, no driver id, phone or
--                          text. RLS by facility (CENTRAL turns, facility_id NULL, carry no facility data).
--   evaluation_runs        one row per Phase G evaluation run (G2): totals and per-category results. No facility data.
--   observability_global() read-only aggregate (G3) over the tables that have no facility column (model usage ledger,
--                          idempotency keys, approval callbacks): counts, sums and percentiles only.
--
-- Writers are SECURITY DEFINER functions owned by setuhaul_rpc (NOBYPASSRLS) with a pinned search_path; they validate
-- every value. The service records runs and turns through the feasibility role (the agent workload already has that
-- login; the agent role gains nothing) and console runs through the console role. Every function returns only types
-- the RDS Data API can carry (uuid, jsonb, bigint): no range, interval or "char".
SET search_path = setuhaul, public;

-- ================================================================ G1: optimizer runs
CREATE TABLE optimization_runs (
  run_id                uuid PRIMARY KEY,
  request_id            text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 80),
  facility_id           text NOT NULL REFERENCES facilities,
  shipment_id           text NOT NULL REFERENCES shipments,
  caller                text NOT NULL CHECK (caller IN ('agent', 'console', 'eval', 'perf')),
  engine                text NOT NULL CHECK (length(engine) BETWEEN 3 AND 60),
  solver_status         text NOT NULL CHECK (solver_status IN ('OPTIMAL','FEASIBLE','INFEASIBLE','UNKNOWN','MODEL_INVALID',
                                                                'STALE_DATA','ERROR')),
  outcome_code          text NOT NULL CHECK (length(outcome_code) BETWEEN 1 AND 40),
  recommended_action    text NOT NULL CHECK (recommended_action IN ('OFFER','OFFER_WITH_APPROVAL','ESCALATE','REFRESH_DATA','NONE')),
  options_count         integer NOT NULL CHECK (options_count BETWEEN 0 AND 50),
  rejected_count        integer NOT NULL CHECK (rejected_count >= 0),
  candidates_evaluated  integer NOT NULL CHECK (candidates_evaluated >= 0),
  duration_ms           integer NOT NULL CHECK (duration_ms >= 0),
  input_fingerprint     text CHECK (input_fingerprint IS NULL OR input_fingerprint ~ '^[0-9a-f]{64}$'),
  schedule_version      bigint,
  data_as_of            timestamptz,
  top_dock_id           text,
  top_start             timestamptz,
  top_score             bigint,
  warnings              integer NOT NULL DEFAULT 0 CHECK (warnings >= 0),
  fallback_used         boolean NOT NULL DEFAULT false,         -- the service fell back to the plain engine order
  created_at            timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_optimization_runs_facility ON optimization_runs (facility_id, created_at DESC);

-- ================================================================ G3: agent turn metrics
CREATE TABLE agent_turn_metrics (
  turn_ref        uuid PRIMARY KEY,                    -- the turn's correlation id (also in the structured logs)
  facility_id     text REFERENCES facilities,          -- NULL: a turn before a shipment was known (CENTRAL)
  extractor       text NOT NULL CHECK (extractor IN ('model', 'rules-fallback', 'rules', 'none')),
  fallback_reason text CHECK (fallback_reason IS NULL OR length(fallback_reason) <= 60),
  tools           jsonb NOT NULL CHECK (jsonb_typeof(tools) = 'array' AND jsonb_array_length(tools) <= 20),
  turn_ms         integer NOT NULL CHECK (turn_ms >= 0),
  model_ms        integer CHECK (model_ms IS NULL OR model_ms >= 0),
  escalated       boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_agent_turn_metrics_time ON agent_turn_metrics (created_at DESC);

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['optimization_runs'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY p_%s_facility ON %I USING (scope_allows(facility_id)) WITH CHECK (scope_allows(facility_id))', t, t);
  END LOOP;
END $$;
ALTER TABLE agent_turn_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_turn_metrics FORCE ROW LEVEL SECURITY;
CREATE POLICY p_agent_turn_metrics_facility ON agent_turn_metrics
  USING (facility_id IS NULL OR scope_allows(facility_id)) WITH CHECK (facility_id IS NULL OR scope_allows(facility_id));

-- ================================================================ G2: evaluation runs (no facility data)
CREATE TABLE evaluation_runs (
  run_id           uuid PRIMARY KEY,
  suite            text NOT NULL CHECK (length(suite) BETWEEN 3 AND 60),
  mode             text NOT NULL CHECK (mode IN ('scripted', 'live')),
  dataset_version  text NOT NULL CHECK (length(dataset_version) BETWEEN 1 AND 60),
  total            integer NOT NULL CHECK (total >= 0),
  passed           integer NOT NULL CHECK (passed >= 0),
  failed           integer NOT NULL CHECK (failed >= 0),
  skipped          integer NOT NULL CHECK (skipped >= 0),
  summary          jsonb NOT NULL CHECK (jsonb_typeof(summary) = 'object'),
  cost_usd         numeric(12,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  code_version     text CHECK (code_version IS NULL OR length(code_version) <= 64),
  synthetic        boolean NOT NULL DEFAULT true CHECK (synthetic),       -- evaluation data is always synthetic
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz NOT NULL,
  recorded_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (passed + failed + skipped = total),
  CHECK (finished_at >= started_at)
);

-- the three Phase G tables are append-only evidence
CREATE FUNCTION trg_phase_g_append_only() RETURNS trigger LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'APPEND_ONLY: % rows are never changed or deleted', TG_TABLE_NAME USING ERRCODE = 'P0001';
END $$;
CREATE TRIGGER optimization_runs_append_only BEFORE UPDATE OR DELETE ON optimization_runs
  FOR EACH ROW EXECUTE FUNCTION trg_phase_g_append_only();
CREATE TRIGGER agent_turn_metrics_append_only BEFORE UPDATE OR DELETE ON agent_turn_metrics
  FOR EACH ROW EXECUTE FUNCTION trg_phase_g_append_only();
CREATE TRIGGER evaluation_runs_append_only BEFORE UPDATE OR DELETE ON evaluation_runs
  FOR EACH ROW EXECUTE FUNCTION trg_phase_g_append_only();

-- ================================================================ writers
CREATE FUNCTION record_optimization_run(p_run jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE rid uuid := gen_random_uuid(); fac text := p_run->>'facility_id';
BEGIN
  IF p_run IS NULL OR jsonb_typeof(p_run) <> 'object' THEN RAISE EXCEPTION 'BAD_RUN' USING ERRCODE = 'P0001'; END IF;
  IF fac IS NULL OR NOT scope_allows(fac) THEN RAISE EXCEPTION 'FORBIDDEN: facility outside scope' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM shipments WHERE shipment_id = p_run->>'shipment_id' AND destination_facility_id = fac;
  IF NOT FOUND THEN RAISE EXCEPTION 'CROSS_FACILITY: shipment is not bound for this facility' USING ERRCODE = 'P0001'; END IF;
  IF p_run->>'top_dock_id' IS NOT NULL THEN
    PERFORM 1 FROM docks WHERE dock_id = p_run->>'top_dock_id' AND facility_id = fac;
    IF NOT FOUND THEN RAISE EXCEPTION 'CROSS_FACILITY: dock is not at this facility' USING ERRCODE = 'P0001'; END IF;
  END IF;
  INSERT INTO optimization_runs (run_id, request_id, facility_id, shipment_id, caller, engine, solver_status, outcome_code,
                                 recommended_action, options_count, rejected_count, candidates_evaluated, duration_ms,
                                 input_fingerprint, schedule_version, data_as_of, top_dock_id, top_start, top_score,
                                 warnings, fallback_used)
  VALUES (rid, left(p_run->>'request_id', 80), fac, p_run->>'shipment_id', p_run->>'caller', p_run->>'engine',
          p_run->>'solver_status', p_run->>'outcome_code', p_run->>'recommended_action',
          (p_run->>'options_count')::integer, (p_run->>'rejected_count')::integer,
          (p_run->>'candidates_evaluated')::integer, (p_run->>'duration_ms')::integer,
          nullif(p_run->>'input_fingerprint', ''), (p_run->>'schedule_version')::bigint,
          (p_run->>'data_as_of')::timestamptz, p_run->>'top_dock_id', (p_run->>'top_start')::timestamptz,
          (p_run->>'top_score')::bigint, coalesce((p_run->>'warnings')::integer, 0),
          coalesce((p_run->>'fallback_used')::boolean, false));
  RETURN rid;
END $$;
ALTER FUNCTION record_optimization_run(jsonb) OWNER TO setuhaul_rpc;

CREATE FUNCTION record_agent_turn(p_turn jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ref uuid := (p_turn->>'turn_ref')::uuid; fac text := nullif(p_turn->>'facility_id', ''); t jsonb;
BEGIN
  IF p_turn IS NULL OR jsonb_typeof(p_turn) <> 'object' OR ref IS NULL THEN RAISE EXCEPTION 'BAD_TURN' USING ERRCODE = 'P0001'; END IF;
  IF fac IS NOT NULL AND NOT scope_allows(fac) THEN RAISE EXCEPTION 'FORBIDDEN: facility outside scope' USING ERRCODE = 'P0001'; END IF;
  -- only tool name, status and latency per call: anything else (arguments, text, ids) is refused, never stored
  FOR t IN SELECT * FROM jsonb_array_elements(coalesce(p_turn->'tools', '[]')) LOOP
    IF jsonb_typeof(t) <> 'object' OR (SELECT count(*) FROM jsonb_object_keys(t) k WHERE k NOT IN ('tool','status','ms')) > 0
       OR coalesce(t->>'tool', '') !~ '^[a-z_]{3,40}$' OR coalesce(t->>'status', '') !~ '^[A-Z_]{2,30}$'
       OR coalesce((t->>'ms')::integer, -1) < 0 THEN
      RAISE EXCEPTION 'BAD_TURN: tool entries carry tool, status and ms only' USING ERRCODE = 'P0001';
    END IF;
  END LOOP;
  INSERT INTO agent_turn_metrics (turn_ref, facility_id, extractor, fallback_reason, tools, turn_ms, model_ms, escalated)
  VALUES (ref, fac, p_turn->>'extractor', left(p_turn->>'fallback_reason', 60), coalesce(p_turn->'tools', '[]'),
          (p_turn->>'turn_ms')::integer, (p_turn->>'model_ms')::integer, coalesce((p_turn->>'escalated')::boolean, false))
  ON CONFLICT (turn_ref) DO NOTHING;                          -- a redelivered turn is recorded once
  RETURN ref;
END $$;
ALTER FUNCTION record_agent_turn(jsonb) OWNER TO setuhaul_rpc;

CREATE FUNCTION record_evaluation_run(p_run jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE rid uuid := gen_random_uuid();
BEGIN
  IF p_run IS NULL OR jsonb_typeof(p_run) <> 'object' THEN RAISE EXCEPTION 'BAD_RUN' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO evaluation_runs (run_id, suite, mode, dataset_version, total, passed, failed, skipped, summary, cost_usd,
                               code_version, started_at, finished_at)
  VALUES (rid, p_run->>'suite', p_run->>'mode', p_run->>'dataset_version', (p_run->>'total')::integer,
          (p_run->>'passed')::integer, (p_run->>'failed')::integer, (p_run->>'skipped')::integer,
          coalesce(p_run->'summary', '{}'), coalesce((p_run->>'cost_usd')::numeric, 0), left(p_run->>'code_version', 64),
          (p_run->>'started_at')::timestamptz, (p_run->>'finished_at')::timestamptz);
  RETURN rid;
END $$;
ALTER FUNCTION record_evaluation_run(jsonb) OWNER TO setuhaul_rpc;

-- ================================================================ G3: global aggregates (tables with no facility column)
CREATE FUNCTION observability_global(p_since timestamptz) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE since timestamptz := greatest(coalesce(p_since, clock_timestamp() - interval '1 day'),
                                      clock_timestamp() - interval '31 days');
        m jsonb; k jsonb; cb jsonb;
BEGIN
  SELECT jsonb_build_object(
           'calls', count(*),
           'ok', count(*) FILTER (WHERE status = 'OK'),
           'errors', count(*) FILTER (WHERE status = 'ERROR'),
           'budget_refusals', count(*) FILTER (WHERE status = 'BUDGET_EXHAUSTED'),
           'unsettled', count(*) FILTER (WHERE status = 'RESERVED'),
           'input_tokens', coalesce(sum(input_tokens), 0),
           'output_tokens', coalesce(sum(output_tokens), 0),
           'cost_usd', coalesce(sum(cost_usd), 0)::text,
           'latency_p50_ms', percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE latency_ms IS NOT NULL),
           'latency_p95_ms', percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE latency_ms IS NOT NULL),
           'by_model', coalesce((SELECT jsonb_object_agg(model, n) FROM (SELECT model, count(*) n FROM model_usage
                                  WHERE created_at >= since GROUP BY model) x), '{}'),
           'by_error', coalesce((SELECT jsonb_object_agg(error_category, n) FROM (SELECT error_category, count(*) n
                                  FROM model_usage WHERE created_at >= since AND error_category IS NOT NULL
                                  GROUP BY error_category) y), '{}'))
    INTO m FROM model_usage WHERE created_at >= since;
  SELECT jsonb_build_object('today_spent_usd', coalesce(max(spent_usd) FILTER (WHERE day = (clock_timestamp() AT TIME ZONE 'UTC')::date), 0)::text,
                            'today_reserved_usd', coalesce(max(reserved_usd) FILTER (WHERE day = (clock_timestamp() AT TIME ZONE 'UTC')::date), 0)::text)
    INTO k FROM model_budget_day;
  m := m || k;
  SELECT jsonb_build_object('keys', count(*), 'in_progress', count(*) FILTER (WHERE status <> 'DONE'))
    INTO k FROM idempotency_keys WHERE created_at >= since;
  SELECT jsonb_build_object('callbacks', count(*), 'retried', count(*) FILTER (WHERE attempts > 1),
                            'open', count(*) FILTER (WHERE completed_at IS NULL),
                            'by_outcome', coalesce((SELECT jsonb_object_agg(coalesce(outcome, 'PENDING'), n) FROM
                               (SELECT outcome, count(*) n FROM approval_callbacks WHERE updated_at >= since GROUP BY outcome) z), '{}'))
    INTO cb FROM approval_callbacks WHERE updated_at >= since;
  RETURN jsonb_build_object('since', since, 'model_usage', m, 'idempotency', k, 'approval_callbacks', cb);
END $$;
ALTER FUNCTION observability_global(timestamptz) OWNER TO setuhaul_rpc;

-- ================================================================ grants
GRANT SELECT, INSERT ON optimization_runs, agent_turn_metrics, evaluation_runs TO setuhaul_rpc;
GRANT SELECT ON shipments, docks, model_usage, model_budget_day, idempotency_keys, approval_callbacks TO setuhaul_rpc;
GRANT SELECT ON optimization_runs, agent_turn_metrics, evaluation_runs TO setuhaul_console, setuhaul_system;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
-- the service records its optimizer runs and agent turns (feasibility login: the agent workload has it; the agent
-- role does not get these); the console records its own optimizer runs; evaluation runs come from the system role
GRANT EXECUTE ON FUNCTION record_optimization_run(jsonb) TO setuhaul_feasibility, setuhaul_console;
GRANT EXECUTE ON FUNCTION record_agent_turn(jsonb)       TO setuhaul_feasibility;
GRANT EXECUTE ON FUNCTION record_evaluation_run(jsonb)   TO setuhaul_system;
GRANT EXECUTE ON FUNCTION observability_global(timestamptz) TO setuhaul_console, setuhaul_system;
