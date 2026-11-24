-- 0031 · M1 (PR #6 LLD review, 24 Nov 2026): no deployment evidence is recorded while the database's booking evidence
-- guard (0030) is off. Forward-only and additive: 0001–0030 untouched. No grant to any application role.
--
-- Gap closed: 0030 leaves the guard OFF on a database that had no recorded deployment (a new environment), and the
-- owner had to remember to turn it on before a first activation. Nothing enforced that, so the first activate-booking
-- run in a new environment could record evidence and switch booking on while the per-commitment check stayed off
-- (fail-open). Now the database refuses to record a deployment manifest, gate evidence or an owner-approved exception
-- unless the guard is enforced. Without evidence the start-up check refuses the booking switch, so a fresh database
-- fails closed until the owner takes the one-way step.
--
-- Invariant after this migration: a database that holds any evidence row has the guard ON.
--   1. migration time   a database that already holds evidence, an exception or a manifest has its guard turned ON
--                       here (0030 already did this for manifests; this adds evidence and exceptions).
--   2. every insert     aa_evidence_requires_guard on deployment_manifests, deployment_evidence and
--                       deployment_exceptions refuses the row unless the guard row says enforced. It fires for every
--                       role, the owner included: the owner's path is to enable the guard first (one-way, 0030).
--   3. no bypass        the guard cannot be turned off (0030); the trigger function runs as setuhaul_rpc (NOBYPASSRLS,
--                       no login), which already reads the guard row (0030), so no new privilege is granted to anyone.
--                       Disabling the trigger needs table ownership, and the service's start-up check refuses the
--                       booking switch if any of these triggers is missing or disabled (GUARD_TRIGGERS, inbound.py).
SET search_path = setuhaul, public;

-- ================================================================ 1. a database that holds evidence is guarded
UPDATE booking_evidence_guard
   SET enforced = true,
       reason = 'migration 0031: this database holds deployment evidence, an exception or a manifest'
 WHERE id = 1 AND NOT enforced
   AND (EXISTS (SELECT 1 FROM deployment_evidence) OR EXISTS (SELECT 1 FROM deployment_exceptions)
        OR EXISTS (SELECT 1 FROM deployment_manifests));

-- ================================================================ 2. refuse recording while the guard is off
CREATE FUNCTION trg_evidence_requires_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  on_ boolean;
BEGIN
  BEGIN
    -- a missing row reads as OFF here (refuse recording): failing closed for recording is the safe direction
    on_ := coalesce((SELECT enforced FROM booking_evidence_guard WHERE id = 1), false);
  EXCEPTION WHEN OTHERS THEN
    on_ := false;
  END;
  IF NOT on_ THEN
    RAISE EXCEPTION 'EVIDENCE_GUARD_OFF: % cannot be recorded while the booking evidence guard is off', TG_TABLE_NAME
      USING ERRCODE = 'P0001',
            HINT = 'The owner enables setuhaul.booking_evidence_guard (one-way) before a first activation; '
                   'see docs/booking-renewal/DATABASE_BOOKING_EVIDENCE_GUARD_RUNBOOK.md';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION trg_evidence_requires_guard() OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION trg_evidence_requires_guard() FROM PUBLIC;

-- named aa_ so it fires before every other BEFORE INSERT trigger on these tables (PostgreSQL orders them by name):
-- a refused manifest never takes the manifest lock (0030) and a refused row never reaches the freshness checks
CREATE TRIGGER aa_evidence_requires_guard BEFORE INSERT ON deployment_manifests
  FOR EACH ROW EXECUTE FUNCTION trg_evidence_requires_guard();
CREATE TRIGGER aa_evidence_requires_guard BEFORE INSERT ON deployment_evidence
  FOR EACH ROW EXECUTE FUNCTION trg_evidence_requires_guard();
CREATE TRIGGER aa_evidence_requires_guard BEFORE INSERT ON deployment_exceptions
  FOR EACH ROW EXECUTE FUNCTION trg_evidence_requires_guard();
-- No GRANT: setuhaul_rpc's SELECT on booking_evidence_guard comes from 0030; no application role gains anything.
