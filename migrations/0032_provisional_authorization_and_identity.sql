-- 0032 · Option P (single-use provisional booking authorization) and M2 (deployment identity), owner-approved
-- 24 Nov 2026. Forward-only and additive: 0001–0031 untouched except three CREATE OR REPLACE of 0030 functions whose
-- behaviour is only narrowed (the one-way trigger also guards the new column; the 4-argument verdict now delegates to
-- the 5-argument one, which adds checks and accepts nothing 0030 refused except an open provisional authorization for
-- BOOKING_WORKFLOW alone). Design: docs/booking-renewal/OPTION_P_PROVISIONAL_AUTHORIZATION_DESIGN.md,
-- docs/booking-renewal/M2_DEPLOYMENT_ATTESTATION_DESIGN.md.
--
-- M2 · deployment identity. Every booking commitment comes from exactly two database roles, setuhaul_agent (agent
-- function) and setuhaul_console (api function); their login secrets are readable only by those functions. From 0032
-- the pipeline mints a login per release for each (setuhaul_login_agent_<12 hex>, setuhaul_login_console_<12 hex>,
-- the first 12 hex of the deployed artifact's SHA-256), the stack gives the two functions an IAM role per release (the
-- previous role and its policy are deleted by CloudFormation), and provision_logins sets every other such login
-- NOLOGIN. The database then checks the AUTHENTICATED login (session_user: set by authentication, unchanged by SET
-- ROLE or SECURITY DEFINER) against the newest manifest's code. A process that declares the newest deployment but
-- authenticates as anything else is refused. Enforced per database by booking_evidence_guard.identity_enforced: ON
-- here wherever the guard is ON (the pilot), one-way like the guard.
--
-- Option P · provisional authorization. A new release can't produce its own BOOKING_WORKFLOW evidence without
-- booking (the release-bound smoke drives the deployed agent). The pipeline may issue ONE authorization per release:
-- pilot only, never production; at most 2 hours (60 minutes by default); one open per environment and at most two per
-- environment in any 24 hours; one per deployment and per owner approval; only if the guard and identity enforcement
-- are on, the release is the newest manifest, its other five gates are current, and the previous release had complete
-- normal evidence (no chaining). While it is open, and only then, it stands in for BOOKING_WORKFLOW alone. Recording
-- the release's BOOKING_WORKFLOW evidence consumes it in the same transaction (atomic); after expiry or revocation
-- that evidence is refused (no renewal, no extension: a new release and a new approval are needed). Expiry is the
-- database clock on every commitment; no job is needed. Every transition is audited, append-only.
SET search_path = setuhaul, public;

-- ================================================================ 1. M2 switch (one-way, on the guard row)
ALTER TABLE booking_evidence_guard ADD COLUMN identity_enforced boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION trg_booking_evidence_guard_one_way() RETURNS trigger LANGUAGE plpgsql
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_SINGLE_ROW: booking_evidence_guard is one row, never inserted or removed'
      USING ERRCODE = 'P0001';
  END IF;
  IF OLD.enforced AND NOT NEW.enforced THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_ONE_WAY: the booking evidence guard cannot be turned off' USING ERRCODE = 'P0001';
  END IF;
  IF OLD.identity_enforced AND NOT NEW.identity_enforced THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_ONE_WAY: deployment identity enforcement cannot be turned off'
      USING ERRCODE = 'P0001';
  END IF;
  IF NEW.identity_enforced AND NOT NEW.enforced THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_ORDER: deployment identity is enforced only together with the evidence guard'
      USING ERRCODE = 'P0001';
  END IF;
  NEW.changed_at := now();
  NEW.changed_by := session_user;
  RETURN NEW;
END $$;

UPDATE booking_evidence_guard
   SET identity_enforced = true,
       reason = 'migration 0032: deployment identity enforced with the guard (this database hosts a deployment)'
 WHERE id = 1 AND enforced AND NOT identity_enforced;

-- The two logins that may make booking commitments for a deployment's code (first 12 hex of its artifact SHA-256).
CREATE FUNCTION booking_identity_logins(p_code text) RETURNS text[] LANGUAGE sql IMMUTABLE
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT CASE WHEN p_code ~ '^[0-9a-f]{64}$'
              THEN ARRAY['setuhaul_login_agent_' || left(p_code, 12), 'setuhaul_login_console_' || left(p_code, 12)]
              ELSE ARRAY[]::text[] END
$$;

-- ================================================================ 2. the authorization and its audit trail
CREATE TABLE provisional_authorizations (
  authorization_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  environment             text NOT NULL CHECK (booking_environment_class(environment) = 'pilot'),
  deployment_id           text NOT NULL CHECK (deployment_id ~ '^m-[0-9a-f]{40}$'),
  code_sha256             text NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
  predecessor_deployment  text NOT NULL CHECK (predecessor_deployment ~ '^m-[0-9a-f]{40}$'),
  release_sha             text NOT NULL CHECK (release_sha ~ '^[0-9a-f]{40}$'),
  approval_ref            text NOT NULL UNIQUE CHECK (approval_ref ~ '^OWNER-APPROVAL-20[0-9]{6}-[A-Za-z0-9-]{3,40}$'),
  pipeline_run            text NOT NULL CHECK (length(pipeline_run) BETWEEN 1 AND 100),
  issued_by               text NOT NULL,
  issued_at               timestamptz NOT NULL,
  expires_at              timestamptz NOT NULL,
  status                  text NOT NULL DEFAULT 'ISSUED' CHECK (status IN ('ISSUED', 'CONSUMED', 'REVOKED', 'EXPIRED')),
  closed_at               timestamptz,
  closed_by               text,
  close_reason            text CHECK (close_reason IS NULL OR length(close_reason) BETWEEN 5 AND 300),
  consumed_evidence_id    bigint,
  UNIQUE (environment, deployment_id),                              -- one per deployment
  CHECK (predecessor_deployment <> deployment_id),
  CHECK (expires_at > issued_at AND expires_at <= issued_at + interval '2 hours'),   -- hard maximum
  CHECK ((status = 'ISSUED') = (closed_at IS NULL)),
  CHECK ((status = 'CONSUMED') = (consumed_evidence_id IS NOT NULL))
);

CREATE TABLE provisional_authorization_events (
  event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  authorization_id  bigint NOT NULL REFERENCES provisional_authorizations,
  environment       text NOT NULL,
  deployment_id     text NOT NULL,
  action            text NOT NULL CHECK (action IN ('ISSUED', 'CONSUMED', 'REVOKED', 'EXPIRED')),
  principal         text NOT NULL,
  detail            jsonb NOT NULL,
  recorded_at       timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER provisional_authorization_events_append_only
  BEFORE UPDATE OR DELETE OR TRUNCATE ON provisional_authorization_events
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();

-- Only ISSUED -> CONSUMED | REVOKED | EXPIRED, once; nothing else ever changes; never removed (the owner included).
CREATE FUNCTION trg_provisional_transition() RETURNS trigger LANGUAGE plpgsql
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
      NEW.release_sha, NEW.approval_ref, NEW.pipeline_run, NEW.issued_by, NEW.issued_at, NEW.expires_at)
     IS DISTINCT FROM
     (OLD.authorization_id, OLD.environment, OLD.deployment_id, OLD.code_sha256, OLD.predecessor_deployment,
      OLD.release_sha, OLD.approval_ref, OLD.pipeline_run, OLD.issued_by, OLD.issued_at, OLD.expires_at) THEN
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
CREATE TRIGGER provisional_authorizations_transition BEFORE UPDATE OR DELETE ON provisional_authorizations
  FOR EACH ROW EXECUTE FUNCTION trg_provisional_transition();
CREATE TRIGGER provisional_authorizations_no_truncate BEFORE TRUNCATE ON provisional_authorizations
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();

-- Every insert and transition is audited in the same transaction, whatever path made it.
CREATE FUNCTION trg_provisional_audit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO provisional_authorization_events (authorization_id, environment, deployment_id, action, principal, detail)
  VALUES (NEW.authorization_id, NEW.environment, NEW.deployment_id, NEW.status,
          CASE WHEN TG_OP = 'INSERT' THEN NEW.issued_by ELSE NEW.closed_by END,
          jsonb_build_object('release_sha', NEW.release_sha, 'approval_ref', NEW.approval_ref,
                             'pipeline_run', NEW.pipeline_run, 'issued_at', NEW.issued_at,
                             'expires_at', NEW.expires_at, 'reason', NEW.close_reason,
                             'evidence_id', NEW.consumed_evidence_id,
                             'correlation', nullif(current_setting('app.correlation_id', true), '')));
  RETURN NULL;
END $$;
CREATE TRIGGER provisional_authorizations_audit AFTER INSERT OR UPDATE ON provisional_authorizations
  FOR EACH ROW EXECUTE FUNCTION trg_provisional_audit();

-- ================================================================ 3. the rules
-- Normal gate coverage (evidence or an owner-approved exception) of one deployment at time t, judged against the
-- manifest row it must postdate; p_skip leaves one gate out. Same rules as 0030's verdict loop.
CREATE FUNCTION booking_gate_problems(p_environment text, p_deployment text, p_manifest_recorded_at timestamptz,
                                      p_at timestamptz, p_skip text, OUT problems text[], OUT refs text[])
LANGUAGE plpgsql STABLE SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  gates text[] := ARRAY['AWS_QUEUE', 'AURORA', 'RECOVERY_WORKER', 'BOOKING_WORKFLOW', 'WORKLOAD_IDENTITY',
                        'OUTBOUND_DELIVERY'];          -- = setuhaul/api/inbound.REQUIRED_GATES (a test keeps them equal)
  g text; ev record; ex record;
BEGIN
  problems := '{}'; refs := '{}';
  FOREACH g IN ARRAY gates LOOP
    CONTINUE WHEN g = p_skip;
    SELECT verified_at, expires_at INTO ev FROM deployment_evidence
     WHERE environment = p_environment AND deployment_id = p_deployment AND gate = g
     ORDER BY evidence_id DESC LIMIT 1;
    IF FOUND AND booking_evidence_current(g, ev.verified_at, ev.expires_at, p_manifest_recorded_at, p_at) THEN
      CONTINUE;
    END IF;
    SELECT exception_code, approval_ref, recorded_at, expires_at INTO ex FROM deployment_exceptions
     WHERE environment = p_environment AND deployment_id = p_deployment AND gate = g
     ORDER BY exception_id DESC LIMIT 1;
    IF FOUND AND booking_exception_current(p_environment, g, ex.exception_code, ex.approval_ref, ex.recorded_at,
                                           ex.expires_at, p_manifest_recorded_at, p_at) THEN
      refs := array_append(refs, ex.approval_ref);
      CONTINUE;
    END IF;
    problems := array_append(problems, g || CASE
      WHEN ev.expires_at IS NOT NULL AND ev.expires_at <= p_at THEN ' evidence expired'
      WHEN ev.verified_at IS NOT NULL AND ev.verified_at < p_manifest_recorded_at THEN ' evidence predates this deployment record'
      WHEN ev.verified_at IS NOT NULL THEN ' evidence outside its validity rules'
      WHEN ex.exception_code IS NOT NULL THEN ' exception not valid here (expired, out of scope or predates the deployment)'
      ELSE ' no evidence' END);
  END LOOP;
END $$;

-- The verdict for one declared deployment, at time t, for the authenticated login p_login: NULL when authorized.
-- 0030's checks in 0030's order, then M2 (identity), then the gates, where an OPEN provisional authorization may stand
-- in for BOOKING_WORKFLOW only.
CREATE FUNCTION booking_evidence_verdict(p_environment text, p_deployment text, p_code text, p_at timestamptz,
                                         p_login text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  m record; gp record; pa record; guard record; problems text[]; refs text[];
BEGIN
  IF p_environment IS NULL OR p_deployment IS NULL OR p_code IS NULL THEN
    RETURN 'DEPLOYMENT_UNDECLARED: the transaction does not name its environment, deployment and code';
  END IF;
  IF booking_environment_class(p_environment) = 'production' THEN
    RETURN 'PRODUCTION_DISABLED: no booking commitment is authorized for a production environment';
  END IF;
  IF booking_environment_class(p_environment) = 'invalid' THEN
    RETURN 'ENVIRONMENT_INVALID: the declared environment is not a canonical environment name';
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
  -- a missing guard row reads as enforced (fail closed), as in 0030
  SELECT coalesce(bool_or(identity_enforced), true) AS identity INTO guard FROM booking_evidence_guard WHERE id = 1;
  IF guard.identity AND NOT (coalesce(p_login, '') = ANY (booking_identity_logins(m.artifact_sha256))) THEN
    RETURN format('IDENTITY_MISMATCH: login %s is not the identity of deployment %s (expected %s)',
                  coalesce(p_login, '-'), left(m.deployment_id, 14),
                  array_to_string(booking_identity_logins(m.artifact_sha256), ' or '));
  END IF;
  SELECT * INTO gp FROM booking_gate_problems(p_environment, p_deployment, m.recorded_at, p_at, NULL);
  problems := gp.problems; refs := gp.refs;
  IF EXISTS (SELECT 1 FROM unnest(problems) x WHERE x LIKE 'BOOKING_WORKFLOW %') THEN
    SELECT * INTO pa FROM provisional_authorizations
     WHERE environment = p_environment AND deployment_id = p_deployment
     ORDER BY authorization_id DESC LIMIT 1;
    IF FOUND AND pa.status = 'ISSUED' AND guard.identity AND pa.code_sha256 = m.artifact_sha256
       AND pa.issued_at >= m.recorded_at AND p_at >= pa.issued_at AND p_at < pa.expires_at
       AND booking_environment_class(p_environment) = 'pilot' THEN
      problems := ARRAY(SELECT x FROM unnest(problems) x WHERE x NOT LIKE 'BOOKING_WORKFLOW %');
      refs := array_append(refs, pa.approval_ref);
    ELSIF FOUND THEN
      problems := ARRAY(SELECT CASE WHEN x LIKE 'BOOKING_WORKFLOW %'
                                    THEN format('BOOKING_WORKFLOW no evidence and the provisional authorization is %s',
                                                CASE WHEN pa.status = 'ISSUED' AND p_at >= pa.expires_at THEN 'EXPIRED'
                                                     ELSE pa.status END)
                                    ELSE x END FROM unnest(problems) x);
    END IF;
  END IF;
  IF cardinality(problems) > 0 THEN
    RETURN 'EVIDENCE_NOT_CURRENT: ' || array_to_string(problems, '; ');
  END IF;
  IF (SELECT count(DISTINCT r) FROM unnest(refs) r) > 1 THEN
    RETURN 'APPROVAL_INCONSISTENT: the exceptions for this deployment name different owner approvals';
  END IF;
  RETURN NULL;
END $$;

-- 0030's entry point (called by the guard trigger) now judges the authenticated login too.
CREATE OR REPLACE FUNCTION booking_evidence_verdict(p_environment text, p_deployment text, p_code text,
                                                    p_at timestamptz)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT booking_evidence_verdict(p_environment, p_deployment, p_code, p_at, session_user::text)
$$;

-- ================================================================ 4. issue, consume, revoke, expire
CREATE FUNCTION issue_provisional_authorization(p_environment text, p_deployment text, p_release_sha text,
                                                p_approval_ref text, p_pipeline_run text, p_minutes integer DEFAULT 60)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  g record; m record; prev record; pr record; cur record; now_ timestamptz; n integer; r provisional_authorizations;
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
  -- serialize with manifests, commitments and other issuers (0030's lock, exclusive)
  PERFORM pg_advisory_xact_lock(hashtextextended('setuhaul.booking_evidence_guard', 0));
  now_ := clock_timestamp();
  -- audit hygiene: an ISSUED authorization past its window becomes EXPIRED (enforcement never needed this)
  UPDATE provisional_authorizations SET status = 'EXPIRED', closed_by = 'expiry',
         close_reason = 'window ended without BOOKING_WORKFLOW evidence'
   WHERE environment = p_environment AND status = 'ISSUED' AND expires_at <= now_;
  SELECT deployment_id, artifact_sha256, recorded_at INTO m
    FROM deployment_manifests WHERE environment = p_environment ORDER BY manifest_id DESC LIMIT 1;
  IF NOT FOUND OR m.deployment_id <> p_deployment THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: % is not the newest recorded deployment of %', left(p_deployment, 14),
      p_environment USING ERRCODE = 'P0001';
  END IF;
  -- the previous release (newest other deployment) must have had complete NORMAL evidence: no chaining
  SELECT deployment_id, recorded_at INTO prev FROM deployment_manifests
   WHERE environment = p_environment AND deployment_id <> p_deployment ORDER BY manifest_id DESC LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: no previous release is recorded for %', p_environment USING ERRCODE = 'P0001';
  END IF;
  SELECT * INTO pr FROM booking_gate_problems(p_environment, prev.deployment_id, prev.recorded_at, now_, NULL);
  IF cardinality(pr.problems) > 0 THEN
    RAISE EXCEPTION 'PROVISIONAL_REFUSED: the previous release % has no complete evidence (%)',
      left(prev.deployment_id, 14), array_to_string(pr.problems, '; ') USING ERRCODE = 'P0001';
  END IF;
  -- the five other gates of this release pass normally, under this approval only
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
                                          approval_ref, pipeline_run, issued_by, issued_at, expires_at)
  VALUES (p_environment, p_deployment, m.artifact_sha256, prev.deployment_id, p_release_sha, p_approval_ref,
          p_pipeline_run, session_user, now_, now_ + make_interval(mins => p_minutes))
  RETURNING * INTO r;
  RETURN jsonb_build_object('authorization_id', r.authorization_id, 'environment', r.environment,
                            'deployment_id', r.deployment_id, 'issued_at', r.issued_at, 'expires_at', r.expires_at,
                            'predecessor_deployment', r.predecessor_deployment, 'approval_ref', r.approval_ref);
END $$;

-- Consumption: the release's BOOKING_WORKFLOW evidence closes its authorization in the same statement; after the
-- window, or once revoked or expired, that evidence is refused (no renewal, no extension).
CREATE FUNCTION trg_provisional_consumption() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE pa provisional_authorizations;
BEGIN
  IF NEW.gate <> 'BOOKING_WORKFLOW' THEN
    RETURN NEW;
  END IF;
  SELECT * INTO pa FROM provisional_authorizations
   WHERE environment = NEW.environment AND deployment_id = NEW.deployment_id FOR UPDATE;
  IF NOT FOUND OR pa.status = 'CONSUMED' THEN
    RETURN NEW;
  END IF;
  IF pa.status <> 'ISSUED' OR clock_timestamp() >= pa.expires_at THEN
    RAISE EXCEPTION 'PROVISIONAL_CLOSED: BOOKING_WORKFLOW evidence for % is refused: its provisional authorization is %',
      left(NEW.deployment_id, 14), CASE WHEN pa.status = 'ISSUED' THEN 'EXPIRED' ELSE pa.status END
      USING ERRCODE = 'P0001', HINT = 'No renewal or extension: ship a new release with a new owner approval.';
  END IF;
  UPDATE provisional_authorizations
     SET status = 'CONSUMED', consumed_evidence_id = NEW.evidence_id, closed_by = session_user,
         close_reason = 'BOOKING_WORKFLOW evidence recorded inside the window'
   WHERE authorization_id = pa.authorization_id;
  RETURN NEW;
END $$;
-- after aa_evidence_requires_guard (0031), before 0013/0014's checks (deployment_evidence_*): if any later check
-- refuses the row, the consumption is rolled back with it
CREATE TRIGGER ab_provisional_consumption BEFORE INSERT ON deployment_evidence
  FOR EACH ROW EXECUTE FUNCTION trg_provisional_consumption();

CREATE FUNCTION revoke_provisional_authorization(p_environment text, p_reason text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'REASON_REQUIRED' USING ERRCODE = 'P0001';
  END IF;
  UPDATE provisional_authorizations SET status = 'REVOKED', closed_by = session_user, close_reason = left(p_reason, 300)
   WHERE environment = p_environment AND status = 'ISSUED';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- The ops lead's path (console, verified staff identity), like the kill switch (0026).
CREATE FUNCTION revoke_provisional_authorization_staff(p_environment text, p_principal text, p_reason text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); n integer;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  PERFORM staff_role_for(ctx, ARRAY['ops_lead'], 'revoking a provisional booking authorization');
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'REASON_REQUIRED' USING ERRCODE = 'P0001';
  END IF;
  UPDATE provisional_authorizations SET status = 'REVOKED', closed_by = ctx.principal, close_reason = left(p_reason, 300)
   WHERE environment = p_environment AND status = 'ISSUED';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

CREATE FUNCTION expire_provisional_authorizations() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN
  UPDATE provisional_authorizations SET status = 'EXPIRED', closed_by = 'expiry',
         close_reason = 'window ended without BOOKING_WORKFLOW evidence'
   WHERE status = 'ISSUED' AND expires_at <= clock_timestamp();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Read-only view for the start-up check, /health and the pipeline: the newest authorization of one deployment.
CREATE FUNCTION booking_provisional_snapshot(p_environment text, p_deployment text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT jsonb_build_object('authorization_id', pa.authorization_id, 'status',
                            CASE WHEN pa.status = 'ISSUED' AND clock_timestamp() >= pa.expires_at THEN 'EXPIRED'
                                 ELSE pa.status END,
                            'open', pa.status = 'ISSUED' AND clock_timestamp() >= pa.issued_at
                                    AND clock_timestamp() < pa.expires_at,
                            'issued_at', pa.issued_at, 'expires_at', pa.expires_at, 'approval_ref', pa.approval_ref,
                            'release_sha', pa.release_sha, 'code_sha256', pa.code_sha256,
                            'identity_enforced', (SELECT identity_enforced FROM booking_evidence_guard WHERE id = 1))
    FROM provisional_authorizations pa
   WHERE pa.environment = p_environment AND pa.deployment_id = p_deployment
   ORDER BY pa.authorization_id DESC LIMIT 1
$$;

-- ================================================================ ownership and grants
ALTER FUNCTION booking_evidence_verdict(text, text, text, timestamptz, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION booking_evidence_verdict(text, text, text, timestamptz) OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_provisional_audit() OWNER TO setuhaul_rpc;
ALTER FUNCTION issue_provisional_authorization(text, text, text, text, text, integer) OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_provisional_consumption() OWNER TO setuhaul_rpc;
ALTER FUNCTION revoke_provisional_authorization(text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION revoke_provisional_authorization_staff(text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION expire_provisional_authorizations() OWNER TO setuhaul_rpc;
ALTER FUNCTION booking_provisional_snapshot(text, text) OWNER TO setuhaul_rpc;
GRANT SELECT, INSERT, UPDATE ON provisional_authorizations TO setuhaul_rpc;
GRANT SELECT, INSERT ON provisional_authorization_events TO setuhaul_rpc;
GRANT EXECUTE ON FUNCTION booking_identity_logins(text),
                          booking_gate_problems(text, text, timestamptz, timestamptz, text) TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION booking_identity_logins(text), booking_gate_problems(text, text, timestamptz, timestamptz, text),
                           booking_evidence_verdict(text, text, text, timestamptz, text),
                           trg_provisional_transition(), trg_provisional_audit(),
                           issue_provisional_authorization(text, text, text, text, text, integer),
                           trg_provisional_consumption(), revoke_provisional_authorization(text, text),
                           revoke_provisional_authorization_staff(text, text, text),
                           expire_provisional_authorizations(), booking_provisional_snapshot(text, text)
  FROM PUBLIC;
-- the pipeline (deploy-evidence login) issues, revokes on failure and expires; the ops lead revokes through the
-- console; the start-up check and /health read the snapshot (as booking_gate_snapshot, 0026). No table grant.
GRANT EXECUTE ON FUNCTION issue_provisional_authorization(text, text, text, text, text, integer),
                          revoke_provisional_authorization(text, text),
                          expire_provisional_authorizations() TO setuhaul_deploy_evidence;
GRANT EXECUTE ON FUNCTION revoke_provisional_authorization_staff(text, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION booking_provisional_snapshot(text, text)
  TO setuhaul_system, setuhaul_ingest, setuhaul_agent, setuhaul_deploy_evidence;
