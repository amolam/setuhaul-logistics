-- 0033 · Recovery Release A (owner-requested 24 Nov 2026). Forward-only and additive: 0001–0032 are untouched; three
-- functions are replaced with the same signatures (0029's approval_execution_evidence, 0032's transition and audit
-- triggers) and one is turned into a wrapper (0032's 6-argument issue_provisional_authorization). No booking
-- commitment, evidence row, approval row or audit row is changed. Design: docs/booking-renewal/RECOVERY_RELEASE_A_DESIGN.md.
--
--   D2  approval_execution_evidence classifies a person who decided through the console (staff:<Cognito sub UUID>) as
--       console_user and only named identities (staff:<name>: drills, smokes, probes) as drill_staff. Owner-attested
--       corrections live in a new append-only table with their provenance (they confirm the rule and can never change
--       a classification: a disagreeing one reads as attestation_conflict); 0033 records the one for the owner's 23 Nov
--       console decision (approval 29ffac03-…), only where that approval exists as a staff:<UUID> human decision.
--   D4  booking_authorization_status: the database's verdict for one deployment, its code and its release identity,
--       now, with the kill switch and any open provisional window. /health reads it on every call.
--   P   the recovery exception: a release whose provisional authorization was REVOKED may precede ONE more provisional
--       authorization, named explicitly, within 7 days, once, never from a recovery (no second hop). The default rule
--       (the previous release must have complete normal evidence) is unchanged.
SET search_path = setuhaul, public;

-- ================================================================ D2 · decider provenance
CREATE TABLE approval_decider_attestations (
  approval_id     uuid PRIMARY KEY,
  decider_kind    text NOT NULL CHECK (decider_kind IN ('console_user', 'drill_staff')),
  previous_kind   text CHECK (previous_kind IN ('console_user', 'drill_staff')),
  basis           text NOT NULL CHECK (length(basis) BETWEEN 10 AND 500),
  evidence_ref    text NOT NULL CHECK (length(evidence_ref) BETWEEN 5 AND 300),
  recorded_by     text NOT NULL DEFAULT session_user,
  recorded_at     timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER approval_decider_attestations_append_only
  BEFORE UPDATE OR DELETE OR TRUNCATE ON approval_decider_attestations
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();
REVOKE ALL ON approval_decider_attestations FROM PUBLIC;
GRANT SELECT ON approval_decider_attestations TO setuhaul_rpc;

-- The person who decided, reduced to a kind. A console decision records the verified Cognito subject
-- (service.py: f"staff:{p.sub}", a UUID); drills, smokes and probes use named identities (staff:step6-drill-planner,
-- staff:option-d-smoke-planner, ...; tests/test_recovery_release_a.py keeps every script's identity non-UUID).
CREATE FUNCTION decider_kind_of(p_decided_by text, p_decided_by_type text) RETURNS text LANGUAGE sql IMMUTABLE
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT CASE WHEN p_decided_by IS NULL THEN NULL
              WHEN p_decided_by_type = 'SYSTEM' THEN 'system'
              WHEN p_decided_by ~ '^staff:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN 'console_user'
              WHEN p_decided_by LIKE 'staff:%' THEN 'drill_staff'
              ELSE 'console_user' END
$$;

-- 0029's function, same signature and grants; only decider_kind changes. The kind ALWAYS comes from the rule above, on
-- the immutable decided_by: an attestation records provenance and can only agree with it. An attestation that
-- disagrees yields 'attestation_conflict', which matches no record, so no attestation can relabel a decision.
CREATE OR REPLACE FUNCTION approval_execution_evidence(p_approval_ids uuid[])
RETURNS TABLE (approval_id uuid, kind text, facility_id text, shipment_id text, shipment_is_synthetic boolean,
               appointment_id text, appointment_status text, status text, decided_by_type text, decided_by_role text,
               decider_kind text, outcome_reason text, created_at timestamptz, decided_at timestamptz,
               expires_at timestamptz, task_token_recorded boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF coalesce(current_setting('app.facility_scope', true), '') = '' THEN
    RAISE EXCEPTION 'approval_execution_evidence: the caller has no facility scope' USING ERRCODE = '42501';
  END IF;
  IF p_approval_ids IS NULL OR cardinality(p_approval_ids) = 0 OR cardinality(p_approval_ids) > 200 THEN
    RAISE EXCEPTION 'approval_execution_evidence: between 1 and 200 approval ids' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
    SELECT r.approval_id, r.kind, r.facility_id, r.shipment_id, coalesce(r.shipment_id LIKE 'SYN-%', false),
           r.appointment_id, a.status, r.status, r.decided_by_type, r.decided_by_role,
           CASE WHEN att.decider_kind IS NOT NULL
                     AND att.decider_kind IS DISTINCT FROM decider_kind_of(r.decided_by, r.decided_by_type)
                THEN 'attestation_conflict'
                ELSE decider_kind_of(r.decided_by, r.decided_by_type) END,
           r.outcome_reason, r.created_at, r.decided_at, r.expires_at, r.task_token IS NOT NULL
      FROM approval_requests r
      LEFT JOIN appointments a ON a.appointment_id = r.appointment_id
      LEFT JOIN approval_decider_attestations att ON att.approval_id = r.approval_id
     WHERE r.approval_id = ANY (p_approval_ids)
     ORDER BY r.created_at;
END $$;

-- BEGIN historical-attestation (tests/test_recovery_release_a.py runs this block on its own)
-- The owner's 23 Nov 2026 console decision. Recorded only if that approval exists here as a person's console decision
-- (staff:<UUID>); on any other database (CI, tests) nothing is written. approval_requests itself is never modified.
DO $$
DECLARE r record;
BEGIN
  PERFORM set_config('app.facility_scope', (SELECT coalesce(string_agg(facility_id, ','), '') FROM facilities), true);
  SELECT approval_id, decided_by, decided_by_type INTO r FROM approval_requests
   WHERE approval_id = '29ffac03-d8bd-4dac-b35c-66b0f8ebf24b';
  IF FOUND AND r.decided_by_type = 'HUMAN'
     AND r.decided_by ~ '^staff:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     AND NOT EXISTS (SELECT 1 FROM approval_decider_attestations WHERE approval_id = r.approval_id) THEN
    INSERT INTO approval_decider_attestations (approval_id, decider_kind, previous_kind, basis, evidence_ref)
    VALUES (r.approval_id, 'console_user', 'drill_staff',
            'Owner-confirmed console decision (Category C, 23 Nov 2026). 0029 classified every staff: decider as a '
            || 'drill; the console records a signed-in person as staff:<Cognito sub>. Corrected by migration 0033.',
            'docs/release/evidence/owner_attribution_20261123.json; API log request 882b6fca-28c1-4ae3-b960-e38f78613aef');
  END IF;
  PERFORM set_config('app.facility_scope', '', true);
END $$;
-- END historical-attestation

-- ================================================================ P · recovery of a revoked provisional release
ALTER TABLE provisional_authorizations
  ADD COLUMN recovery_of bigint REFERENCES provisional_authorizations (authorization_id),
  ADD CONSTRAINT provisional_recovery_not_self CHECK (recovery_of IS NULL OR recovery_of <> authorization_id);
CREATE UNIQUE INDEX ux_provisional_recovery_once ON provisional_authorizations (recovery_of) WHERE recovery_of IS NOT NULL;

-- 0032's transition rule, now also keeping recovery_of immutable.
CREATE OR REPLACE FUNCTION trg_provisional_transition() RETURNS trigger LANGUAGE plpgsql
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'APPEND_ONLY: provisional authorizations are never removed' USING ERRCODE = 'P0001';
  END IF;
  IF OLD.status <> 'ISSUED' THEN
    RAISE EXCEPTION 'PROVISIONAL_FINAL: authorization % is already %', OLD.authorization_id, OLD.status
      USING ERRCODE = 'P0001';
  END IF;
  IF (NEW.authorization_id, NEW.environment, NEW.deployment_id, NEW.code_sha256, NEW.predecessor_deployment,
      NEW.release_sha, NEW.approval_ref, NEW.pipeline_run, NEW.issued_by, NEW.issued_at, NEW.expires_at,
      NEW.recovery_of)
     IS DISTINCT FROM
     (OLD.authorization_id, OLD.environment, OLD.deployment_id, OLD.code_sha256, OLD.predecessor_deployment,
      OLD.release_sha, OLD.approval_ref, OLD.pipeline_run, OLD.issued_by, OLD.issued_at, OLD.expires_at,
      OLD.recovery_of) THEN
    RAISE EXCEPTION 'PROVISIONAL_IMMUTABLE: only the status of an authorization changes (never its window)'
      USING ERRCODE = 'P0001';
  END IF;
  IF NEW.status NOT IN ('CONSUMED', 'REVOKED', 'EXPIRED') THEN
    RAISE EXCEPTION 'PROVISIONAL_TRANSITION: % -> % is not allowed', OLD.status, NEW.status USING ERRCODE = 'P0001';
  END IF;
  IF NEW.status = 'CONSUMED' AND clock_timestamp() >= OLD.expires_at THEN
    RAISE EXCEPTION 'PROVISIONAL_EXPIRED: authorization % expired at %', OLD.authorization_id, OLD.expires_at
      USING ERRCODE = 'P0001';
  END IF;
  IF NEW.status = 'EXPIRED' AND clock_timestamp() < OLD.expires_at THEN
    RAISE EXCEPTION 'PROVISIONAL_NOT_EXPIRED: authorization % is open until %', OLD.authorization_id, OLD.expires_at
      USING ERRCODE = 'P0001';
  END IF;
  NEW.closed_at := clock_timestamp();
  NEW.closed_by := coalesce(NEW.closed_by, session_user);
  RETURN NEW;
END $$;

-- 0032's audit, now recording recovery_of.
CREATE OR REPLACE FUNCTION trg_provisional_audit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO provisional_authorization_events (authorization_id, environment, deployment_id, action, principal, detail)
  VALUES (NEW.authorization_id, NEW.environment, NEW.deployment_id, NEW.status,
          CASE WHEN TG_OP = 'INSERT' THEN NEW.issued_by ELSE NEW.closed_by END,
          jsonb_build_object('release_sha', NEW.release_sha, 'approval_ref', NEW.approval_ref,
                             'pipeline_run', NEW.pipeline_run, 'issued_at', NEW.issued_at,
                             'expires_at', NEW.expires_at, 'reason', NEW.close_reason,
                             'evidence_id', NEW.consumed_evidence_id, 'recovery_of', NEW.recovery_of,
                             'correlation', nullif(current_setting('app.correlation_id', true), '')));
  RETURN NULL;
END $$;

-- The 7-argument issuer. p_recovery_of NULL = 0032's rules exactly. Otherwise the recovery exception (design §3.1).
CREATE FUNCTION issue_provisional_authorization(p_environment text, p_deployment text, p_release_sha text,
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
  IF n >= 2 THEN
    RAISE EXCEPTION 'PROVISIONAL_RATE_LIMIT: % authorizations issued for % in the last 24 hours (maximum 2)', n,
      p_environment USING ERRCODE = 'P0001';
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

-- 0032's 6-argument entry point keeps its signature, owner and grant, and means "no recovery".
CREATE OR REPLACE FUNCTION issue_provisional_authorization(p_environment text, p_deployment text, p_release_sha text,
                                                           p_approval_ref text, p_pipeline_run text,
                                                           p_minutes integer DEFAULT 60)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT issue_provisional_authorization(p_environment, p_deployment, p_release_sha, p_approval_ref, p_pipeline_run,
                                         p_minutes, NULL::bigint)
$$;

-- ================================================================ D4 · the authoritative booking state for /health
-- The database's answer for one declared deployment and its code, now: the release identity's verdict (0032), the
-- kill switch and any open provisional window. authorized is true only when the verdict is NULL and the switch is off.
CREATE FUNCTION booking_authorization_status(p_environment text, p_deployment text, p_code text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE v text; killed boolean; pa record; logins text[];
BEGIN
  logins := booking_identity_logins(coalesce(p_code, ''));
  v := booking_evidence_verdict(p_environment, p_deployment, p_code, clock_timestamp(), logins[2]);
  killed := coalesce((SELECT engaged FROM booking_kill_switch WHERE id = 1), true);      -- no row: fail closed
  SELECT status, issued_at, expires_at INTO pa FROM provisional_authorizations
   WHERE environment = p_environment AND deployment_id = p_deployment ORDER BY authorization_id DESC LIMIT 1;
  RETURN jsonb_build_object(
    'authorized', v IS NULL AND NOT killed,
    'verdict', v,
    'kill_switch', killed,
    'provisional_open', coalesce(pa.status = 'ISSUED' AND clock_timestamp() >= pa.issued_at
                                 AND clock_timestamp() < pa.expires_at, false),
    'provisional_until', CASE WHEN pa.status = 'ISSUED' AND clock_timestamp() < pa.expires_at THEN pa.expires_at END,
    'checked_at', clock_timestamp());
END $$;

-- ================================================================ ownership and grants
ALTER FUNCTION decider_kind_of(text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION approval_execution_evidence(uuid[]) OWNER TO setuhaul_rpc;
ALTER FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint) OWNER TO setuhaul_rpc;
ALTER FUNCTION issue_provisional_authorization(text, text, text, text, text, integer) OWNER TO setuhaul_rpc;
ALTER FUNCTION booking_authorization_status(text, text, text) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION decider_kind_of(text, text),
                           issue_provisional_authorization(text, text, text, text, text, integer, bigint),
                           booking_authorization_status(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION decider_kind_of(text, text) TO setuhaul_rpc;
GRANT EXECUTE ON FUNCTION issue_provisional_authorization(text, text, text, text, text, integer, bigint)
  TO setuhaul_deploy_evidence;
GRANT EXECUTE ON FUNCTION booking_authorization_status(text, text, text) TO setuhaul_ingest;
