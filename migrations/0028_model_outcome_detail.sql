-- 0028 · Phase G.1 (G3, G5): model-use outcome detail for the console. Forward-only and additive: 0001–0027 untouched;
-- no table, no column, no booking, approval, capacity, kill-switch or budget change. One read-only function.
--
--   observability_model_detail(p_since)   counts, sums and the cost source of model_usage rows since p_since (at most 31
--                                         days back), and the UTC budget days touched. What the console needs to keep
--                                         provider-reported cost apart from kept reservations (an ESTIMATE), refusals
--                                         apart from errors, and to show that attempts reconcile:
--                                             attempts = ok + errors + budget_refusals + unsettled
--                                         No prompt, driver, message, key or facility data exists in the ledger.
--
-- Owned by setuhaul_rpc (NOBYPASSRLS), SECURITY DEFINER with a pinned search_path, STABLE (reads only). Returns jsonb,
-- which the RDS Data API carries. Executable by the console and system roles only, like observability_global.
SET search_path = setuhaul, public;

CREATE FUNCTION observability_model_detail(p_since timestamptz) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE since timestamptz := greatest(coalesce(p_since, clock_timestamp() - interval '1 day'),
                                      clock_timestamp() - interval '31 days');
        m jsonb; d jsonb;
BEGIN
  SELECT jsonb_build_object(
           'attempts', count(*),
           'ok', count(*) FILTER (WHERE status = 'OK'),
           'errors', count(*) FILTER (WHERE status = 'ERROR'),
           'budget_refusals', count(*) FILTER (WHERE status = 'BUDGET_EXHAUSTED'),
           'unsettled', count(*) FILTER (WHERE status = 'RESERVED'),
           'calls', count(DISTINCT call_id),
           'sent', count(*) FILTER (WHERE status IN ('OK', 'ERROR') AND cost_source IS DISTINCT FROM 'not_sent'),
           'usage_reported', count(*) FILTER (WHERE input_tokens IS NOT NULL OR output_tokens IS NOT NULL),
           'input_tokens', coalesce(sum(input_tokens), 0),
           'output_tokens', coalesce(sum(output_tokens), 0),
           'cost_provider_usd', coalesce(sum(cost_usd) FILTER (WHERE cost_source = 'provider'), 0)::text,
           'cost_estimated_usd', coalesce(sum(cost_usd) FILTER (WHERE cost_source = 'reservation'), 0)::text,
           'reserved_open_usd', coalesce(sum(reserved_usd) FILTER (WHERE status = 'RESERVED'), 0)::text,
           'by_cost_source', coalesce((SELECT jsonb_object_agg(coalesce(cost_source, 'unsettled'), n) FROM
                                (SELECT cost_source, count(*) n FROM model_usage WHERE created_at >= since
                                  GROUP BY cost_source) s), '{}'),
           'by_error', coalesce((SELECT jsonb_object_agg(error_category, n) FROM
                                (SELECT error_category, count(*) n FROM model_usage WHERE created_at >= since
                                    AND error_category IS NOT NULL GROUP BY error_category) e), '{}'),
           'first_at', min(created_at), 'last_at', max(created_at))
    INTO m FROM model_usage WHERE created_at >= since;
  SELECT coalesce(jsonb_agg(jsonb_build_object('day_utc', day, 'spent_usd', spent_usd::text,
                                               'reserved_usd', reserved_usd::text, 'label', environment)
                            ORDER BY day DESC), '[]')
    INTO d FROM model_budget_day WHERE day >= (since AT TIME ZONE 'UTC')::date;
  RETURN m || jsonb_build_object('since', since, 'budget_days_utc', d,
                                 'budget_day_boundary', '00:00 UTC (05:30 Asia/Kolkata)');
END $$;
ALTER FUNCTION observability_model_detail(timestamptz) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION observability_model_detail(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION observability_model_detail(timestamptz) TO setuhaul_console, setuhaul_system;
