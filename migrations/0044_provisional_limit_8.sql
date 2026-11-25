-- 0044 · Provisional authorization limit: 4 -> 8 per rolling 24 hours, every environment (OWNER POLICY CHANGE).
--
-- Owner instruction, 26 Nov 2026 ("SetuHaul – Synthetic Pilot Patch Release Authorization", section 2): change the
-- maximum provisional deployment authorization limit from 4 to 8 within any rolling 24-hour period. Nothing else
-- changes. Forward-only: 0034's provisional_rate_limit(text) is replaced with the same signature, owner, privileges
-- and SECURITY DEFINER setting; issue_provisional_authorization (0034) already reads it, so every other rule is
-- untouched and still enforced in the same transaction under the same advisory lock taken before the count:
--   * per-environment counting over a rolling 24 hours (every status counts, revoked and expired included);
--   * one open (ISSUED) authorization per environment;
--   * one authorization per deployment; single-use approval references (UNIQUE, 0032); one recovery per window;
--   * production refused before the count, at any limit;
--   * the append-only history and its audit trigger (no row is changed by this migration).
SET search_path = setuhaul, public;

CREATE OR REPLACE FUNCTION provisional_rate_limit(p_environment text)
RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT 8                       -- per environment (the caller counts one environment); the same for every one
$$;
