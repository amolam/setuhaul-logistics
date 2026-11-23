-- 0026 · Option D activation on the synthetic Sydney pilot (owner's written approval, 23 Nov 2026,
-- OWNER-APPROVAL-20261123-OPTION-D). Forward-only and additive; 0001–0025 untouched. No row is changed or removed.
--
-- 1. deployment_manifests   The deployment pipeline records the manifest of what it deployed (setuhaul/ops/manifest.py).
--                           The functions read the newest one for their environment and check it against their own
--                           code hash, so the booking switch's evidence is bound to the deployment actually running.
--                           Append-only; written only by setuhaul_deploy_evidence (the pipeline's login).
-- 2. deployment_exceptions  An owner-approved exception stands in for a gate's evidence ONLY where the owner accepted
--                           it in writing: T33-P (AURORA on 17.9), no live drift detection (WORKLOAD_IDENTITY) and
--                           Meta deferred with internal test inbox only (OUTBOUND_DELIVERY). Never for a production
--                           environment, never for another gate or code, at most 30 days, bound to the live
--                           configuration fingerprint like evidence. Append-only; written only by the pipeline login.
--                           An exception is not evidence: it is a separate table and gate_evidence.py never writes a
--                           non-PASS status into deployment_evidence.
-- 3. booking_kill_switch    One row. While ENGAGED the database refuses every new booking commitment, whatever the
--                           application or its deployment says: a new hold or appointment claim on dock time, moving
--                           one, a new appointment, or confirming one. Releasing, rejecting, expiring and cancelling
--                           stay possible (they reduce commitments). Operable without a deployment: an ops_lead through
--                           the console (set_booking_kill_switch, audited) or the owner (setuhaul/ops/kill_switch.py).
SET search_path = setuhaul, public;

-- ================================================================ 1. manifests
CREATE TABLE deployment_manifests (
  manifest_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  environment      text NOT NULL CHECK (environment ~ '^[a-z0-9-]{2,32}$'),
  deployment_id    text NOT NULL CHECK (deployment_id ~ '^m-[0-9a-f]{40}$'),
  artifact_sha256  text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  manifest         jsonb NOT NULL CHECK (manifest->>'environment' = environment
                                         AND manifest#>>'{gates,RECOVERY_WORKER,artifact_sha256}' = artifact_sha256),
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  recorded_by      text NOT NULL DEFAULT session_user
);
CREATE INDEX ix_deployment_manifests_newest ON deployment_manifests (environment, manifest_id DESC);

-- ================================================================ 2. owner-approved gate exceptions
CREATE TABLE deployment_exceptions (
  exception_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  environment         text NOT NULL CHECK (environment ~ '^[a-z0-9-]{2,32}$' AND environment !~ 'prod'),
  deployment_id       text NOT NULL CHECK (length(deployment_id) BETWEEN 6 AND 128),
  gate                text NOT NULL,
  exception_code      text NOT NULL,
  approval_ref        text NOT NULL CHECK (approval_ref ~ '^OWNER-APPROVAL-20[0-9]{6}-[A-Za-z0-9-]{3,40}$'),
  reason              text NOT NULL CHECK (length(reason) BETWEEN 10 AND 1000),
  config_fingerprint  text NOT NULL CHECK (config_fingerprint ~ '^[0-9a-f]{64}$'),
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  expires_at          timestamptz NOT NULL,
  recorded_by         text NOT NULL DEFAULT session_user,
  -- the only exceptions the owner has accepted; anything else needs a new migration, so a new decision
  CHECK ((gate, exception_code) IN (('AURORA', 'T33-P'),
                                    ('WORKLOAD_IDENTITY', 'NO-LIVE-DRIFT-DETECTION'),
                                    ('OUTBOUND_DELIVERY', 'META-DEFERRED-TEST-SINK-ONLY'))),
  CHECK (expires_at > recorded_at AND expires_at <= recorded_at + interval '30 days')
);
CREATE INDEX ix_deployment_exceptions_current ON deployment_exceptions (environment, deployment_id, gate, exception_id DESC);

-- dated by the database clock, whatever the caller sends (as 0014 does for evidence)
CREATE FUNCTION trg_deployment_exception_clock() RETURNS trigger LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog AS $$
BEGIN
  IF NEW.recorded_at > clock_timestamp() + interval '5 minutes' OR NEW.recorded_at < clock_timestamp() - interval '5 minutes' THEN
    RAISE EXCEPTION 'EXCEPTION_MISDATED' USING ERRCODE = 'P0001'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER deployment_exceptions_clock BEFORE INSERT ON deployment_exceptions
  FOR EACH ROW EXECUTE FUNCTION trg_deployment_exception_clock();

CREATE FUNCTION trg_activation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'APPEND_ONLY: % is never changed or removed', TG_TABLE_NAME USING ERRCODE = 'P0001'; END $$;
CREATE TRIGGER deployment_manifests_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON deployment_manifests
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();
CREATE TRIGGER deployment_exceptions_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON deployment_exceptions
  FOR EACH STATEMENT EXECUTE FUNCTION trg_activation_append_only();

GRANT INSERT ON deployment_manifests, deployment_exceptions TO setuhaul_deploy_evidence;
GRANT SELECT ON deployment_manifests, deployment_exceptions TO setuhaul_deploy_evidence;

-- ================================================================ 3. the booking kill switch
CREATE TABLE booking_kill_switch (
  id          smallint PRIMARY KEY CHECK (id = 1),
  engaged     boolean NOT NULL,
  changed_at  timestamptz NOT NULL DEFAULT now(),
  changed_by  text NOT NULL,
  reason      text NOT NULL
);
INSERT INTO booking_kill_switch VALUES (1, false, now(), 'migration:0026', 'created disengaged; the booking switch itself is still off');
CREATE FUNCTION trg_kill_switch_single() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'KILL_SWITCH_SINGLE_ROW' USING ERRCODE = 'P0001'; END $$;
CREATE TRIGGER booking_kill_switch_single BEFORE INSERT OR DELETE OR TRUNCATE ON booking_kill_switch
  FOR EACH STATEMENT EXECUTE FUNCTION trg_kill_switch_single();

CREATE FUNCTION booking_killed() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$ SELECT engaged FROM booking_kill_switch WHERE id = 1 $$;

-- SECURITY DEFINER (owner setuhaul_rpc, below) so it can read the switch whoever writes the row; it changes nothing
CREATE FUNCTION trg_refuse_booking_when_killed() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE commit_new boolean;
BEGIN
  IF NOT (SELECT engaged FROM booking_kill_switch WHERE id = 1) THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME = 'dock_occupancy' THEN
    -- a new or re-activated claim on dock time for a truck, a changed time, or a hold becoming an appointment;
    -- operational blocks (BLOCK, FIXED) and releases (active -> false) are not booking commitments
    commit_new := NEW.active AND NEW.kind IN ('HOLD', 'APPOINTMENT') AND (TG_OP = 'INSERT'
                  OR NOT OLD.active OR NEW.during IS DISTINCT FROM OLD.during OR NEW.kind IS DISTINCT FROM OLD.kind
                  OR NEW.dock_id IS DISTINCT FROM OLD.dock_id);
  ELSE                                                                  -- appointments
    commit_new := (TG_OP = 'INSERT' AND NEW.status IN ('PENDING_CONFIRMATION', 'CONFIRMED'))
               OR (TG_OP = 'UPDATE' AND NEW.status = 'CONFIRMED' AND OLD.status IS DISTINCT FROM 'CONFIRMED')
               OR (TG_OP = 'UPDATE' AND NEW.status IN ('PENDING_CONFIRMATION', 'CONFIRMED')
                   AND (NEW.during IS DISTINCT FROM OLD.during OR NEW.dock_id IS DISTINCT FROM OLD.dock_id));
  END IF;
  IF commit_new THEN
    RAISE EXCEPTION 'BOOKING_KILLED: the booking kill switch is engaged; no new booking commitment is accepted'
      USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;
-- named to fire first among the BEFORE triggers (PostgreSQL orders them by name), so a refusal names the switch
CREATE TRIGGER aa_booking_kill_switch BEFORE INSERT OR UPDATE ON dock_occupancy
  FOR EACH ROW EXECUTE FUNCTION trg_refuse_booking_when_killed();
CREATE TRIGGER aa_booking_kill_switch BEFORE INSERT OR UPDATE ON appointments
  FOR EACH ROW EXECUTE FUNCTION trg_refuse_booking_when_killed();

-- the console path: an ops_lead (trusted staff context, as set_automation_paused) engages or disengages it; audited
CREATE FUNCTION set_booking_kill_switch(p_engaged boolean, p_principal text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); role text; was boolean;
BEGIN
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['ops_lead'], 'operating the booking kill switch');
  IF p_reason IS NULL OR length(trim(p_reason)) < 5 THEN
    RAISE EXCEPTION 'REASON_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  SELECT engaged INTO was FROM booking_kill_switch WHERE id = 1 FOR UPDATE;
  UPDATE booking_kill_switch SET engaged = p_engaged, changed_at = now(), changed_by = ctx.principal,
         reason = p_reason WHERE id = 1;
  INSERT INTO booking_events (principal, principal_role, action, reason, before_state, after_state)
  VALUES (ctx.principal, role, CASE WHEN p_engaged THEN 'BOOKING_KILL_ENGAGED' ELSE 'BOOKING_KILL_DISENGAGED' END,
          p_reason, jsonb_build_object('engaged', was), jsonb_build_object('engaged', p_engaged));
  RETURN p_engaged;
END $$;

-- ================================================================ 4. one read-only view of the gates
-- The start-up check and /health need the gate rows, the database fingerprint inputs, the guard triggers, the kill
-- switch and the newest manifest. Rather than table grants to the api (ingest) and agent logins, one read-only
-- definer function returns exactly that; it changes nothing and returns no facility, driver or booking data.
CREATE FUNCTION booking_gate_snapshot(p_environment text, p_deployment text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT jsonb_build_object(
    'database', current_database(),
    'server_version_num', current_setting('server_version_num'),
    'migrations', (SELECT coalesce(jsonb_agg(jsonb_build_array(version, sha256) ORDER BY version), '[]'::jsonb)
                     FROM setuhaul_meta.schema_migrations),
    'guards', (SELECT coalesce(jsonb_agg(jsonb_build_array(r.relname::text, t.tgname::text)), '[]'::jsonb)
                 FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid JOIN pg_namespace s ON s.oid = r.relnamespace
                WHERE s.nspname = 'setuhaul' AND t.tgenabled IN ('O','A') AND NOT t.tgisinternal),
    'kill_switch', (SELECT engaged FROM booking_kill_switch WHERE id = 1),
    'manifest', (SELECT jsonb_build_object('deployment_id', deployment_id, 'artifact_sha256', artifact_sha256,
                                           'manifest', manifest)
                   FROM deployment_manifests WHERE environment = p_environment ORDER BY manifest_id DESC LIMIT 1),
    -- the same freshness rules as the evidence insert trigger (0014)
    'evidence', (SELECT coalesce(jsonb_agg(jsonb_build_array(gate, fresh, config_fingerprint)), '[]'::jsonb) FROM (
                   SELECT DISTINCT ON (gate) gate, config_fingerprint,
                          expires_at > now() AND verified_at <= now() + interval '5 minutes'
                            AND expires_at <= now() + evidence_max_validity(gate)
                            AND expires_at <= verified_at + evidence_max_validity(gate) AS fresh
                     FROM deployment_evidence WHERE environment = p_environment AND deployment_id = p_deployment
                    ORDER BY gate, evidence_id DESC) e),
    'exceptions', (SELECT coalesce(jsonb_agg(jsonb_build_array(gate, exception_code, expires_at > now(),
                                                               config_fingerprint, approval_ref)), '[]'::jsonb) FROM (
                     SELECT DISTINCT ON (gate) * FROM deployment_exceptions
                      WHERE environment = p_environment AND deployment_id = p_deployment
                      ORDER BY gate, exception_id DESC) x))
$$;

ALTER FUNCTION booking_killed() OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_refuse_booking_when_killed() OWNER TO setuhaul_rpc;
ALTER FUNCTION set_booking_kill_switch(boolean, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION booking_gate_snapshot(text, text) OWNER TO setuhaul_rpc;
GRANT SELECT, UPDATE ON booking_kill_switch TO setuhaul_rpc;
GRANT SELECT ON deployment_evidence, deployment_manifests, deployment_exceptions TO setuhaul_rpc;
GRANT USAGE ON SCHEMA setuhaul_meta TO setuhaul_rpc;
GRANT SELECT ON setuhaul_meta.schema_migrations TO setuhaul_rpc;
GRANT INSERT ON booking_events TO setuhaul_rpc;
GRANT EXECUTE ON FUNCTION evidence_max_validity(text) TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION booking_killed() TO setuhaul_console;
GRANT EXECUTE ON FUNCTION set_booking_kill_switch(boolean, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION booking_gate_snapshot(text, text)
  TO setuhaul_system, setuhaul_ingest, setuhaul_agent, setuhaul_deploy_evidence;
