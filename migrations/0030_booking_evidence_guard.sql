-- 0030 · Database-side booking evidence guard. Forward-only and additive: 0001–0029 untouched (0029 is PR #5's and is
-- independent of this file). No existing trigger, constraint, policy or grant is changed.
--
-- Gap closed: the booking switch's evidence check (setuhaul/api/inbound.check_booking_preconditions) runs once per
-- Lambda container, when the app is created. A container that started while evidence was current kept booking after it
-- expired, until AWS recycled the container. The database already refused new booking commitments while the kill switch
-- is engaged (0026); it now also refuses them unless the CALLING deployment is the newest one recorded for its
-- environment and has current evidence (or an owner-approved exception) for every gate, judged by the database clock at
-- the moment of the commitment.
--
-- 1. booking_evidence_guard   One row: is the guard enforced in this database? Set ON by this migration when the
--                             database already hosts a recorded deployment (deployment_manifests not empty: the pilot);
--                             OFF on a fresh database (local and test databases), where the owner turns it on before a
--                             first activation. ONE-WAY: it can never be turned off again (only a new migration could).
--                             No application role can change it.
-- 2. the guard trigger        ab_booking_evidence_guard on dock_occupancy and appointments: fires after the kill switch
--                             (aa_…; PostgreSQL orders BEFORE triggers by name), on exactly the kill switch's definition
--                             of a NEW booking commitment (a new or re-activated dock claim for a truck, a changed time,
--                             dock or kind, a new appointment request, or a confirmation). Releases, cancellations,
--                             rejections, expiries, status progressions and metadata updates are never touched.
-- 3. the manifest lock        a deployment_manifests insert takes an exclusive advisory lock; every guarded commitment
--                             takes the shared one first, so a new manifest and a commitment never interleave.
--
-- The caller declares its deployment in the transaction (app.deployment_id, app.code_sha256, app.environment: set by
-- setuhaul/api/db.py and setuhaul/aws/data_api.py from the manifest the start-up check accepted). A declaration is not
-- a bypass: it must name the NEWEST manifest of its environment and that manifest's code, and that deployment's own
-- evidence must be current. An old container, a partly updated deployment or a rollback without fresh evidence names a
-- superseded deployment or other code and is refused. An undeclared caller is refused.
SET search_path = setuhaul, public;

-- ================================================================ 1. the enforcement row
CREATE TABLE booking_evidence_guard (
  id          smallint PRIMARY KEY CHECK (id = 1),
  enforced    boolean NOT NULL,
  changed_at  timestamptz NOT NULL DEFAULT now(),
  changed_by  text NOT NULL DEFAULT session_user,
  reason      text NOT NULL CHECK (length(reason) BETWEEN 5 AND 500)
);
INSERT INTO booking_evidence_guard (id, enforced, reason)
SELECT 1, EXISTS (SELECT 1 FROM deployment_manifests),
       CASE WHEN EXISTS (SELECT 1 FROM deployment_manifests)
            THEN 'migration 0030: this database hosts a recorded deployment'
            ELSE 'migration 0030: no recorded deployment yet; the owner enables it before a first activation' END;

CREATE FUNCTION trg_booking_evidence_guard_one_way() RETURNS trigger LANGUAGE plpgsql
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_SINGLE_ROW: booking_evidence_guard is one row, never inserted or removed'
      USING ERRCODE = 'P0001';
  END IF;
  IF OLD.enforced AND NOT NEW.enforced THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_ONE_WAY: the booking evidence guard cannot be turned off' USING ERRCODE = 'P0001';
  END IF;
  NEW.changed_at := now();
  NEW.changed_by := session_user;
  RETURN NEW;
END $$;
CREATE TRIGGER booking_evidence_guard_one_way BEFORE UPDATE ON booking_evidence_guard
  FOR EACH ROW EXECUTE FUNCTION trg_booking_evidence_guard_one_way();
CREATE TRIGGER booking_evidence_guard_single BEFORE INSERT OR DELETE ON booking_evidence_guard
  FOR EACH ROW EXECUTE FUNCTION trg_booking_evidence_guard_one_way();
CREATE TRIGGER booking_evidence_guard_no_truncate BEFORE TRUNCATE ON booking_evidence_guard
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();

-- ================================================================ 2. the rules, as testable functions
-- Is one evidence row current at time t? The 0014 freshness rules, plus: recorded no earlier than the manifest it must
-- authorize (a rollback re-records an older deployment's manifest; that deployment's earlier evidence does not count).
-- Expiry is EXCLUSIVE: at the instant expires_at the row is already expired.
CREATE FUNCTION booking_evidence_current(p_gate text, p_verified_at timestamptz, p_expires_at timestamptz,
                                         p_manifest_recorded_at timestamptz, p_at timestamptz)
RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT p_expires_at > p_at
     AND p_verified_at <= p_at + interval '5 minutes'
     AND p_expires_at <= p_at + evidence_max_validity(p_gate)
     AND p_expires_at <= p_verified_at + evidence_max_validity(p_gate)
     AND p_verified_at >= p_manifest_recorded_at
$$;

-- Is one owner-approved exception current at time t for this environment? Its own scope only: never production, only
-- a pilot environment, a well-formed owner approval, and T33-P only on PostgreSQL 17.9–17.x. The (gate, code) pairs
-- are already limited by deployment_exceptions' CHECK; the fingerprint is the start-up check's (it reads live AWS).
CREATE FUNCTION booking_exception_current(p_environment text, p_gate text, p_code text, p_approval text,
                                          p_recorded_at timestamptz, p_expires_at timestamptz,
                                          p_manifest_recorded_at timestamptz, p_at timestamptz)
RETURNS boolean LANGUAGE sql STABLE SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT p_expires_at > p_at
     AND p_recorded_at >= p_manifest_recorded_at
     AND p_environment !~ 'prod' AND p_environment ~ '^pilot'
     AND coalesce(p_approval, '') ~ '^OWNER-APPROVAL-20[0-9]{6}-[A-Za-z0-9-]{3,40}$'
     AND (p_gate, p_code) IN (('AURORA', 'T33-P'), ('WORKLOAD_IDENTITY', 'NO-LIVE-DRIFT-DETECTION'),
                              ('OUTBOUND_DELIVERY', 'META-DEFERRED-TEST-SINK-ONLY'))
     AND (p_gate <> 'AURORA' OR current_setting('server_version_num')::int BETWEEN 170009 AND 179999)
$$;

-- The verdict for one declared deployment at time t: NULL when authorized, else 'CODE: detail'. Read-only.
CREATE FUNCTION booking_evidence_verdict(p_environment text, p_deployment text, p_code text, p_at timestamptz)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  gates text[] := ARRAY['AWS_QUEUE', 'AURORA', 'RECOVERY_WORKER', 'BOOKING_WORKFLOW', 'WORKLOAD_IDENTITY',
                        'OUTBOUND_DELIVERY'];          -- = setuhaul/api/inbound.REQUIRED_GATES (a test keeps them equal)
  g text; m record; ev record; ex record; problems text[] := '{}'; refs text[] := '{}';
BEGIN
  IF p_environment IS NULL OR p_deployment IS NULL OR p_code IS NULL THEN
    RETURN 'DEPLOYMENT_UNDECLARED: the transaction does not name its environment, deployment and code';
  END IF;
  IF p_environment ~ 'prod' THEN
    RETURN 'PRODUCTION_DISABLED: no booking commitment is authorized for a production environment';
  END IF;
  SELECT deployment_id, artifact_sha256, recorded_at INTO m
    FROM deployment_manifests WHERE environment = p_environment ORDER BY manifest_id DESC LIMIT 1;
  IF NOT FOUND THEN
    RETURN format('NO_MANIFEST: no deployment is recorded for %s', p_environment);
  END IF;
  IF m.deployment_id <> p_deployment THEN
    RETURN format('DEPLOYMENT_SUPERSEDED: the caller runs %s, the newest recorded deployment is %s',
                  left(p_deployment, 14), left(m.deployment_id, 14));
  END IF;
  IF m.artifact_sha256 <> p_code THEN
    RETURN format('ARTIFACT_MISMATCH: the caller runs code %s, the deployment is for %s (partial deployment?)',
                  left(p_code, 12), left(m.artifact_sha256, 12));
  END IF;
  FOREACH g IN ARRAY gates LOOP
    SELECT verified_at, expires_at INTO ev FROM deployment_evidence
     WHERE environment = p_environment AND deployment_id = p_deployment AND gate = g
     ORDER BY evidence_id DESC LIMIT 1;
    IF FOUND AND booking_evidence_current(g, ev.verified_at, ev.expires_at, m.recorded_at, p_at) THEN
      CONTINUE;
    END IF;
    SELECT exception_code, approval_ref, recorded_at, expires_at INTO ex FROM deployment_exceptions
     WHERE environment = p_environment AND deployment_id = p_deployment AND gate = g
     ORDER BY exception_id DESC LIMIT 1;
    IF FOUND AND booking_exception_current(p_environment, g, ex.exception_code, ex.approval_ref, ex.recorded_at,
                                           ex.expires_at, m.recorded_at, p_at) THEN
      refs := array_append(refs, ex.approval_ref);
      CONTINUE;
    END IF;
    problems := array_append(problems, g || CASE
      WHEN ev.expires_at IS NOT NULL AND ev.expires_at <= p_at THEN ' evidence expired'
      WHEN ev.verified_at IS NOT NULL AND ev.verified_at < m.recorded_at THEN ' evidence predates this deployment record'
      WHEN ev.verified_at IS NOT NULL THEN ' evidence outside its validity rules'
      WHEN ex.exception_code IS NOT NULL THEN ' exception not valid here (expired, out of scope or predates the deployment)'
      ELSE ' no evidence' END);
  END LOOP;
  IF cardinality(problems) > 0 THEN
    RETURN 'EVIDENCE_NOT_CURRENT: ' || array_to_string(problems, '; ');
  END IF;
  IF (SELECT count(DISTINCT r) FROM unnest(refs) r) > 1 THEN
    RETURN 'APPROVAL_INCONSISTENT: the exceptions for this deployment name different owner approvals';
  END IF;
  RETURN NULL;
END $$;

-- ================================================================ 3. the guard
CREATE FUNCTION trg_require_booking_evidence() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  commit_new boolean; verdict text; state text;
  env text := nullif(current_setting('app.environment', true), '');
  dep text := nullif(current_setting('app.deployment_id', true), '');
  code text := nullif(current_setting('app.code_sha256', true), '');
  corr text := coalesce(nullif(current_setting('app.correlation_id', true), ''), '-');
BEGIN
  -- exactly the kill switch's definition of a new booking commitment (0026, trg_refuse_booking_when_killed)
  IF TG_TABLE_NAME = 'dock_occupancy' THEN
    commit_new := NEW.active AND NEW.kind IN ('HOLD', 'APPOINTMENT') AND (TG_OP = 'INSERT'
                  OR NOT OLD.active OR NEW.during IS DISTINCT FROM OLD.during OR NEW.kind IS DISTINCT FROM OLD.kind
                  OR NEW.dock_id IS DISTINCT FROM OLD.dock_id);
  ELSE
    commit_new := (TG_OP = 'INSERT' AND NEW.status IN ('PENDING_CONFIRMATION', 'CONFIRMED'))
               OR (TG_OP = 'UPDATE' AND NEW.status = 'CONFIRMED' AND OLD.status IS DISTINCT FROM 'CONFIRMED')
               OR (TG_OP = 'UPDATE' AND NEW.status IN ('PENDING_CONFIRMATION', 'CONFIRMED')
                   AND (NEW.during IS DISTINCT FROM OLD.during OR NEW.dock_id IS DISTINCT FROM OLD.dock_id));
  END IF;
  IF NOT commit_new THEN
    RETURN NEW;
  END IF;
  -- a missing row means enforced (fail closed); the row cannot be removed
  IF NOT coalesce((SELECT enforced FROM booking_evidence_guard WHERE id = 1), true) THEN
    RETURN NEW;
  END IF;
  BEGIN
    -- serialize with manifest inserts: a new manifest waits for in-flight commitments, and a commitment that starts
    -- while a manifest is being recorded waits for it and then reads it (READ COMMITTED: a fresh snapshot per statement)
    PERFORM pg_advisory_xact_lock_shared(hashtextextended('setuhaul.booking_evidence_guard', 0));
    IF current_setting('transaction_isolation') <> 'read committed' THEN
      verdict := 'ISOLATION: a booking commitment must run at READ COMMITTED so the newest deployment record is seen';
    ELSE
      verdict := booking_evidence_verdict(env, dep, code, clock_timestamp());
    END IF;
  EXCEPTION
    WHEN insufficient_privilege THEN
      verdict := 'ACCESS_DENIED: the evidence could not be read (' || SQLSTATE || ')';
    WHEN OTHERS THEN
      GET STACKED DIAGNOSTICS state = RETURNED_SQLSTATE;
      verdict := 'VERIFICATION_FAILED: the evidence could not be verified (' || state || ')';
  END;
  IF verdict IS NOT NULL THEN
    RAISE EXCEPTION 'BOOKING_EVIDENCE_%', verdict
      USING ERRCODE = 'P0001',
            HINT = format('table=%s environment=%s deployment=%s correlation=%s', TG_TABLE_NAME, coalesce(env, '-'),
                          left(coalesce(dep, '-'), 14), corr);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ab_booking_evidence_guard BEFORE INSERT OR UPDATE ON dock_occupancy
  FOR EACH ROW EXECUTE FUNCTION trg_require_booking_evidence();
CREATE TRIGGER ab_booking_evidence_guard BEFORE INSERT OR UPDATE ON appointments
  FOR EACH ROW EXECUTE FUNCTION trg_require_booking_evidence();

-- ================================================================ 4. the manifest lock
CREATE FUNCTION trg_manifest_waits_for_commitments() RETURNS trigger LANGUAGE plpgsql
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('setuhaul.booking_evidence_guard', 0));
  RETURN NEW;
END $$;
CREATE TRIGGER deployment_manifests_lock BEFORE INSERT ON deployment_manifests
  FOR EACH ROW EXECUTE FUNCTION trg_manifest_waits_for_commitments();

-- ================================================================ ownership and grants
ALTER FUNCTION booking_evidence_verdict(text, text, text, timestamptz) OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_require_booking_evidence() OWNER TO setuhaul_rpc;
GRANT SELECT ON booking_evidence_guard TO setuhaul_rpc;
GRANT EXECUTE ON FUNCTION booking_evidence_current(text, timestamptz, timestamptz, timestamptz, timestamptz),
                          booking_exception_current(text, text, text, text, timestamptz, timestamptz, timestamptz,
                                                    timestamptz)
  TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION booking_evidence_current(text, timestamptz, timestamptz, timestamptz, timestamptz),
                           booking_exception_current(text, text, text, text, timestamptz, timestamptz, timestamptz,
                                                     timestamptz),
                           booking_evidence_verdict(text, text, text, timestamptz),
                           trg_require_booking_evidence(), trg_manifest_waits_for_commitments(),
                           trg_booking_evidence_guard_one_way()
  FROM PUBLIC;
-- No application role gets anything: the guard row is read only by the trigger (as setuhaul_rpc) and changed only by
-- the owner (one-way). No table, policy or role grant beyond SELECT on the guard row for setuhaul_rpc.
