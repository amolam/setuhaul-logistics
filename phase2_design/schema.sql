-- SetuHaul exception service — system-of-record schema (Aurora PostgreSQL 16+/ PostgreSQL 18 tested locally)
-- Phase 2 Design. Integrity is enforced HERE, not in the agent:
--   * one dock, one truck at a time  -> EXCLUDE constraint on dock_occupancy (covers holds, bookings, blocks, in-dock work)
--   * dock fits the load             -> trigger trg_occupancy_compat
--   * one current active booking per shipment -> partial unique index
--   * idempotent writes / deduped messages -> unique keys
--   * append-only audit              -> no UPDATE/DELETE on booking_events
-- All timestamps are timestamptz. Facility local time = Asia/Kolkata.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS setuhaul;
SET search_path = setuhaul, public;

-- ---------------------------------------------------------------- reference data (synced from TMS / WMS)
CREATE TABLE carriers (
  carrier_id    text PRIMARY KEY,
  carrier_name  text NOT NULL UNIQUE,
  active        boolean NOT NULL DEFAULT true
);

CREATE TABLE drivers (
  driver_id     text PRIMARY KEY,
  carrier_id    text NOT NULL REFERENCES carriers,
  driver_name   text NOT NULL,
  phone_e164    text NOT NULL UNIQUE,                 -- identity anchor for WhatsApp / OTP
  cognito_sub   uuid UNIQUE,                          -- set when the driver has a Cognito identity
  status        text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','OFF_DUTY','SUSPENDED','INACTIVE'))
);

CREATE TABLE vehicles (
  vehicle_id            text PRIMARY KEY,
  carrier_id            text NOT NULL REFERENCES carriers,
  vehicle_type_code     text NOT NULL,
  registration_number   text NOT NULL UNIQUE,
  capacity_kg           integer NOT NULL CHECK (capacity_kg > 0),
  refrigeration_capable boolean NOT NULL DEFAULT false
);

CREATE TABLE facilities (
  facility_id      text PRIMARY KEY,
  facility_name    text NOT NULL UNIQUE,
  timezone         text NOT NULL DEFAULT 'Asia/Kolkata',
  open_time        time NOT NULL,
  close_time       time NOT NULL,
  schedule_version bigint NOT NULL DEFAULT 0,          -- bumped on every capacity change (stale-option detection, scheduler trigger)
  automation_paused boolean NOT NULL DEFAULT false     -- human "pause switch" (HITL mode E)
);

-- Versioned per-facility configuration (timings learned per facility, TTLs per shift). One active row per facility.
CREATE TABLE facility_config (
  facility_id                text NOT NULL REFERENCES facilities,
  cfg_version                integer NOT NULL,
  effective_from             timestamptz NOT NULL DEFAULT now(),
  gate_to_dock_min           integer NOT NULL,
  changeover_min             integer NOT NULL,
  no_show_grace_min          integer NOT NULL,
  checkin_early_limit_min    integer,
  heavy_dock_required_kg     integer,
  last_new_start_local       time,
  confirm_lead_min           integer NOT NULL DEFAULT 30,
  warehouse_confirm_ttl_min  jsonb NOT NULL,             -- {"DAY":15,"NIGHT":0}
  shifts                     jsonb NOT NULL,             -- [{"name":"DAY","start":"06:00","end":"14:00"},...]
  auto_confirm_chat_bookings jsonb NOT NULL DEFAULT '{}',-- {"NIGHT":false}
  max_yard_wait_min          integer NOT NULL DEFAULT 90,
  timing_basis               text NOT NULL DEFAULT 'SEED' CHECK (timing_basis IN ('SEED','LEARNED')),
  active                     boolean NOT NULL DEFAULT true,
  PRIMARY KEY (facility_id, cfg_version)
);
CREATE UNIQUE INDEX ux_facility_config_active ON facility_config (facility_id) WHERE active;

CREATE TABLE docks (
  dock_id               text PRIMARY KEY,
  facility_id           text NOT NULL REFERENCES facilities,
  dock_code             text NOT NULL,
  dock_type             text NOT NULL CHECK (dock_type IN ('STANDARD','REEFER','HEAVY')),
  supports_refrigerated boolean NOT NULL DEFAULT false,
  max_vehicle_weight_kg integer NOT NULL CHECK (max_vehicle_weight_kg > 0),
  slot_len_min          integer NOT NULL DEFAULT 60,
  UNIQUE (facility_id, dock_code),
  UNIQUE (dock_id, facility_id)
);

CREATE TABLE shipments (
  shipment_id                  text PRIMARY KEY,
  order_reference              text NOT NULL UNIQUE,
  carrier_id                   text NOT NULL REFERENCES carriers,
  driver_id                    text NOT NULL REFERENCES drivers,
  vehicle_id                   text NOT NULL REFERENCES vehicles,
  destination_facility_id      text NOT NULL REFERENCES facilities,
  load_weight_kg               integer NOT NULL CHECK (load_weight_kg > 0),
  required_dock_type           text NOT NULL DEFAULT 'ANY' CHECK (required_dock_type IN ('ANY','STANDARD','REEFER','HEAVY')),
  temperature_control_required boolean NOT NULL DEFAULT false,
  priority_code                text NOT NULL DEFAULT 'NORMAL' CHECK (priority_code IN ('LOW','NORMAL','HIGH','CRITICAL')),
  expected_unload_min          integer NOT NULL CHECK (expected_unload_min > 0),
  planned_departure_ts         timestamptz NOT NULL,
  actual_departure_ts          timestamptz,
  original_eta_ts              timestamptz NOT NULL,
  current_status               text NOT NULL CHECK (current_status IN ('PLANNED','ASSIGNED','IN_TRANSIT','AT_GATE','WAITING','IN_DOCK','COMPLETED','CANCELLED')),
  updated_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_shipments_dest_status ON shipments (destination_facility_id, current_status);
CREATE INDEX ix_shipments_driver ON shipments (driver_id) WHERE current_status NOT IN ('COMPLETED','CANCELLED');

-- ---------------------------------------------------------------- the integrity core
-- Every claim on dock time is a row here: holds, bookings, dock blocks (breakdown/maintenance), trucks already in dock.
-- The EXCLUDE constraint makes double occupancy impossible regardless of how many agents, people or retries race.
CREATE TABLE dock_occupancy (
  occupancy_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  dock_id        text NOT NULL,
  facility_id    text NOT NULL,
  during         tstzrange NOT NULL CHECK (NOT isempty(during) AND lower_inc(during) AND NOT upper_inc(during)),
  kind           text NOT NULL CHECK (kind IN ('HOLD','APPOINTMENT','BLOCK','FIXED')),
  shipment_id    text REFERENCES shipments,
  vehicle_id     text,                                  -- set by trigger from the shipment (callers cannot spoof it)
  ref_id         text,                                  -- hold_id / appointment_id / dock_event_id
  active         boolean NOT NULL DEFAULT true,
  expires_at     timestamptz,                           -- holds only
  created_by     text NOT NULL,                         -- principal (agent / user / system)
  created_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (dock_id, facility_id) REFERENCES docks (dock_id, facility_id),
  CHECK ((kind = 'HOLD') = (expires_at IS NOT NULL)),
  CHECK ((kind IN ('HOLD','APPOINTMENT','FIXED')) = (shipment_id IS NOT NULL)),
  CONSTRAINT ex_one_truck_per_dock EXCLUDE USING gist (dock_id WITH =, during WITH &&) WHERE (active),
  -- Review P1: the same truck cannot hold or book two docks at overlapping times for DIFFERENT shipments.
  -- (Rows of the same shipment may overlap each other, e.g. an at-risk booking while its replacement hold exists.)
  CONSTRAINT ex_one_dock_per_truck EXCLUDE USING gist (vehicle_id WITH =, during WITH &&, shipment_id WITH <>)
    WHERE (active AND vehicle_id IS NOT NULL)
);
CREATE INDEX ix_occupancy_shipment ON dock_occupancy (shipment_id) WHERE active;
CREATE INDEX ix_occupancy_expiry ON dock_occupancy (expires_at) WHERE active AND kind = 'HOLD';
-- one active hold per shipment (a driver cannot hoard capacity)
CREATE UNIQUE INDEX ux_one_active_hold_per_shipment ON dock_occupancy (shipment_id) WHERE active AND kind = 'HOLD';

-- Compatibility guard: the dock must accept the load, the dock must be at the shipment's destination,
-- and a cancelled/completed shipment cannot claim dock time. (Closes audit gaps 3-6.)
CREATE OR REPLACE FUNCTION trg_occupancy_compat() RETURNS trigger LANGUAGE plpgsql SET search_path = setuhaul, public AS $$
DECLARE s shipments; d docks; heavy_kg integer;
BEGIN
  IF NEW.shipment_id IS NULL OR NOT NEW.active THEN RETURN NEW; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = NEW.shipment_id;
  -- shipments has RLS: a shipment the caller may not see must FAIL the write, never skip the checks below
  -- (a NULL row would make every comparison NULL and silently pass, and vehicle_id would escape the truck guard).
  IF NOT FOUND THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_VISIBLE: % is outside the caller''s driver or facility scope', NEW.shipment_id
      USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO d FROM docks WHERE dock_id = NEW.dock_id;
  SELECT heavy_dock_required_kg INTO heavy_kg FROM facility_config WHERE facility_id = d.facility_id AND active;
  IF s.destination_facility_id <> d.facility_id THEN
    RAISE EXCEPTION 'DOCK_WRONG_FACILITY: % is not at %', d.dock_id, s.destination_facility_id USING ERRCODE = 'P0001'; END IF;
  IF s.current_status IN ('CANCELLED','COMPLETED') AND NEW.kind <> 'FIXED' THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_ELIGIBLE: % is %', s.shipment_id, s.current_status USING ERRCODE = 'P0001'; END IF;
  IF s.load_weight_kg > d.max_vehicle_weight_kg THEN
    RAISE EXCEPTION 'DOCK_WEIGHT_EXCEEDED: % kg > % kg on %', s.load_weight_kg, d.max_vehicle_weight_kg, d.dock_code USING ERRCODE = 'P0001'; END IF;
  IF s.temperature_control_required AND NOT d.supports_refrigerated THEN
    RAISE EXCEPTION 'DOCK_NOT_REFRIGERATED: %', d.dock_code USING ERRCODE = 'P0001'; END IF;
  IF (s.required_dock_type = 'HEAVY' OR (heavy_kg IS NOT NULL AND s.load_weight_kg > heavy_kg)) AND d.dock_type <> 'HEAVY' THEN
    RAISE EXCEPTION 'DOCK_NOT_HEAVY: %', d.dock_code USING ERRCODE = 'P0001'; END IF;
  NEW.vehicle_id := s.vehicle_id;   -- truck-level exclusion uses the shipment's assigned vehicle
  RETURN NEW;
END $$;
CREATE TRIGGER occupancy_compat BEFORE INSERT OR UPDATE OF dock_id, during, shipment_id, active ON dock_occupancy
  FOR EACH ROW EXECUTE FUNCTION trg_occupancy_compat();

-- Bump the facility schedule version on every capacity change (options carry the version they were computed at).
CREATE OR REPLACE FUNCTION trg_bump_schedule_version() RETURNS trigger LANGUAGE plpgsql SET search_path = setuhaul, public AS $$
BEGIN
  UPDATE facilities SET schedule_version = schedule_version + 1
   WHERE facility_id = COALESCE(NEW.facility_id, OLD.facility_id);
  RETURN NULL;
END $$;
CREATE TRIGGER occupancy_version AFTER INSERT OR UPDATE OR DELETE ON dock_occupancy
  FOR EACH ROW EXECUTE FUNCTION trg_bump_schedule_version();

-- Planning grid shown to warehouse planners (slots are a view of time, not the integrity mechanism).
CREATE TABLE appointment_slots (
  slot_id      text PRIMARY KEY,
  dock_id      text NOT NULL REFERENCES docks,
  slot         tstzrange NOT NULL,
  slot_status  text NOT NULL DEFAULT 'OPEN' CHECK (slot_status IN ('OPEN','BLOCKED','CLOSED')),
  block_reason text,
  EXCLUDE USING gist (dock_id WITH =, slot WITH &&)       -- slots on one dock never overlap (audit gap 1)
);

CREATE TABLE dock_events (
  dock_event_id text PRIMARY KEY,
  dock_id       text NOT NULL REFERENCES docks,
  event_type    text NOT NULL CHECK (event_type IN ('MAINTENANCE','BREAKDOWN','CAPACITY_REDUCTION','REOPENED','MANUAL_BLOCK')),
  during        tstzrange NOT NULL,
  reason        text NOT NULL,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
-- A BREAKDOWN / MAINTENANCE / MANUAL_BLOCK becomes a BLOCK occupancy row in the same transaction. If a booking already
-- sits there, the insert fails -> the service first moves affected bookings to AT_RISK and re-plans (never silently).

CREATE TABLE appointments (
  appointment_id          text PRIMARY KEY,
  shipment_id             text NOT NULL REFERENCES shipments,
  dock_id                 text NOT NULL REFERENCES docks,
  during                  tstzrange NOT NULL,
  occupancy_id            bigint REFERENCES dock_occupancy,
  status                  text NOT NULL CHECK (status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','COMPLETED','CANCELLED','NO_SHOW','REJECTED','EXPIRED')),
  at_risk                 boolean NOT NULL DEFAULT false,
  is_current              boolean NOT NULL DEFAULT true,
  booking_source          text NOT NULL CHECK (booking_source IN ('PLANNER','DRIVER_CHAT','WAREHOUSE','SCHEDULING_TOOL','MANUAL_OVERRIDE')),
  pending_expires_at      timestamptz,
  replaced_appointment_id text REFERENCES appointments,
  needs_approval_reason   text,
  warehouse_confirmation_ref text,
  row_version             integer NOT NULL DEFAULT 1,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'PENDING_CONFIRMATION' OR pending_expires_at IS NOT NULL)
);
CREATE UNIQUE INDEX ux_current_active_appointment_per_shipment ON appointments (shipment_id)
  WHERE is_current AND status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');

-- ---------------------------------------------------------------- options, holds, approvals
CREATE TABLE shown_options (
  option_id          uuid PRIMARY KEY,
  thread_id          text NOT NULL,
  shipment_id        text NOT NULL REFERENCES shipments,
  dock_id            text NOT NULL REFERENCES docks,
  during             tstzrange NOT NULL,
  needs_approval     boolean NOT NULL,
  rank               smallint NOT NULL,
  schedule_version   bigint NOT NULL,         -- facility schedule version at computation time
  expires_at         timestamptz NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_options_thread ON shown_options (thread_id, created_at DESC);

CREATE TABLE approval_requests (
  approval_id     uuid PRIMARY KEY,
  kind            text NOT NULL CHECK (kind IN ('AFTER_HOURS_START','MOVE_CONFIRMED_PROMISE','DOCK_DOWNGRADE','PRIORITY_OVERRIDE','SCHEDULE_PROPOSAL')),
  facility_id     text NOT NULL REFERENCES facilities,
  shipment_id     text REFERENCES shipments,
  proposed_action jsonb NOT NULL,
  evidence        jsonb NOT NULL,
  status          text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','APPROVED','REJECTED','EXPIRED','SUPERSEDED')),
  expires_at      timestamptz NOT NULL,
  decided_by      text,
  decided_at      timestamptz,
  decision_note   text,
  task_token      text,                        -- Step Functions waitForTaskToken
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE escalations (
  escalation_id    uuid PRIMARY KEY,
  reason_code      text NOT NULL CHECK (reason_code IN ('NO_FEASIBLE_SLOT','SAFETY','CONTRADICTORY_DATA','AFTER_HOURS_APPROVAL','CRITICAL_CONFLICT',
                                                         'CLARIFICATION_LIMIT','PENDING_EXPIRED','DRIVER_REQUEST','OUT_OF_SCOPE','AGENT_FAILURE','NOTIFICATION_FAILED')),
  mode             text NOT NULL CHECK (mode IN ('DECIDE','TAKE_OVER')),
  facility_id      text NOT NULL REFERENCES facilities,
  shipment_id      text REFERENCES shipments,
  thread_id        text,
  priority_code    text,
  handoff          jsonb NOT NULL,             -- schema-validated hand-off record (02_variables §7)
  status           text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED','REROUTED')),
  assigned_to      text,
  ack_due_at       timestamptz NOT NULL,
  resolve_by       timestamptz,
  acknowledged_at  timestamptz,
  resolved_at      timestamptz,
  resolution       jsonb,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_escalations_queue ON escalations (facility_id, status, ack_due_at);

-- ---------------------------------------------------------------- conversation, signals, idempotency, outbox, audit
CREATE TABLE chat_threads (
  thread_id     text PRIMARY KEY,
  driver_id     text NOT NULL REFERENCES drivers,
  shipment_id   text REFERENCES shipments,     -- null until resolved (THR010 case)
  status        text NOT NULL CHECK (status IN ('OPEN','WAITING_FOR_DRIVER','WAITING_FOR_WAREHOUSE','WAITING_FOR_HUMAN','RESOLVED','ESCALATED','CLOSED')),
  human_active  boolean NOT NULL DEFAULT false, -- takeover flag: agent must not reply (eval D13)
  clarification_turns smallint NOT NULL DEFAULT 0,
  opened_at     timestamptz NOT NULL DEFAULT now(),
  closed_at     timestamptz
);

CREATE TABLE chat_messages (
  message_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  thread_id           text NOT NULL REFERENCES chat_threads,
  sender_type         text NOT NULL CHECK (sender_type IN ('DRIVER','AGENT','OPERATIONS','WAREHOUSE','SYSTEM')),
  channel             text NOT NULL CHECK (channel IN ('WHATSAPP','WEB','SMS','CONSOLE')),
  external_message_id text,
  content_hash        text NOT NULL,           -- sha256 of normalised text (dedupe across retries with new ids)
  body_masked         text NOT NULL,           -- PII-masked text; raw text kept encrypted in S3 with short retention
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, external_message_id)
);

CREATE TABLE driver_exceptions (
  exception_id    text PRIMARY KEY,
  shipment_id     text REFERENCES shipments,
  driver_id       text NOT NULL REFERENCES drivers,
  thread_id       text NOT NULL REFERENCES chat_threads,
  exception_type  text NOT NULL,
  status          text NOT NULL,
  dedupe_key      text NOT NULL UNIQUE,         -- audit A18: duplicates cannot be stored twice
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eta_signals (
  signal_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  shipment_id      text NOT NULL REFERENCES shipments,
  source           text NOT NULL CHECK (source IN ('GATE_IN','OPERATIONS_OVERRIDE','TELEMATICS','LOCATION_PIN','SIM_PING','FASTAG','WAREHOUSE_ESTIMATE','DRIVER_DECLARED','ORIGINAL_PLAN')),
  observed_at      timestamptz NOT NULL,
  position_geohash text,                        -- never sent to the LLM
  derived_eta      timestamptz NOT NULL,
  uncertainty_min  integer NOT NULL DEFAULT 0,
  confidence       text NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
  flags            text[] NOT NULL DEFAULT '{}',
  raw_ref          text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_signals_shipment ON eta_signals (shipment_id, observed_at DESC);

CREATE TABLE tracking_consents (
  consent_id   uuid PRIMARY KEY,
  driver_id    text NOT NULL REFERENCES drivers,
  shipment_id  text NOT NULL REFERENCES shipments,
  scope        text NOT NULL CHECK (scope IN ('LOCATION_PIN','SIM_TRACKING')),
  granted_at   timestamptz NOT NULL,
  expires_at   timestamptz NOT NULL,
  withdrawn_at timestamptz,
  consent_text_version text NOT NULL
);

CREATE TABLE idempotency_keys (
  idem_key      text PRIMARY KEY,               -- hash(thread_id, tool, args, turn_id)
  request_hash  text NOT NULL,
  response      jsonb,
  status        text NOT NULL CHECK (status IN ('IN_PROGRESS','DONE')),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox (
  event_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  facility_id   text NOT NULL REFERENCES facilities,  -- re-review A: every facility-owned row carries facility_id (RLS)
  aggregate     text NOT NULL,
  aggregate_id  text NOT NULL,
  event_type    text NOT NULL,
  payload       jsonb NOT NULL,
  status        text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','DELIVERED','FAILED')),
  attempts      integer NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_pending ON outbox (created_at) WHERE status = 'PENDING';

-- Durable notification state machine (re-review C). One row per (source event, channel).
--   PENDING -> SENDING -> SENT -> DELIVERED          (provider accepted, then delivery webhook)
--                     \-> RETRY (attempts < max, next_retry_at) -> SENDING
--                     \-> FAILED (terminal: attempts exhausted or permanent error) -> ops alert / fallback channel
--                     \-> UNKNOWN (timeout with no provider reply: the provider MAY have accepted it).
-- UNKNOWN is never blindly re-sent: wait for the provider status webhook up to unknown_deadline, then re-send once with
-- the same reference (driver sees an identical, harmless repeat) or escalate. Claim rows with FOR UPDATE SKIP LOCKED.
-- Fourth review: "re-send once" is ENFORCED, not just documented: unknown_resend_count is 0 or 1, can never go down,
-- and trg_notification_transition allows UNKNOWN -> SENDING only while it is 0. A worker that crashes mid-send
-- leaves SENDING; reap_stuck_sends() moves it to UNKNOWN (ambiguous), never back to PENDING.
-- Delivery is at-least-once with at most one same-reference duplicate per notification. Never exactly-once.
CREATE TABLE notifications (
  notification_id      uuid PRIMARY KEY,
  facility_id          text NOT NULL REFERENCES facilities,
  source_event_id      bigint NOT NULL REFERENCES outbox,
  shipment_id          text REFERENCES shipments,
  channel              text NOT NULL CHECK (channel IN ('WHATSAPP','SMS','EMAIL','PAGE','CONSOLE')),
  recipient_ref        text NOT NULL,        -- pseudonymous reference resolved at send time; never a raw number in logs
  template_id          text NOT NULL,
  payload              jsonb NOT NULL,
  status               text NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING','SENDING','SENT','DELIVERED','RETRY','FAILED','UNKNOWN')),
  attempts             integer NOT NULL DEFAULT 0,
  max_attempts         integer NOT NULL DEFAULT 4,
  next_retry_at        timestamptz,
  sending_started_at   timestamptz,
  provider_message_id  text,
  last_error           text,
  unknown_since        timestamptz,          -- first time the send became ambiguous
  unknown_deadline     timestamptz,          -- wait for the provider webhook until here, then resend once or escalate
  unknown_resend_count smallint NOT NULL DEFAULT 0 CHECK (unknown_resend_count BETWEEN 0 AND 1),
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_event_id, channel),         -- the same event can never create two notifications on one channel
  UNIQUE (channel, provider_message_id),
  CHECK (status <> 'RETRY' OR next_retry_at IS NOT NULL),
  CHECK (status <> 'SENDING' OR sending_started_at IS NOT NULL),
  CHECK (status <> 'UNKNOWN' OR (unknown_since IS NOT NULL AND unknown_deadline IS NOT NULL)),
  CHECK (attempts <= max_attempts)
);
CREATE INDEX ix_notifications_due ON notifications (next_retry_at) WHERE status IN ('PENDING','RETRY');
CREATE INDEX ix_notifications_unknown ON notifications (unknown_deadline) WHERE status = 'UNKNOWN';
CREATE INDEX ix_notifications_sending ON notifications (sending_started_at) WHERE status = 'SENDING';

-- Allowed transitions. Anything else raises P0010. DELIVERED and FAILED are terminal.
CREATE OR REPLACE FUNCTION trg_notification_transition() RETURNS trigger LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF NEW.unknown_resend_count < OLD.unknown_resend_count THEN
    RAISE EXCEPTION 'NOTIFICATION_COUNTER_DECREASE' USING ERRCODE = 'P0010'; END IF;
  IF NEW.status = OLD.status THEN NEW.updated_at := now(); RETURN NEW; END IF;
  IF NOT (
       (OLD.status = 'PENDING' AND NEW.status IN ('SENDING','FAILED'))
    OR (OLD.status = 'SENDING' AND NEW.status IN ('SENT','DELIVERED','RETRY','FAILED','UNKNOWN'))
    OR (OLD.status = 'SENT'    AND NEW.status IN ('DELIVERED','FAILED'))
    OR (OLD.status = 'RETRY'   AND NEW.status IN ('SENDING','FAILED'))
    OR (OLD.status = 'UNKNOWN' AND NEW.status IN ('SENT','DELIVERED','FAILED'))
    OR (OLD.status = 'UNKNOWN' AND NEW.status = 'SENDING' AND OLD.unknown_resend_count = 0)
  ) THEN
    RAISE EXCEPTION 'NOTIFICATION_TRANSITION_DENIED: % -> % (unknown resends used: %)',
      OLD.status, NEW.status, OLD.unknown_resend_count USING ERRCODE = 'P0010';
  END IF;
  IF OLD.status = 'UNKNOWN' AND NEW.status = 'SENDING' THEN
    NEW.unknown_resend_count := 1; END IF;                       -- the one recovery resend is now spent
  IF NEW.status = 'SENDING' THEN NEW.sending_started_at := now(); END IF;
  IF NEW.status = 'UNKNOWN' THEN
    NEW.unknown_since    := COALESCE(OLD.unknown_since, now());
    IF NEW.unknown_deadline IS NOT DISTINCT FROM OLD.unknown_deadline THEN   -- fresh grace window on every entry
      NEW.unknown_deadline := now() + interval '2 minutes'; END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER notification_transition BEFORE UPDATE ON notifications
  FOR EACH ROW EXECUTE FUNCTION trg_notification_transition();

CREATE TABLE booking_events (                 -- append-only audit trail
  event_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at            timestamptz NOT NULL DEFAULT now(),
  principal     text NOT NULL,
  principal_role text NOT NULL,
  action        text NOT NULL,
  facility_id   text,
  shipment_id   text,
  before_state  jsonb,
  after_state   jsonb,
  reason        text,
  trace_id      text
);
CREATE OR REPLACE FUNCTION trg_append_only() RETURNS trigger LANGUAGE plpgsql SET search_path = setuhaul, public AS $$
BEGIN RAISE EXCEPTION 'booking_events is append-only'; END $$;
CREATE TRIGGER booking_events_append_only BEFORE UPDATE OR DELETE ON booking_events
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();

-- ---------------------------------------------------------------- the one write path for a hold
-- Expired holds are released lazily inside the same transaction (and by a sweeper), then the new hold is inserted.
-- Any overlap -> exclusion_violation (23P01) -> caller returns LOST + fresh options. Compat failure -> P0001.
CREATE OR REPLACE FUNCTION place_hold(p_option uuid, p_thread text, p_principal text, p_ttl interval)
RETURNS TABLE (occupancy_id bigint, expires_at timestamptz) LANGUAGE plpgsql SET search_path = setuhaul, public AS $$
DECLARE o shown_options; fac text;
BEGIN
  SELECT * INTO o FROM shown_options WHERE option_id = p_option AND thread_id = p_thread;
  IF NOT FOUND THEN RAISE EXCEPTION 'OPTION_UNKNOWN' USING ERRCODE = 'P0002'; END IF;
  IF o.expires_at < now() THEN RAISE EXCEPTION 'OPTION_EXPIRED' USING ERRCODE = 'P0003'; END IF;
  -- Serialise writers per dock. Without this, two racing inserts on an exclusion constraint can deadlock (40P01)
  -- instead of one cleanly losing (23P01). Found by validate_schema.py. The EXCLUDE constraint stays the safety net.
  PERFORM pg_advisory_xact_lock(hashtext(o.dock_id));
  SELECT facility_id INTO fac FROM docks WHERE dock_id = o.dock_id;
  IF (SELECT automation_paused FROM facilities WHERE facility_id = fac) THEN
    RAISE EXCEPTION 'AUTOMATION_PAUSED' USING ERRCODE = 'P0004'; END IF;
  -- release this shipment's previous hold and any expired holds that overlap the requested window
  UPDATE dock_occupancy d SET active = false
   WHERE d.active AND d.kind = 'HOLD'
     AND (d.shipment_id = o.shipment_id OR (d.dock_id = o.dock_id AND d.during && o.during AND d.expires_at < now()));
  RETURN QUERY
  INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, shipment_id, ref_id, expires_at, created_by)
  VALUES (o.dock_id, fac, o.during, 'HOLD', o.shipment_id, o.option_id::text, now() + p_ttl, p_principal)
  RETURNING dock_occupancy.occupancy_id, dock_occupancy.expires_at;
END $$;

-- ---------------------------------------------------------------- row-level security (re-review A)
-- Facility is the isolation boundary. Every facility-owned table has RLS ENABLED + FORCED, for reads AND writes.
-- RDS Data API resets session state between calls (DISCARD ALL), so scope is set inside the same transaction:
--   BeginTransaction -> SET LOCAL ROLE <app role>; SELECT set_config('app.facility_scope', <verified claim>, true)
--   [driver channel: also set_config('app.driver_id', <verified driver>, true)] -> queries -> Commit/Rollback.
-- Missing scope => scope_allows() is false => zero rows and no writes (fail closed).
-- No application role is SUPERUSER or BYPASSRLS. Background jobs (sweeper, reconciler, scheduler) use
-- setuhaul_system and set the scope to the facilities they own explicitly; nothing bypasses RLS.
-- Superusers and the RDS master user always bypass RLS: they are used for migrations only, never by the app.
CREATE OR REPLACE FUNCTION scope_allows(fac text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = setuhaul, public AS $$
  SELECT COALESCE(fac = ANY (string_to_array(NULLIF(current_setting('app.facility_scope', true), ''), ',')), false)
$$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_console') THEN CREATE ROLE setuhaul_console NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_agent')   THEN CREATE ROLE setuhaul_agent NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_system')  THEN CREATE ROLE setuhaul_system NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'setuhaul_rpc')     THEN CREATE ROLE setuhaul_rpc NOLOGIN; END IF;
END $$;
ALTER ROLE setuhaul_console NOSUPERUSER NOBYPASSRLS;
ALTER ROLE setuhaul_agent   NOSUPERUSER NOBYPASSRLS;
ALTER ROLE setuhaul_system  NOSUPERUSER NOBYPASSRLS;
ALTER ROLE setuhaul_rpc     NOSUPERUSER NOBYPASSRLS NOLOGIN;   -- owns the mutation functions; no member, no login

-- ---------------------------------------------------------------- least privilege (fourth review P0)
-- RLS decides WHICH rows; these grants decide WHICH operations. Principles:
--   * Nothing by default: PUBLIC loses table access and EXECUTE on every function.
--   * Reads are granted per table; approval task tokens are readable by the workflow job only (T27).
--   * Business mutations of capacity (dock_occupancy) go only through SECURITY DEFINER functions owned by
--     setuhaul_rpc. setuhaul_rpc is NOBYPASSRLS and not the table owner, so RLS still applies inside them using
--     the caller's scope. Definer functions pin search_path to (setuhaul, pg_catalog, pg_temp).
--   * Remaining direct writes are column-level and limited to what each caller owns
--     (e.g. only the console can set chat_threads.human_active, the takeover flag of eval D13).
--   * No role has DELETE. booking_events is INSERT-only for everyone.
-- Build adds further functions on the same pattern, each with its own EXECUTE grant:
--   submit_booking_request (agent), decide_approval / block_dock / set_automation_paused / move_appointment (console).
REVOKE ALL ON ALL TABLES IN SCHEMA setuhaul FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
-- Note: ALTER DEFAULT PRIVILEGES ... IN SCHEMA cannot revoke PostgreSQL's global default EXECUTE-to-PUBLIC on new
-- functions (found by validate_schema.py §7). Every migration therefore ends with the REVOKE at the bottom of this file,
-- and the validator fails if any function in the schema is executable by PUBLIC.
GRANT USAGE ON SCHEMA setuhaul TO setuhaul_console, setuhaul_agent, setuhaul_system, setuhaul_rpc;

-- reads shared by the three app roles (RLS still filters the rows)
GRANT SELECT ON carriers, drivers, vehicles, facilities, facility_config, docks, shipments, appointment_slots, dock_events,
                dock_occupancy, appointments, shown_options, escalations, chat_threads, chat_messages,
                driver_exceptions, eta_signals, tracking_consents
  TO setuhaul_console, setuhaul_agent, setuhaul_system;
GRANT SELECT (approval_id, kind, facility_id, shipment_id, proposed_action, evidence, status, expires_at,
              decided_by, decided_at, decision_note, created_at)        -- every column except task_token
  ON approval_requests TO setuhaul_console, setuhaul_agent;

-- agent tool service (driver channel)
GRANT INSERT ON chat_threads, chat_messages, driver_exceptions, eta_signals, tracking_consents, shown_options,
                escalations, booking_events, outbox TO setuhaul_agent;
GRANT UPDATE (status, shipment_id, clarification_turns, closed_at) ON chat_threads TO setuhaul_agent;
GRANT UPDATE (status, shipment_id) ON driver_exceptions TO setuhaul_agent;
GRANT UPDATE (withdrawn_at) ON tracking_consents TO setuhaul_agent;
GRANT SELECT, INSERT ON idempotency_keys TO setuhaul_agent;
GRANT UPDATE (response, status) ON idempotency_keys TO setuhaul_agent;

-- planner / operations console
GRANT SELECT ON outbox, notifications, booking_events TO setuhaul_console;
GRANT INSERT ON chat_messages, escalations, booking_events, outbox TO setuhaul_console;
GRANT UPDATE (status, assigned_to, acknowledged_at, resolved_at, resolution) ON escalations TO setuhaul_console;
GRANT UPDATE (human_active, status) ON chat_threads TO setuhaul_console;

-- background jobs: outbox relay, notification worker, expiry / reconciler, TMS / WMS mirror sync
GRANT SELECT ON outbox, notifications, booking_events, idempotency_keys, approval_requests TO setuhaul_system;
GRANT INSERT, UPDATE ON shipments, appointment_slots, dock_events TO setuhaul_system;
GRANT INSERT ON notifications, escalations, booking_events TO setuhaul_system;
GRANT UPDATE (status, attempts, next_retry_at, provider_message_id, last_error, unknown_deadline) ON notifications TO setuhaul_system;
GRANT UPDATE (status, attempts) ON outbox TO setuhaul_system;
GRANT UPDATE (status, at_risk, is_current, updated_at, row_version) ON appointments TO setuhaul_system;
GRANT UPDATE (status) ON approval_requests TO setuhaul_system;
GRANT UPDATE (status, assigned_to) ON escalations TO setuhaul_system;

-- function owner: exactly what the definer functions need
GRANT SELECT ON shown_options, shipments, docks, facilities, facility_config, dock_occupancy, notifications TO setuhaul_rpc;
GRANT INSERT ON dock_occupancy TO setuhaul_rpc;
GRANT UPDATE (active) ON dock_occupancy TO setuhaul_rpc;
GRANT UPDATE (schedule_version) ON facilities TO setuhaul_rpc;
GRANT UPDATE (status) ON notifications TO setuhaul_rpc;

-- sweeper: release expired holds (also done lazily by place_hold and by the exact EventBridge timer)
CREATE OR REPLACE FUNCTION release_expired_holds() RETURNS integer LANGUAGE sql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  WITH r AS (UPDATE dock_occupancy SET active = false
              WHERE active AND kind = 'HOLD' AND expires_at < now() RETURNING 1)
  SELECT count(*)::integer FROM r
$$;
-- crash recovery: a worker died after claiming a send. The provider may or may not have the message -> UNKNOWN.
CREATE OR REPLACE FUNCTION reap_stuck_sends(p_timeout interval) RETURNS integer LANGUAGE sql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  WITH r AS (UPDATE notifications SET status = 'UNKNOWN'
              WHERE status = 'SENDING' AND sending_started_at < now() - p_timeout RETURNING 1)
  SELECT count(*)::integer FROM r
$$;

ALTER FUNCTION place_hold(uuid, text, text, interval) SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp;
ALTER FUNCTION release_expired_holds()               SECURITY DEFINER;
ALTER FUNCTION reap_stuck_sends(interval)            SECURITY DEFINER;
ALTER FUNCTION trg_bump_schedule_version()           SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp;
ALTER FUNCTION place_hold(uuid, text, text, interval) OWNER TO setuhaul_rpc;
ALTER FUNCTION release_expired_holds()               OWNER TO setuhaul_rpc;
ALTER FUNCTION reap_stuck_sends(interval)            OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_bump_schedule_version()           OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;   -- again: new functions above defaulted to PUBLIC

GRANT EXECUTE ON FUNCTION place_hold(uuid, text, text, interval) TO setuhaul_agent, setuhaul_console;
GRANT EXECUTE ON FUNCTION release_expired_holds()               TO setuhaul_system;
GRANT EXECUTE ON FUNCTION reap_stuck_sends(interval)            TO setuhaul_system;
GRANT EXECUTE ON FUNCTION scope_allows(text) TO setuhaul_console, setuhaul_agent, setuhaul_system, setuhaul_rpc;  -- RLS calls it as the caller

-- facility-owned tables with a facility_id column
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['dock_occupancy','approval_requests','escalations','outbox','notifications','booking_events'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY p_%s_facility ON %I USING (scope_allows(facility_id)) WITH CHECK (scope_allows(facility_id))', t, t);
  END LOOP;
END $$;

-- facility-owned through the dock
ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;  ALTER TABLE appointments FORCE ROW LEVEL SECURITY;
CREATE POLICY p_appointments_facility ON appointments
  USING (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = appointments.dock_id)))
  WITH CHECK (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = appointments.dock_id)));
ALTER TABLE shown_options ENABLE ROW LEVEL SECURITY; ALTER TABLE shown_options FORCE ROW LEVEL SECURITY;
CREATE POLICY p_shown_options_facility ON shown_options
  USING (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = shown_options.dock_id)))
  WITH CHECK (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = shown_options.dock_id)));

-- driver-owned conversation data: visible to that driver's session, or to staff scoped to the shipment's facility
ALTER TABLE chat_threads ENABLE ROW LEVEL SECURITY;  ALTER TABLE chat_threads FORCE ROW LEVEL SECURITY;
CREATE POLICY p_chat_threads ON chat_threads
  USING (driver_id = current_setting('app.driver_id', true)
         OR scope_allows((SELECT destination_facility_id FROM shipments s WHERE s.shipment_id = chat_threads.shipment_id)))
  WITH CHECK (driver_id = current_setting('app.driver_id', true)
         OR scope_allows((SELECT destination_facility_id FROM shipments s WHERE s.shipment_id = chat_threads.shipment_id)));
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY; ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;
CREATE POLICY p_chat_messages ON chat_messages
  USING (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = chat_messages.thread_id))
  WITH CHECK (EXISTS (SELECT 1 FROM chat_threads t WHERE t.thread_id = chat_messages.thread_id));

-- driver-or-facility tables (third review P0-A): eta_signals, driver_exceptions, tracking_consents.
-- Driver session (app.driver_id set): only rows for that driver, and only for shipments assigned to that driver
--   in the TMS. The driver context is strict: it does not fall back to facility scope.
-- Staff / job session: only rows whose shipment's destination facility is in scope.
-- A driver_exception not yet linked to a shipment (shipment_id NULL) is visible to its driver only; staff see it
-- through the escalation it raises, which carries a facility_id.
CREATE OR REPLACE FUNCTION driver_or_staff(p_driver text, p_shipment text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = setuhaul, public AS $$
  SELECT CASE
    WHEN NULLIF(current_setting('app.driver_id', true), '') IS NOT NULL THEN
      COALESCE(p_driver = current_setting('app.driver_id', true), false)
      AND (p_shipment IS NULL OR EXISTS (SELECT 1 FROM shipments s
                                          WHERE s.shipment_id = p_shipment AND s.driver_id = p_driver))
    ELSE scope_allows((SELECT destination_facility_id FROM shipments s WHERE s.shipment_id = p_shipment))
  END
$$;
GRANT EXECUTE ON FUNCTION driver_or_staff(text, text) TO setuhaul_console, setuhaul_agent, setuhaul_system, setuhaul_rpc;

ALTER TABLE driver_exceptions ENABLE ROW LEVEL SECURITY; ALTER TABLE driver_exceptions FORCE ROW LEVEL SECURITY;
CREATE POLICY p_driver_exceptions ON driver_exceptions
  USING (driver_or_staff(driver_id, shipment_id)) WITH CHECK (driver_or_staff(driver_id, shipment_id));
ALTER TABLE tracking_consents ENABLE ROW LEVEL SECURITY; ALTER TABLE tracking_consents FORCE ROW LEVEL SECURITY;
CREATE POLICY p_tracking_consents ON tracking_consents
  USING (driver_or_staff(driver_id, shipment_id)) WITH CHECK (driver_or_staff(driver_id, shipment_id));
ALTER TABLE eta_signals ENABLE ROW LEVEL SECURITY; ALTER TABLE eta_signals FORCE ROW LEVEL SECURITY;
CREATE POLICY p_eta_signals ON eta_signals
  USING (driver_or_staff((SELECT driver_id FROM shipments s WHERE s.shipment_id = eta_signals.shipment_id), shipment_id))
  WITH CHECK (driver_or_staff((SELECT driver_id FROM shipments s WHERE s.shipment_id = eta_signals.shipment_id), shipment_id));

-- TMS / WMS mirror tables (fourth review; closes B-RLS-2). Other policies and the compat trigger look shipments up
-- under the SAME caller context, so a lookup succeeds exactly when the caller may see the shipment, and a hidden
-- shipment makes the outer check deny (fail closed). No SECURITY DEFINER lookup helper is needed, which keeps the
-- definer surface to the mutation functions above. Writes to the mirror come only from the sync job (setuhaul_system).
ALTER TABLE shipments ENABLE ROW LEVEL SECURITY; ALTER TABLE shipments FORCE ROW LEVEL SECURITY;
CREATE POLICY p_shipments ON shipments
  USING (CASE WHEN NULLIF(current_setting('app.driver_id', true), '') IS NOT NULL
              THEN driver_id = current_setting('app.driver_id', true)     -- driver session: own shipments only
              ELSE scope_allows(destination_facility_id) END)
  WITH CHECK (scope_allows(destination_facility_id));
ALTER TABLE appointment_slots ENABLE ROW LEVEL SECURITY; ALTER TABLE appointment_slots FORCE ROW LEVEL SECURITY;
CREATE POLICY p_appointment_slots ON appointment_slots
  USING (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = appointment_slots.dock_id)))
  WITH CHECK (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = appointment_slots.dock_id)));
ALTER TABLE dock_events ENABLE ROW LEVEL SECURITY; ALTER TABLE dock_events FORCE ROW LEVEL SECURITY;
CREATE POLICY p_dock_events ON dock_events
  USING (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = dock_events.dock_id)))
  WITH CHECK (scope_allows((SELECT facility_id FROM docks d WHERE d.dock_id = dock_events.dock_id)));

-- Coverage guard: every table in the schema is either RLS-forced or on the explicit reference-data allowlist.
-- validate_schema.py fails if a new table appears in neither list.

-- Last statement of every migration: no function in the schema is executable by PUBLIC.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
