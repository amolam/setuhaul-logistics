-- 0013 · Review R2.0 (Build step 4, round 2): deployment evidence that is fresh, bound and append-only.
-- Forward-only; 0001–0012 untouched.
--
-- S4-R2-04  0012's deployment_gates proved only that a gate NAME had been written once: no environment, no deployment,
--           no expiry, no link to the configuration it verified. It is replaced by deployment_evidence: one immutable
--           row per verification, bound to an environment and a deployment id, with an expiry, an artifact URI and its
--           SHA-256, and a fingerprint of the configuration that was verified. A later row supersedes an earlier one;
--           nothing is updated or deleted. The service starts with the booking switch on only if every gate has a
--           current, unexpired row for THIS environment and deployment, and, where the service can compute it itself
--           (queue, database), the fingerprint matches what it sees now.
-- S4-R2-05  A dedicated writer role, setuhaul_deploy_evidence, may only INSERT here. No application role can write.
--           In step 6 the deployment pipeline's short-lived identity maps to it; that mapping is verified there.
-- S4-R2-06  Six gates: AWS_QUEUE, AURORA, RECOVERY_WORKER, BOOKING_WORKFLOW, WORKLOAD_IDENTITY, OUTBOUND_DELIVERY.
--           What each must cover, including its monitoring, is in DEPLOYMENT_GATES.md.
SET search_path = setuhaul, public;

DROP TABLE deployment_gates;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_deploy_evidence') THEN
    CREATE ROLE setuhaul_deploy_evidence NOLOGIN; END IF;
END $$;
ALTER ROLE setuhaul_deploy_evidence NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOLOGIN;
GRANT USAGE ON SCHEMA setuhaul TO setuhaul_deploy_evidence;

CREATE TABLE deployment_evidence (
  evidence_id         bigserial PRIMARY KEY,
  environment         text NOT NULL CHECK (environment ~ '^[a-z0-9-]{2,32}$'),
  deployment_id       text NOT NULL CHECK (length(deployment_id) BETWEEN 6 AND 128),
  gate                text NOT NULL CHECK (gate IN ('AWS_QUEUE','AURORA','RECOVERY_WORKER','BOOKING_WORKFLOW',
                                                    'WORKLOAD_IDENTITY','OUTBOUND_DELIVERY')),
  verified_at         timestamptz NOT NULL DEFAULT now(),
  expires_at          timestamptz NOT NULL,
  verifier            text NOT NULL DEFAULT session_user,               -- who wrote it, not what they claim
  artifact_uri        text NOT NULL CHECK (artifact_uri ~ '^(s3|https)://'),
  evidence_sha256     text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  config_fingerprint  text NOT NULL CHECK (config_fingerprint ~ '^[0-9a-f]{64}$'),
  supersedes          bigint REFERENCES deployment_evidence,
  CHECK (expires_at > verified_at AND expires_at <= verified_at + interval '30 days')
);
CREATE INDEX ix_deployment_evidence_current ON deployment_evidence (environment, deployment_id, gate, evidence_id DESC);

-- append-only, for every role including the owner
CREATE FUNCTION trg_deployment_evidence_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'APPEND_ONLY: deployment evidence is never changed or removed; supersede it' USING ERRCODE = 'P0001'; END $$;
CREATE TRIGGER deployment_evidence_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON deployment_evidence
  FOR EACH STATEMENT EXECUTE FUNCTION trg_deployment_evidence_append_only();

GRANT INSERT ON deployment_evidence TO setuhaul_deploy_evidence;
GRANT USAGE ON SEQUENCE deployment_evidence_evidence_id_seq TO setuhaul_deploy_evidence;
GRANT SELECT ON deployment_evidence TO setuhaul_system, setuhaul_deploy_evidence;   -- start-up check; supersession

-- the start-up check fingerprints the database it runs on (name, server version, exact migrations)
GRANT USAGE ON SCHEMA setuhaul_meta TO setuhaul_system;
GRANT SELECT ON setuhaul_meta.schema_migrations TO setuhaul_system;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
