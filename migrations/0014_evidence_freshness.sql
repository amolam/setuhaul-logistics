-- 0014 · Review R2.1 (Build step 4, round 3): deployment evidence cannot be dated into the future, and operational
-- evidence expires sooner. Forward-only; 0001–0013 untouched.
--
-- S4-R3-01  0013 limited expires_at to verified_at + 30 days, but nothing stopped a writer from choosing a future
--           verified_at: a row "verified" on 1 Jan 2027 and expiring on 31 Jan 2027 would be accepted for months.
--           Every insert is now checked against the database clock: verified_at may be at most 5 minutes ahead (clock
--           skew), and expires_at at most one validity window from now.
-- S4-R3-02  (part) Validity windows per gate. Configuration evidence (queue, database, workflow definition,
--           identity) keeps the 30-day ceiling; it is also invalidated at once by its fingerprint when the
--           configuration changes. Operational evidence (the recovery worker exercised against the real queue,
--           outbound delivery exercised against the provider) is valid for at most 7 days.
--           The start-up check applies the same rules when it reads, so a row written before 0014 gets no more.
SET search_path = setuhaul, public;

CREATE FUNCTION evidence_max_validity(p_gate text) RETURNS interval LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_gate IN ('RECOVERY_WORKER', 'OUTBOUND_DELIVERY') THEN interval '7 days'
              ELSE interval '30 days' END
$$;

CREATE FUNCTION trg_deployment_evidence_fresh() RETURNS trigger LANGUAGE plpgsql
  SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  t timestamptz := clock_timestamp();
  w interval := evidence_max_validity(NEW.gate);
BEGIN
  IF NEW.verified_at > t + interval '5 minutes' THEN
    RAISE EXCEPTION 'EVIDENCE_FUTURE_DATED: verified_at % is after the database time %', NEW.verified_at, t
      USING ERRCODE = 'P0001';
  END IF;
  IF NEW.expires_at > t + w OR NEW.expires_at > NEW.verified_at + w THEN
    RAISE EXCEPTION 'EVIDENCE_TOO_LONG: % evidence is valid for at most %', NEW.gate, w USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER deployment_evidence_fresh BEFORE INSERT ON deployment_evidence
  FOR EACH ROW EXECUTE FUNCTION trg_deployment_evidence_fresh();

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION evidence_max_validity(text) TO setuhaul_system, setuhaul_deploy_evidence;
