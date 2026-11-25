-- 0041 · Driver traceability: agent_turn_metrics records which driver and shipment a turn belonged to. Forward-only,
-- additive.
--
-- Two nullable columns, driver_id and shipment_id. Existing rows keep NULL and stay valid. The same format gate as
-- setuhaul/observability/identifiers.py applies (same regular expressions):
--   * 3-48 characters of [A-Za-z0-9_-], starting with a letter or digit;
--   * a driver id contains the token DRV-, a shipment id the token SHP-;
--   * no run of 7 or more digits (a phone number or a Telegram chat id cannot hide in an id).
-- An id is stored only on a row that has a facility_id. A row without a facility (a turn before a shipment was known)
-- is visible to every staff scope under the existing RLS policy, so it must never name a driver. The CHECK enforces
-- this; record_agent_turn stores NULL instead of raising, because turn metrics are best effort and must not be lost.
--
-- The gate is a format check, not proof that an id is synthetic: tracing and the pilot stay synthetic-only by their
-- own controls (SETUHAUL_DATA_CLASS=synthetic).
--
-- Unchanged: the table's RLS policy (facility scope), its append-only trigger, the grants (SELECT for the console and
-- system roles, record_agent_turn for setuhaul_feasibility only), the function's owner (setuhaul_rpc). No new role,
-- no new grant. Who reads it: /console/observability, which only planner and ops_lead may call (view_observability).
--
-- Recovery: forward-only like every migration. If the columns had to go, a later migration would restore the 0027
-- body of record_agent_turn and then drop the two constraints, the index and the columns. Dropping them loses no
-- other data, and nothing else reads them.

SET search_path = setuhaul, public;

ALTER TABLE agent_turn_metrics
  ADD COLUMN driver_id   text,
  ADD COLUMN shipment_id text;

ALTER TABLE agent_turn_metrics ADD CONSTRAINT agent_turn_metrics_driver_id_format CHECK (
  driver_id IS NULL OR (facility_id IS NOT NULL AND driver_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$'
                        AND driver_id ~ '(^|-)DRV-' AND driver_id !~ '[0-9]{7,}'));
ALTER TABLE agent_turn_metrics ADD CONSTRAINT agent_turn_metrics_shipment_id_format CHECK (
  shipment_id IS NULL OR (facility_id IS NOT NULL AND shipment_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$'
                          AND shipment_id ~ '(^|-)SHP-' AND shipment_id !~ '[0-9]{7,}'));

CREATE INDEX ix_agent_turn_metrics_driver ON agent_turn_metrics (driver_id, created_at DESC) WHERE driver_id IS NOT NULL;

CREATE OR REPLACE FUNCTION record_agent_turn(p_turn jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ref uuid := (p_turn->>'turn_ref')::uuid; fac text := nullif(p_turn->>'facility_id', ''); t jsonb;
        drv text := nullif(p_turn->>'driver_id', ''); shp text := nullif(p_turn->>'shipment_id', '');
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
  -- 0041: ids only on a facility row, only when well-formed; otherwise NULL (never the value, never a substitute)
  IF fac IS NULL OR drv IS NULL OR drv !~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$' OR drv !~ '(^|-)DRV-' OR drv ~ '[0-9]{7,}' THEN
    drv := NULL;
  END IF;
  IF fac IS NULL OR shp IS NULL OR shp !~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$' OR shp !~ '(^|-)SHP-' OR shp ~ '[0-9]{7,}' THEN
    shp := NULL;
  END IF;
  INSERT INTO agent_turn_metrics (turn_ref, facility_id, extractor, fallback_reason, tools, turn_ms, model_ms, escalated,
                                  driver_id, shipment_id)
  VALUES (ref, fac, p_turn->>'extractor', left(p_turn->>'fallback_reason', 60), coalesce(p_turn->'tools', '[]'),
          (p_turn->>'turn_ms')::integer, (p_turn->>'model_ms')::integer, coalesce((p_turn->>'escalated')::boolean, false),
          drv, shp)
  ON CONFLICT (turn_ref) DO NOTHING;                          -- a redelivered turn is recorded once
  RETURN ref;
END $$;
ALTER FUNCTION record_agent_turn(jsonb) OWNER TO setuhaul_rpc;
