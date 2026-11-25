-- 0043 · Online LLM judge for the SYNTHETIC PILOT: advisory judgements of agent replies. Forward-only, additive.
--
-- What it adds (nothing existing changes):
--   * agent_turn_judgements: one row per judged (or deliberately skipped) agent turn, linked to the turn by turn_ref
--     (= the turn's LangSmith trace id and agent_turn_metrics.turn_ref) and to the conversation by correlation_id.
--     Scores are ADVISORY: the row cannot say otherwise (advisory CHECK true), and human calibration is recorded as
--     outstanding. Nothing reads this table to decide, approve, book or authorize anything.
--   * judge_spent_today(): the judge's share of today's usage ledger (0023/0042, purpose 'judge'), so the online judge
--     can enforce ITS OWN daily sub-cap. It is called under a transaction-scoped advisory lock that every judge
--     reservation takes, then reserve_model_call (0023) enforces the shared cap in the same transaction: judge
--     reservations cannot pass the judge cap together, and nothing can pass the shared cap.
--   * record_turn_judgement(jsonb): the only write path (SECURITY DEFINER, owner setuhaul_rpc, execute for
--     setuhaul_agent only). Ids are format-gated like 0041; no message text is stored except the judge's evidence,
--     which validation guarantees is a short exact quote of the ASSISTANT's reply (never the driver's words).
--
-- No new role. No change to booking, authorization, kill switch, ledger functions or any existing grant.

SET search_path = setuhaul, public;

CREATE TABLE agent_turn_judgements (
  turn_ref            uuid PRIMARY KEY,                         -- the agent turn (= its trace id); one judgement each
  correlation_id      text,
  trace_id            uuid,                                     -- the judge's own LangSmith trace (NULL: not traced)
  facility_id         text,
  driver_id           text,
  shipment_id         text,
  status              text NOT NULL CHECK (status IN ('SCORED', 'UNPARSEABLE', 'SKIPPED', 'ERROR')),
  reason              text CHECK (reason IS NULL OR length(reason) <= 200),
  model               text,
  provider            text,
  prompt_version      text NOT NULL,
  eval_version        text NOT NULL,
  clarity             smallint CHECK (clarity BETWEEN 1 AND 5),
  tone                smallint CHECK (tone BETWEEN 1 AND 5),
  language_match      smallint CHECK (language_match BETWEEN 1 AND 5),
  details             jsonb NOT NULL DEFAULT '{}'::jsonb,       -- per dimension: rationale (<=200), evidence (<=120)
  input_tokens        integer,
  output_tokens       integer,
  cost_usd            numeric(12,6),
  latency_ms          integer,
  advisory            boolean NOT NULL DEFAULT true CHECK (advisory),
  calibration_status  text NOT NULL DEFAULT 'HUMAN_CALIBRATION_OUTSTANDING',
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT agent_turn_judgements_driver_id_format CHECK (
    driver_id IS NULL OR (driver_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$' AND driver_id ~ '(^|-)DRV-'
                          AND driver_id !~ '[0-9]{7,}')),
  CONSTRAINT agent_turn_judgements_shipment_id_format CHECK (
    shipment_id IS NULL OR (shipment_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$' AND shipment_id ~ '(^|-)SHP-'
                            AND shipment_id !~ '[0-9]{7,}'))
);
CREATE INDEX ix_agent_turn_judgements_created ON agent_turn_judgements (created_at);
-- like agent_turn_metrics (0027): RLS forced, a facility row is visible and writable only within that facility's scope,
-- and the rows are append-only evidence
ALTER TABLE agent_turn_judgements ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_turn_judgements FORCE ROW LEVEL SECURITY;
CREATE POLICY p_agent_turn_judgements_facility ON agent_turn_judgements
  USING (facility_id IS NULL OR scope_allows(facility_id)) WITH CHECK (facility_id IS NULL OR scope_allows(facility_id));
CREATE TRIGGER agent_turn_judgements_append_only BEFORE UPDATE OR DELETE ON agent_turn_judgements
  FOR EACH ROW EXECUTE FUNCTION trg_phase_g_append_only();
REVOKE ALL ON agent_turn_judgements FROM PUBLIC;
GRANT SELECT ON agent_turn_judgements TO setuhaul_system;

CREATE FUNCTION judge_spent_today() RETURNS numeric
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT coalesce(sum(CASE WHEN status = 'RESERVED' THEN reserved_usd ELSE coalesce(cost_usd, reserved_usd) END), 0)
    FROM model_usage
   WHERE day = (clock_timestamp() AT TIME ZONE 'UTC')::date AND purpose = 'judge' AND status <> 'BUDGET_EXHAUSTED'
$$;
ALTER FUNCTION judge_spent_today() OWNER TO setuhaul_rpc;
REVOKE ALL ON FUNCTION judge_spent_today() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION judge_spent_today() TO setuhaul_agent;

CREATE FUNCTION record_turn_judgement(p jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  drv text := p->>'driver_id';
  shp text := p->>'shipment_id';
  n integer;
BEGIN
  IF drv IS NOT NULL AND NOT (drv ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$' AND drv ~ '(^|-)DRV-' AND drv !~ '[0-9]{7,}') THEN
    drv := NULL;                                               -- like 0041: a malformed id is not stored
  END IF;
  IF shp IS NOT NULL AND NOT (shp ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$' AND shp ~ '(^|-)SHP-' AND shp !~ '[0-9]{7,}') THEN
    shp := NULL;
  END IF;
  INSERT INTO agent_turn_judgements (turn_ref, correlation_id, trace_id, facility_id, driver_id, shipment_id, status,
                                     reason, model, provider, prompt_version, eval_version, clarity, tone,
                                     language_match, details, input_tokens, output_tokens, cost_usd, latency_ms)
  VALUES ((p->>'turn_ref')::uuid, left(p->>'correlation_id', 64), (p->>'trace_id')::uuid, left(p->>'facility_id', 48),
          drv, shp, p->>'status', left(p->>'reason', 200), left(p->>'model', 80), left(p->>'provider', 80),
          left(p->>'prompt_version', 40), left(p->>'eval_version', 60), (p->>'clarity')::smallint,
          (p->>'tone')::smallint, (p->>'language_match')::smallint, coalesce(p->'details', '{}'::jsonb),
          (p->>'input_tokens')::integer, (p->>'output_tokens')::integer, (p->>'cost_usd')::numeric,
          (p->>'latency_ms')::integer)
  ON CONFLICT (turn_ref) DO NOTHING;                           -- a redelivered turn is judged at most once
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n = 1;
END $$;
ALTER FUNCTION record_turn_judgement(jsonb) OWNER TO setuhaul_rpc;
REVOKE ALL ON FUNCTION record_turn_judgement(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_turn_judgement(jsonb) TO setuhaul_agent;
GRANT SELECT, INSERT ON agent_turn_judgements TO setuhaul_rpc;
