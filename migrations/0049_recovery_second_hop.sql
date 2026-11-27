-- 0049 · one additional recovery hop for the synthetic pilot (owner, 27 Nov 2026, OWNER-APPROVAL-20261127-DEMO-03).
-- Forward-only. Replaces issue_provisional_authorization (0045 body, same signature, owner and grants) with one change:
-- a recovery may be recovered once more when the authorization it recovered was not itself a recovery (at most two
-- hops in a chain). Unchanged: pilot only (never production), the evidence guard and deployment identity, REVOKED or
-- owner-approved EXPIRED only, once per authorization (RECOVERY_USED and ux_provisional_recovery_once), 7 days, the
-- ISSUED/<status> audit trail, only BOOKING_WORKFLOW missing on the previous release, the other gates current, one
-- open window, the rate limit, one authorization per deployment, single-use approval references.
SET search_path = setuhaul, public;

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
    IF rv.status NOT IN ('REVOKED', 'EXPIRED') THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % is %, only a REVOKED or an owner-approved EXPIRED window can be recovered',
        p_recovery_of, rv.status USING ERRCODE = 'P0001';
    END IF;
    -- 0045: an EXPIRED window is recoverable only when the owner approved recovering exactly that authorization
    IF rv.status = 'EXPIRED' AND NOT EXISTS (SELECT 1 FROM provisional_expired_recovery_approvals x
                                              WHERE x.authorization_id = p_recovery_of
                                                AND x.environment = rv.environment
                                                AND x.deployment_id = rv.deployment_id) THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % expired; an EXPIRED window is recoverable only with the owner''s recorded approval for that authorization',
        p_recovery_of USING ERRCODE = 'P0001';
    END IF;
    -- 0049 (owner, 27 Nov 2026, OWNER-APPROVAL-20261127-DEMO-03): exactly ONE additional hop. A recovery may itself
    -- be recovered only when the authorization IT recovered was not a recovery (at most two hops in any chain);
    -- every other condition below (pilot only, REVOKED or owner-approved EXPIRED, once per authorization, 7 days,
    -- audit trail, only BOOKING_WORKFLOW missing) is unchanged.
    IF rv.recovery_of IS NOT NULL AND (SELECT x.recovery_of FROM provisional_authorizations x
                                        WHERE x.authorization_id = rv.recovery_of) IS NOT NULL THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % is already a second recovery; no third hop (full activation)',
        p_recovery_of USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM provisional_authorizations WHERE recovery_of = p_recovery_of) THEN
      RAISE EXCEPTION 'RECOVERY_USED: authorization % has already been recovered once', p_recovery_of
        USING ERRCODE = 'P0001';
    END IF;
    IF rv.status = 'REVOKED' AND rv.closed_at < now_ - interval '7 days' THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % was revoked at %, more than 7 days ago (full activation)',
        p_recovery_of, rv.closed_at USING ERRCODE = 'P0001';
    END IF;
    IF rv.status = 'EXPIRED' AND rv.expires_at < now_ - interval '7 days' THEN       -- 0045: seven days from expiry
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % expired at %, more than 7 days ago (full activation)',
        p_recovery_of, rv.expires_at USING ERRCODE = 'P0001';
    END IF;
    ev := ARRAY(SELECT action FROM provisional_authorization_events WHERE authorization_id = p_recovery_of
                 ORDER BY event_id);
    IF ev IS DISTINCT FROM ARRAY['ISSUED', rv.status::text] THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: the audit trail of authorization % is not ISSUED, % (got %)',
        p_recovery_of, rv.status, ev USING ERRCODE = 'P0001';
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
  IF n >= provisional_rate_limit(p_environment) THEN                       -- 0034/0044: the database's limit
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

-- ================================================================ ownership and grants (as 0034)
ALTER FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint)
  TO setuhaul_deploy_evidence;
