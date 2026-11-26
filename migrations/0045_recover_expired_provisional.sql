-- 0045 · Bounded recovery of an EXPIRED provisional authorization (OWNER DECISION, 26 Nov 2026).
--
-- Owner decision ("Bounded recovery of expired provisional authorization", 26 Nov 2026): authorization 68 (release
-- 24c388c, OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-PATCH-01) expired without a renewal; it may be recovered ONCE.
-- Forward-only and additive. 0033's recovery exception (Recovery Release A) covered only a REVOKED window. This
-- migration extends it to an EXPIRED window under every 0033 condition plus one more:
--   * the expired authorization is named explicitly (p_recovery_of) and is the previous release's;
--   * its status is EXPIRED (the issuer's own expiry sweep, earlier in the same transaction, makes a lapsed ISSUED
--     window EXPIRED, exactly as for any other issuance) and its audit trail is exactly ISSUED, EXPIRED;
--   * NEW: the owner's approval to recover exactly that authorization is recorded in the append-only table
--     provisional_expired_recovery_approvals. This migration records one row, for authorization 68, and only where
--     authorization 68 exists here with release 24c388c and PATCH-01 (on any other database nothing is written);
--   * within seven days of its expiry (expires_at; a REVOKED window still counts from closed_at);
--   * once (0033's UNIQUE index on recovery_of); never from a recovery (no second hop);
--   * the previous release lacks only BOOKING_WORKFLOW. Every other rule of the issuer is unchanged: evidence
--     guard and identity enforcement, pilot only (never production), newest manifest, the five other gates current,
--     the exceptions naming this approval, one open window, the rolling limit (provisional_rate_limit), one
--     authorization per deployment, single-use approval references.
-- No existing row is changed: provisional_authorizations and its events stay append-only (0032/0033 triggers).
-- issue_provisional_authorization (7 arguments) is replaced with the same signature, owner and grants; the body is
-- 0034's except the recovery block. The 6-argument wrapper (0033) needs no change.
SET search_path = setuhaul, public;

CREATE TABLE provisional_expired_recovery_approvals (
  authorization_id  bigint PRIMARY KEY REFERENCES provisional_authorizations (authorization_id),
  environment       text NOT NULL,
  deployment_id     text NOT NULL,
  basis             text NOT NULL CHECK (length(basis) BETWEEN 10 AND 600),
  evidence_ref      text NOT NULL CHECK (length(evidence_ref) BETWEEN 5 AND 300),
  recorded_by       text NOT NULL DEFAULT session_user,
  recorded_at       timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER provisional_expired_recovery_approvals_append_only
  BEFORE UPDATE OR DELETE OR TRUNCATE ON provisional_expired_recovery_approvals
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();
REVOKE ALL ON provisional_expired_recovery_approvals FROM PUBLIC;
GRANT SELECT ON provisional_expired_recovery_approvals TO setuhaul_rpc, setuhaul_deploy_evidence;

-- BEGIN owner-approved-expired-recovery (tests/test_recover_expired_0045.py runs this block on its own)
INSERT INTO provisional_expired_recovery_approvals (authorization_id, environment, deployment_id, basis, evidence_ref)
SELECT a.authorization_id, a.environment, a.deployment_id,
       'Owner decision 26 Nov 2026, "Bounded recovery of expired provisional authorization": authorization 68 '
       || '(release 24c388c, PATCH-01) expired without renewal (smoke check 7 defect) and may be recovered once.',
       'migrations/0045_recover_expired_provisional.sql; docs/release/evidence/owner_test_origin_20261126.json'
  FROM provisional_authorizations a
 WHERE a.authorization_id = 68
   AND a.release_sha = '24c388c1d73a2f07654f2f0a31307e99aef01f80'
   AND a.approval_ref = 'OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-PATCH-01'
   AND a.expires_at <= clock_timestamp()
   AND NOT EXISTS (SELECT 1 FROM provisional_expired_recovery_approvals x WHERE x.authorization_id = 68);
-- END owner-approved-expired-recovery

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
    IF rv.recovery_of IS NOT NULL THEN
      RAISE EXCEPTION 'RECOVERY_REFUSED: authorization % was itself a recovery; no second hop (full activation)',
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
