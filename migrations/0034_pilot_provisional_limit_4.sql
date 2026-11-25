-- 0034 · Synthetic-pilot provisional authorization limit: 2 -> 4 per rolling 24 hours (OWNER POLICY CHANGE).
--
-- REQUIRES THE OWNER'S SEPARATE WRITTEN APPROVAL BEFORE IT IS MERGED OR DEPLOYED. It changes a booking-authorization
-- safety control (Option P, 0032; Recovery Release A, 0033) and is outside every earlier deployment approval,
-- including OWNER-APPROVAL-20261125-G2LS-7KQ4. Design: docs/booking-renewal/OPTION_P_PROVISIONAL_AUTHORIZATION_DESIGN.md.
--
-- Forward-only and additive. One new function, provisional_rate_limit(environment), states the limit: 4 for an
-- environment of class 'pilot', 2 otherwise (the issuing function already refuses every environment that is not a
-- pilot, production included, so production policy is unchanged). issue_provisional_authorization (the 7-argument
-- form, 0033) is replaced with the same signature and a body identical to 0033 except the rate check, which now
-- reads that function. Everything else is unchanged and still enforced in the same transaction, under the same
-- transaction-level advisory lock taken before the count:
--   * per-environment counting over a rolling 24 hours (issued_at > clock_timestamp() - 24 h, every status counts);
--   * one open (ISSUED) authorization per environment;
--   * one authorization per deployment, single-use approval references, one recovery per revoked window
--     (the UNIQUE constraints and index of 0032/0033);
--   * the append-only history and its audit trigger (no row is changed by this migration).
-- The 6-argument wrapper (0033) calls the 7-argument form and needs no change.
SET search_path = setuhaul, public;

CREATE FUNCTION provisional_rate_limit(p_environment text)
RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT CASE WHEN booking_environment_class(p_environment) = 'pilot' THEN 4 ELSE 2 END
$$;

CREATE OR REPLACE FUNCTION issue_provisional_authorization(p_environment text, p_deployment text, p_release_sha text,
                                                p_approval_ref text, p_pipeline_run text, p_minutes integer,
                                                p_recovery_of bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  g record; m record; prev record; pr record; cur record; rv record; now_ timestamptz; n integer;
  r provisional_authorizations; ev text[];
BEGIN
  SELECT enforced, identity_enforced INTO g FROM booking_evidence_guard WHERE id = 1;
  IF NOT coalesce(g.enforced, false) THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: the booking evidence guard is off' USING ERRCODE = 'P0001';
  END IF;
  IF NOT coalesce(g.identity_enforced, false) THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: deployment identity (M2) is not enforced in this database'
      USING ERRCODE = 'P0001';
  END IF;
  IF booking_environment_class(p_environment) <> 'pilot' THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: only a pilot environment, never production (got class %)',
      booking_environment_class(p_environment) USING ERRCODE = 'P0001';
  END IF;
  IF p_minutes IS NULL OR p_minutes < 1 OR p_minutes > 120 THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: the window is 1 to 120 minutes (got %)', p_minutes USING ERRCODE = 'P0001';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('setuhaul.booking_evidence_guard', 0));
  now_ := clock_timestamp();
  UPDATE provisional_authorizations SET status = 'EXPIRED', closed_by = 'expiry',
         close_reason = 'window ended without BOOKING_WORKFLOW evidence'
   WHERE environment = p_environment AND status = 'ISSUED' AND expires_at <= now_;
  SELECT deployment_id, artifact_sha256, recorded_at INTO m
    FROM deployment_manifests WHERE environment = p_environment ORDER BY manifest_id DESC LIMIT 1;
  IF NOT FOUND OR m.deployment_id <> p_deployment THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: % is not the newest recorded deployment of %', left(p_deployment, 14),
      p_environment USING ERRCODE = 'P0001';
  END IF;
  SELECT deployment_id, recorded_at INTO prev FROM deployment_manifests
   WHERE environment = p_environment AND deployment_id <> p_deployment ORDER BY manifest_id DESC LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: no previous release is recorded for %', p_environment USING ERRCODE = 'P0001';
  END IF;
  SELECT * INTO pr FROM booking_gate_problems(p_environment, prev.deployment_id, prev.recorded_at, now_, NULL);
  IF cardinality(pr.problems) = 0 THEN
    IF p_recovery_of IS NOT NULL THEN
      RAISE EXCEPTION 'RECOVERY_NOT_APPLICABLE: the previous release % has complete evidence; issue normally',
        left(prev.deployment_id, 14) USING ERRCODE = 'P0001';
    END IF;
  ELSIF p_recovery_of IS NULL THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: the previous release % has no complete evidence (%)',
      left(prev.deployment_id, 14), array_to_string(pr.problems, '; ') USING ERRCODE = 'P0001';
  ELSE
    -- the recovery exception: every condition, or nothing
    SELECT * INTO rv FROM provisional_authorizations WHERE authorization_id = p_recovery_of;
    IF NOT FOUND OR rv.environment <> p_environment OR rv.deployment_id <> prev.deployment_id THEN
      RAISE EXCEPTION 'RECOVERY_MISMATCH: authorization % is not the previous release''s (% in %)', p_recovery_of,
        left(prev.deployment_id, 14), p_environment USING ERRCODE = 'P0001';
    END IF;
    IF rv.status <> 'REVOKED' THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % is %, only a REVOKED window can be recovered',
        p_recovery_of, rv.status USING ERRCODE = 'P0001';
    END IF;
    IF rv.recovery_of IS NOT NULL THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % was itself a recovery; no second hop (full activation)',
        p_recovery_of USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM provisional_authorizations WHERE recovery_of = p_recovery_of) THEN
      RAISE EXCEPTION 'RECOVERY_USED: authorization % has already been recovered once', p_recovery_of
        USING ERRCODE = 'P0001';
    END IF;
    IF rv.closed_at < now_ - interval '7 days' THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % was revoked at %, more than 7 days ago (full activation)',
        p_recovery_of, rv.closed_at USING ERRCODE = 'P0001';
    END IF;
    ev := ARRAY(SELECT action FROM provisional_authorization_events WHERE authorization_id = p_recovery_of
                 ORDER BY event_id);
    IF ev IS DISTINCT FROM ARRAY['ISSUED', 'REVOKED'] THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: the audit trail of authorization % is not ISSUED, REVOKED (got %)',
        p_recovery_of, ev USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(pr.problems) x WHERE x NOT LIKE 'BOOKING_WORKFLOW %') THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: the previous release lacks more than BOOKING_WORKFLOW (%) (full activation)',
        array_to_string(pr.problems, '; ') USING ERRCODE = 'P0001';
    END IF;
  END IF;
  SELECT * INTO cur FROM booking_gate_problems(p_environment, p_deployment, m.recorded_at, now_, 'BOOKING_WORKFLOW');
  IF cardinality(cur.problems) > 0 THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: the other gates are not current (%)', array_to_string(cur.problems, '; ')
      USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM unnest(cur.refs) x WHERE x <> p_approval_ref) THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: the exceptions of this release name another owner approval'
      USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM provisional_authorizations WHERE environment = p_environment AND status = 'ISSUED') THEN
    RAISE EXCEPTION 'PROVISIONAL_OPEN: an authorization is already open for %', p_environment USING ERRCODE = 'P0001';
  END IF;
  SELECT count(*) INTO n FROM provisional_authorizations
   WHERE environment = p_environment AND issued_at > now_ - interval '24 hours';
  IF n >= provisional_rate_limit(p_environment) THEN                       -- 0034: 4 for a pilot (was 2)
    RAISE EXCEPTION 'PROVISIONAL_RATE_LIMIT: % authorizations issued for % in the last 24 hours (maximum %)', n,
      p_environment, provisional_rate_limit(p_environment) USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM provisional_authorizations WHERE environment = p_environment
                                                         AND deployment_id = p_deployment) THEN
    RAISE EXCEPTION 'PROVISIONAL_ALREADY_ISSUED: one authorization per deployment (%)', left(p_deployment, 14)
      USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM provisional_authorizations WHERE approval_ref = p_approval_ref) THEN
    RAISE EXCEPTION 'PROVISIONAL_APPROVAL_USED: % has already authorized a provisional window', p_approval_ref
      USING ERRCODE = 'P0001';
  END IF;
  INSERT INTO provisional_authorizations (environment, deployment_id, code_sha256, predecessor_deployment, release_sha,
                                          approval_ref, pipeline_run, issued_by, issued_at, expires_at, recovery_of)
  VALUES (p_environment, p_deployment, m.artifact_sha256, prev.deployment_id, p_release_sha, p_approval_ref,
          p_pipeline_run, session_user, now_, now_ + make_interval(mins => p_minutes), p_recovery_of)
  RETURNING * INTO r;
  RETURN jsonb_build_object('authorization_id', r.authorization_id, 'environment', r.environment,
                            'deployment_id', r.deployment_id, 'issued_at', r.issued_at, 'expires_at', r.expires_at,
                            'predecessor_deployment', r.predecessor_deployment, 'approval_ref', r.approval_ref,
                            'recovery_of', r.recovery_of);
END $$;

-- ================================================================ ownership and grants (as 0033)
ALTER FUNCTION provisional_rate_limit(text) OWNER TO setuhaul_rpc;
ALTER FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION provisional_rate_limit(text),
                           issue_provisional_authorization(text, text, text, text, text, integer, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint)
  TO setuhaul_deploy_evidence;
-- read-only: the pipeline's readiness check reads the limit the database enforces
GRANT EXECUTE ON FUNCTION provisional_rate_limit(text) TO setuhaul_deploy_evidence;
