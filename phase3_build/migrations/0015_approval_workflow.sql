-- 0015 · B-WF-2: warehouse approval workflow (design/B-WF-2_approval_design.md, revision 3, approved 19 Nov 2026).
-- Forward-only; 0001–0014 untouched. No deployed environment has pending requests from before 0015 (test databases
-- are rebuilt on every run), so no backfill: every request created from here on has exactly one approval row.
--
-- The boundary: the agent can only ask. A request needing approval becomes CONFIRMED only when an authenticated
-- warehouse person decides inside decide_booking_request, which enforces every booking and capacity rule itself,
-- before the single deadline shared by the request and its claim on the slot. Every other outcome is final and
-- recorded. Deadlines are decided with clock_timestamp() after the row locks are held (§14). The decider comes from
-- staff_context, written only by begin_staff_context, which only setuhaul_console may execute (§15).
--
-- Lock order (§16): thread_lock → approval_requests (approval_id order) → appointments → dock_occupancy.
SET search_path = setuhaul, public;

-- ================================================================ §15 trusted staff identity
CREATE TABLE staff_context (
  txid         bigint NOT NULL,
  backend_pid  integer NOT NULL,
  principal    text NOT NULL CHECK (principal ~ '^staff:[A-Za-z0-9@._:+-]{1,120}$'),
  roles        text[] NOT NULL,
  facilities   text[] NOT NULL,
  set_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (txid, backend_pid)
);
-- No application role has any privilege here; only setuhaul_rpc (the owner of the definer functions) reads and writes.

CREATE FUNCTION begin_staff_context(p_principal text, p_roles text[], p_facilities text[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF p_roles IS NULL OR cardinality(p_roles) = 0 OR NOT (p_roles <@ ARRAY['operations','planner','ops_lead']) THEN
    RAISE EXCEPTION 'BAD_STAFF_CONTEXT: roles %', p_roles USING ERRCODE = 'P0001'; END IF;
  IF p_facilities IS NULL OR cardinality(p_facilities) = 0
     OR EXISTS (SELECT 1 FROM unnest(p_facilities) f WHERE f NOT IN (SELECT facility_id FROM facilities)) THEN
    RAISE EXCEPTION 'BAD_STAFF_CONTEXT: facilities %', p_facilities USING ERRCODE = 'P0001'; END IF;
  BEGIN
    INSERT INTO staff_context (txid, backend_pid, principal, roles, facilities)
    VALUES (txid_current(), pg_backend_pid(), p_principal, p_roles, p_facilities);
  EXCEPTION WHEN unique_violation THEN
    RAISE EXCEPTION 'BAD_STAFF_CONTEXT: already set in this transaction' USING ERRCODE = 'P0001';
  END;
  PERFORM set_config('app.facility_scope', array_to_string(p_facilities, ','), true);   -- RLS sees the same scope
END $$;

-- The identity of the current transaction, or UNTRUSTED_CONTEXT. Never reads app.* settings.
CREATE FUNCTION staff_ctx() RETURNS staff_context LANGUAGE plpgsql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE s staff_context;
BEGIN
  SELECT * INTO s FROM staff_context WHERE txid = txid_current() AND backend_pid = pg_backend_pid();
  IF NOT FOUND THEN
    RAISE EXCEPTION 'UNTRUSTED_CONTEXT: no staff identity for this transaction' USING ERRCODE = 'P0001'; END IF;
  RETURN s;
END $$;

CREATE FUNCTION staff_ctx_or_null() RETURNS staff_context LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT * FROM staff_context WHERE txid = txid_current() AND backend_pid = pg_backend_pid()
$$;

CREATE FUNCTION purge_staff_context() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN DELETE FROM staff_context WHERE set_at < clock_timestamp() - interval '1 day'; GET DIAGNOSTICS n = ROW_COUNT; RETURN n; END $$;

-- ================================================================ §8 who may decide what
CREATE TABLE approval_role_rules (
  kind  text NOT NULL,
  role  text NOT NULL CHECK (role IN ('operations','planner','ops_lead')),
  PRIMARY KEY (kind, role)
);
INSERT INTO approval_role_rules VALUES
  ('STANDARD','operations'), ('STANDARD','planner'), ('STANDARD','ops_lead'),
  ('AFTER_HOURS_START','planner'), ('AFTER_HOURS_START','ops_lead'),
  ('DOCK_DOWNGRADE','planner'), ('DOCK_DOWNGRADE','ops_lead'),
  ('MOVE_CONFIRMED_PROMISE','ops_lead'), ('PRIORITY_OVERRIDE','ops_lead'), ('SCHEDULE_PROPOSAL','ops_lead');

-- ================================================================ §11 operational / safety holds
CREATE TABLE booking_holds (
  hold_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  facility_id text NOT NULL REFERENCES facilities,
  dock_id     text REFERENCES docks,
  reason      text NOT NULL CHECK (length(reason) BETWEEN 3 AND 300),
  set_by      text NOT NULL,
  set_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  cleared_by  text,
  cleared_at  timestamptz,
  CHECK ((cleared_by IS NULL) = (cleared_at IS NULL))
);
ALTER TABLE booking_holds ENABLE ROW LEVEL SECURITY; ALTER TABLE booking_holds FORCE ROW LEVEL SECURITY;
CREATE POLICY p_booking_holds_facility ON booking_holds USING (scope_allows(facility_id)) WITH CHECK (scope_allows(facility_id));

CREATE FUNCTION booking_hold_active(p_facility text, p_dock text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM booking_holds
                  WHERE facility_id = p_facility AND cleared_at IS NULL AND (dock_id IS NULL OR dock_id = p_dock))
$$;

-- ================================================================ §10 / §19 confirmation policy (B-WF-2a fills it)
CREATE TABLE confirmation_rules (
  rule_id      text NOT NULL,
  version      integer NOT NULL CHECK (version > 0),
  facility_id  text NOT NULL REFERENCES facilities,
  priority     integer NOT NULL,
  condition    jsonb NOT NULL DEFAULT '{}',          -- keys that must equal the request's inputs: shift, kind, moves_booking
  outcome      text NOT NULL CHECK (outcome IN ('NEEDS_PLANNER','POLICY_CONFIRM')),
  created_by   text NOT NULL DEFAULT session_user,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (rule_id, version)
);
CREATE FUNCTION trg_confirmation_rules_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'APPEND_ONLY: confirmation rules are versioned; add a new version' USING ERRCODE = 'P0001'; END $$;
CREATE TRIGGER confirmation_rules_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON confirmation_rules
  FOR EACH STATEMENT EXECUTE FUNCTION trg_confirmation_rules_append_only();
-- Q1: every facility starts with one catch-all rule that needs a planner. Nothing confirms automatically.
INSERT INTO confirmation_rules (rule_id, version, facility_id, priority, condition, outcome, created_by)
SELECT 'default-' || facility_id, 1, facility_id, 1000, '{}', 'NEEDS_PLANNER', 'migration:0015' FROM facilities;

-- ...and so does every facility added later.
CREATE FUNCTION trg_default_confirmation_rule() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO confirmation_rules (rule_id, version, facility_id, priority, condition, outcome, created_by)
  VALUES ('default-' || NEW.facility_id, 1, NEW.facility_id, 1000, '{}', 'NEEDS_PLANNER', 'migration:0015')
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;
CREATE TRIGGER default_confirmation_rule AFTER INSERT ON facilities
  FOR EACH ROW EXECUTE FUNCTION trg_default_confirmation_rule();

-- The newest version of each rule for the facility, first match by priority. No match = NEEDS_PLANNER.
CREATE FUNCTION evaluate_confirmation_policy(p_facility text, p_inputs jsonb) RETURNS confirmation_rules
LANGUAGE plpgsql STABLE SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r confirmation_rules;
BEGIN
  SELECT * INTO r FROM (SELECT DISTINCT ON (rule_id) * FROM confirmation_rules WHERE facility_id = p_facility
                         ORDER BY rule_id, version DESC) latest
   WHERE p_inputs @> condition ORDER BY priority, rule_id LIMIT 1;
  IF NOT FOUND THEN
    r.rule_id := 'none'; r.version := 0; r.facility_id := p_facility; r.outcome := 'NEEDS_PLANNER'; r.condition := '{}';
  END IF;
  RETURN r;
END $$;

-- ================================================================ §17 idempotent outbox consumers
CREATE TABLE outbox_consumptions (
  consumer     text NOT NULL CHECK (consumer ~ '^[a-z0-9-]{3,40}$'),
  event_id     bigint NOT NULL REFERENCES outbox,
  consumed_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (consumer, event_id)
);
CREATE FUNCTION consume_outbox_event(p_consumer text, p_event bigint) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN
  INSERT INTO outbox_consumptions (consumer, event_id) VALUES (p_consumer, p_event) ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n = 1;                                      -- false: already consumed, do nothing
END $$;
CREATE UNIQUE INDEX ux_one_approval_decided ON outbox (aggregate_id) WHERE event_type = 'ApprovalDecided';
-- What a consumer has not processed yet (oldest first). The caller's facility scope still applies (RLS).
CREATE FUNCTION pending_outbox_events(p_consumer text, p_types text[], p_limit integer DEFAULT 100)
RETURNS TABLE (event_id bigint, event_type text, payload jsonb) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT e.event_id, e.event_type, e.payload FROM outbox e
   WHERE e.event_type = ANY (p_types)
     AND NOT EXISTS (SELECT 1 FROM outbox_consumptions x WHERE x.consumer = p_consumer AND x.event_id = e.event_id)
   ORDER BY e.event_id LIMIT p_limit
$$;

-- ================================================================ §2 approval and appointment columns
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_status_check;
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_status_check
  CHECK (status IN ('OPEN','APPROVED','REJECTED','EXPIRED','INVALIDATED','WITHDRAWN','SUPERSEDED'));
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_kind_check;
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_kind_check
  CHECK (kind IN ('STANDARD','AFTER_HOURS_START','MOVE_CONFIRMED_PROMISE','DOCK_DOWNGRADE','PRIORITY_OVERRIDE','SCHEDULE_PROPOSAL'));
ALTER TABLE approval_requests
  ADD COLUMN appointment_id       text REFERENCES appointments,
  ADD COLUMN claim_occupancy_id   bigint REFERENCES dock_occupancy,
  ADD COLUMN thread_id            text,
  ADD COLUMN outcome_reason       text,
  ADD COLUMN decided_by_type      text CHECK (decided_by_type IN ('HUMAN','SYSTEM')),
  ADD COLUMN decided_by_role      text,
  ADD COLUMN withdraw_reason_code text CHECK (withdraw_reason_code IN ('DRIVER_REQUESTED','OPERATIONS_DECISION','DATA_CORRECTION')),
  ADD COLUMN superseded_by        text REFERENCES appointments,
  ADD CONSTRAINT approval_final_is_recorded CHECK (
        (status = 'OPEN') = (decided_at IS NULL)
    AND (status = 'OPEN' OR (decided_by IS NOT NULL AND decided_by_type IS NOT NULL AND outcome_reason IS NOT NULL))
    AND (status <> 'WITHDRAWN' OR withdraw_reason_code IS NOT NULL)
    AND (status <> 'SUPERSEDED' OR superseded_by IS NOT NULL));
CREATE UNIQUE INDEX ux_open_approval_per_shipment ON approval_requests (shipment_id) WHERE status = 'OPEN';
CREATE UNIQUE INDEX ux_approval_per_appointment ON approval_requests (appointment_id) WHERE appointment_id IS NOT NULL;

ALTER TABLE appointments
  ADD COLUMN confirmation_basis  text CHECK (confirmation_basis IN ('HUMAN','POLICY')),
  ADD COLUMN policy_rule_id      text,
  ADD COLUMN policy_rule_version integer,
  ADD COLUMN policy_evidence     jsonb,
  ADD CONSTRAINT appointment_policy_evidence CHECK (
    (confirmation_basis = 'POLICY') = (policy_rule_id IS NOT NULL AND policy_rule_version IS NOT NULL AND policy_evidence IS NOT NULL));

-- §2 a final status is permanent; while OPEN only the task token may change; no row is ever deleted
CREATE FUNCTION trg_approval_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'APPROVAL_IMMUTABLE: approvals are never deleted' USING ERRCODE = 'P0001'; END IF;
  IF OLD.status <> 'OPEN' THEN
    RAISE EXCEPTION 'FINAL_STATE: approval % is %', OLD.approval_id, OLD.status USING ERRCODE = 'P0001'; END IF;
  IF (NEW.approval_id, NEW.kind, NEW.facility_id, NEW.shipment_id, NEW.appointment_id, NEW.claim_occupancy_id,
      NEW.expires_at, NEW.proposed_action, NEW.evidence, NEW.created_at, NEW.thread_id)
     IS DISTINCT FROM
     (OLD.approval_id, OLD.kind, OLD.facility_id, OLD.shipment_id, OLD.appointment_id, OLD.claim_occupancy_id,
      OLD.expires_at, OLD.proposed_action, OLD.evidence, OLD.created_at, OLD.thread_id) THEN
    RAISE EXCEPTION 'APPROVAL_IMMUTABLE: only the outcome may change' USING ERRCODE = 'P0001'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approval_transition BEFORE UPDATE OR DELETE ON approval_requests
  FOR EACH ROW EXECUTE FUNCTION trg_approval_transition();

-- §6 checked at commit: OPEN ⇔ PENDING_CONFIRMATION, and an OPEN request always has its live, matching claim.
-- Runs as setuhaul_rpc (NOBYPASSRLS): it reads with the row's own facility as scope, then restores the caller's.
CREATE FUNCTION check_approval_consistent(p_approval uuid, p_facility text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; a appointments; o dock_occupancy; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', p_facility, true);
  SELECT * INTO r FROM approval_requests WHERE approval_id = p_approval;
  IF FOUND AND r.appointment_id IS NOT NULL THEN
    SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id;
    IF (r.status = 'OPEN') <> (a.status = 'PENDING_CONFIRMATION') THEN
      RAISE EXCEPTION 'APPROVAL_INCONSISTENT: approval % is % but appointment % is %',
        r.approval_id, r.status, a.appointment_id, a.status USING ERRCODE = 'P0001'; END IF;
    IF r.status = 'OPEN' THEN
      SELECT * INTO o FROM dock_occupancy WHERE occupancy_id = r.claim_occupancy_id;
      IF NOT FOUND OR NOT o.active OR o.kind <> 'APPOINTMENT' OR o.ref_id IS DISTINCT FROM a.appointment_id
         OR o.dock_id <> a.dock_id OR NOT (o.during @> a.during) THEN           -- the claim covers the slot
        RAISE EXCEPTION 'OPEN_REQUEST_WITHOUT_CLAIM: approval % has no live claim on its slot', r.approval_id
          USING ERRCODE = 'P0001'; END IF;
    END IF;
  END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
END $$;

CREATE FUNCTION trg_check_approval_row() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM check_approval_consistent(NEW.approval_id, NEW.facility_id); RETURN NULL; END $$;
CREATE CONSTRAINT TRIGGER approval_consistent AFTER INSERT OR UPDATE ON approval_requests
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_check_approval_row();

CREATE FUNCTION trg_check_appointment_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE id uuid; fac text := (SELECT facility_id FROM docks WHERE dock_id = NEW.dock_id);
  old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', fac, true);
  SELECT approval_id INTO id FROM approval_requests WHERE appointment_id = NEW.appointment_id;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  IF id IS NOT NULL THEN PERFORM check_approval_consistent(id, fac);
  ELSIF NEW.status = 'PENDING_CONFIRMATION' THEN
    RAISE EXCEPTION 'APPROVAL_INCONSISTENT: pending appointment % has no approval request', NEW.appointment_id
      USING ERRCODE = 'P0001';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER appointment_approval_consistent AFTER INSERT OR UPDATE OF status ON appointments
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_check_appointment_approval();

CREATE FUNCTION trg_check_claim_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE id uuid; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', NEW.facility_id, true);
  FOR id IN SELECT approval_id FROM approval_requests WHERE claim_occupancy_id = NEW.occupancy_id AND status = 'OPEN' LOOP
    PERFORM check_approval_consistent(id, NEW.facility_id);
  END LOOP;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER claim_approval_consistent AFTER UPDATE ON dock_occupancy
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_check_claim_approval();

-- ================================================================ §4 the driver's YES, bound and used once
ALTER TABLE confirmation_attestations
  ADD COLUMN driver_id               text,
  ADD COLUMN shipment_id             text,
  ADD COLUMN dock_id                 text,
  ADD COLUMN slot                    tstzrange,
  ADD COLUMN schedule_version        bigint,
  ADD COLUMN consumed_by_appointment text REFERENCES appointments;

CREATE FUNCTION trg_attestation_snapshot() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  -- the inbound path runs without a facility scope; this trusted snapshot reads the option whatever its facility
  PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities), true);
  SELECT t.driver_id INTO NEW.driver_id FROM chat_threads t WHERE t.thread_id = NEW.thread_id;
  SELECT o.shipment_id, o.dock_id, o.during, o.schedule_version
    INTO NEW.shipment_id, NEW.dock_id, NEW.slot, NEW.schedule_version
    FROM shown_options o WHERE o.option_id = NEW.option_id;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  NEW.consumed_by_appointment := NULL;
  RETURN NEW;
END $$;
CREATE TRIGGER attestation_snapshot BEFORE INSERT ON confirmation_attestations
  FOR EACH ROW EXECUTE FUNCTION trg_attestation_snapshot();

CREATE OR REPLACE FUNCTION verify_attestation(p_option uuid, p_thread text, p_attestation uuid)
RETURNS bigint LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a confirmation_attestations; latest bigint; t chat_threads; o shown_options;
BEGIN
  PERFORM thread_lock(p_thread);              -- wait for any inbound message on this thread to commit first
  SELECT * INTO a FROM confirmation_attestations WHERE attestation_id = p_attestation;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  SELECT * INTO o FROM shown_options WHERE option_id = p_option;
  IF a.attestation_id IS NULL OR a.thread_id <> p_thread OR a.option_id <> p_option
     OR t.thread_id IS NULL OR o.option_id IS NULL
     OR a.driver_id IS DISTINCT FROM t.driver_id
     OR a.driver_id IS DISTINCT FROM nullif(current_setting('app.driver_id', true), '')
     OR a.shipment_id IS DISTINCT FROM t.shipment_id OR a.shipment_id IS DISTINCT FROM o.shipment_id
     OR a.dock_id IS DISTINCT FROM o.dock_id OR a.slot IS DISTINCT FROM o.during THEN
    RAISE EXCEPTION 'ATTESTATION_INVALID' USING ERRCODE = 'P0001'; END IF;
  IF a.verdict <> 'YES' THEN RAISE EXCEPTION 'NO_DRIVER_CONFIRMATION' USING ERRCODE = 'P0001'; END IF;
  IF a.selection_id <> (SELECT max(selection_id) FROM option_selections WHERE thread_id = p_thread) THEN
    RAISE EXCEPTION 'SELECTION_CHANGED' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM chat_messages m
   WHERE m.thread_id = p_thread AND m.sender_type = 'DRIVER' AND m.message_id > a.confirmation_message_id;
  IF FOUND THEN RAISE EXCEPTION 'CONFIRMATION_SUPERSEDED' USING ERRCODE = 'P0001'; END IF;
  SELECT latest_reply_message_id INTO latest FROM option_selections WHERE selection_id = a.selection_id;
  IF latest IS DISTINCT FROM a.confirmation_message_id THEN
    RAISE EXCEPTION 'CONFIRMATION_SUPERSEDED' USING ERRCODE = 'P0001'; END IF;
  IF a.consumed_by_appointment IS NOT NULL THEN
    RAISE EXCEPTION 'ATTESTATION_USED: already used for %', a.consumed_by_appointment USING ERRCODE = 'P0001'; END IF;
  IF a.expires_at < clock_timestamp() THEN RAISE EXCEPTION 'ATTESTATION_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  RETURN a.confirmation_message_id;
END $$;

-- ================================================================ §5 slot classification by the database
CREATE FUNCTION slot_is_after_hours(p_facility text, p_slot tstzrange) RETURNS boolean LANGUAGE plpgsql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE f facilities; lns time; st time; inside boolean;
BEGIN
  SELECT * INTO f FROM facilities WHERE facility_id = p_facility;
  SELECT last_new_start_local INTO lns FROM facility_config WHERE facility_id = p_facility AND active;
  st := (lower(p_slot) AT TIME ZONE f.timezone)::time;
  inside := CASE WHEN f.open_time < f.close_time THEN st >= f.open_time AND st < f.close_time
                 ELSE st >= f.open_time OR st < f.close_time END;            -- overnight windows
  RETURN NOT inside OR (lns IS NOT NULL AND st > lns);
END $$;

-- ================================================================ closing a request (every outcome but APPROVED)
-- The caller has already locked the approval row (lock order step 2). Locks the appointment, then the claim.
CREATE FUNCTION close_approval(p_approval uuid, p_status text, p_reason text, p_by text, p_by_type text, p_role text,
                               p_withdraw_code text DEFAULT NULL, p_superseded_by text DEFAULT NULL, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; a appointments; clk timestamptz := clock_timestamp(); resp jsonb; appt_status text; ev text;
BEGIN
  IF p_status NOT IN ('REJECTED','EXPIRED','INVALIDATED','WITHDRAWN','SUPERSEDED') THEN
    RAISE EXCEPTION 'BAD_OUTCOME: %', p_status USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO r FROM approval_requests WHERE approval_id = p_approval;
  IF r.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_DECIDED: %', r.status USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id FOR UPDATE;
  PERFORM 1 FROM dock_occupancy WHERE occupancy_id = r.claim_occupancy_id FOR UPDATE;
  appt_status := CASE p_status WHEN 'REJECTED' THEN 'REJECTED' WHEN 'EXPIRED' THEN 'EXPIRED' ELSE 'CANCELLED' END;
  ev := CASE p_status WHEN 'REJECTED' THEN 'BookingRejected' WHEN 'EXPIRED' THEN 'BookingExpired'
                      WHEN 'INVALIDATED' THEN 'BookingInvalidated' WHEN 'WITHDRAWN' THEN 'BookingWithdrawn'
                      ELSE 'BookingSuperseded' END;
  UPDATE dock_occupancy SET active = false WHERE occupancy_id = r.claim_occupancy_id;
  UPDATE appointments SET status = appt_status, updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id;
  UPDATE approval_requests SET status = p_status, decided_at = clk, decided_by = p_by, decided_by_type = p_by_type,
                               decided_by_role = p_role, outcome_reason = p_reason, decision_note = p_note,
                               withdraw_reason_code = p_withdraw_code, superseded_by = p_superseded_by
   WHERE approval_id = r.approval_id;
  resp := jsonb_build_object('approval_id', r.approval_id, 'appointment_id', a.appointment_id, 'shipment_id', a.shipment_id,
                             'outcome', p_status, 'status', appt_status, 'reason', p_reason, 'decided_by_type', p_by_type,
                             'decided_at', clk, 'still_valid_booking', a.replaced_appointment_id,
                             'withdraw_reason_code', p_withdraw_code, 'superseded_by', p_superseded_by);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (r.facility_id, 'appointment', a.appointment_id, ev, resp),
         (r.facility_id, 'approval', r.approval_id::text, 'ApprovalDecided', resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (p_by, p_role, 'APPROVAL_' || p_status, r.facility_id, a.shipment_id, to_jsonb(a), resp, coalesce(p_note, p_reason));
  RETURN resp;
END $$;

-- ================================================================ create: the agent asks (§3, §4, §5, §6, §10)
CREATE OR REPLACE FUNCTION submit_booking_request(p_option uuid, p_thread text, p_attestation uuid, p_idem_key text,
                                                  p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  o shown_options; t chat_threads; h dock_occupancy; cfg facility_config; cur appointments; s shipments;
  att confirmation_attestations; rule confirmation_rules;
  msg bigint; fac text; shift text; ttl integer; clk timestamptz; exp timestamptz; appt text; aid uuid; v_kind text;
  inputs jsonb; evidence jsonb; resp jsonb; k text := 'submit:' || p_idem_key;
BEGIN
  PERFORM thread_lock(p_thread);                                              -- lock order 1
  resp := idem_begin(k, md5(concat_ws('|', p_option, p_thread, p_attestation)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  msg := verify_attestation(p_option, p_thread, p_attestation);              -- bound to driver, shipment, slot (§4)
  SELECT * INTO o FROM shown_options WHERE option_id = p_option AND thread_id = p_thread;
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE: a person has taken over this conversation' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO att FROM confirmation_attestations WHERE attestation_id = p_attestation;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = o.dock_id;
  IF (SELECT automation_paused FROM facilities WHERE facility_id = fac) THEN
    RAISE EXCEPTION 'AUTOMATION_PAUSED' USING ERRCODE = 'P0001'; END IF;
  IF booking_hold_active(fac, o.dock_id) THEN
    RAISE EXCEPTION 'OPERATIONAL_HOLD: bookings at this facility or dock are on hold' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = o.shipment_id;
  IF s.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: %', s.current_status USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM approval_requests WHERE shipment_id = o.shipment_id AND status = 'OPEN';
  IF FOUND THEN RAISE EXCEPTION 'FINISH_CURRENT_BOOKING_FIRST: a request is with the warehouse' USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO cur FROM appointments                                          -- lock order 3
   WHERE shipment_id = o.shipment_id AND is_current AND status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
   FOR UPDATE;
  IF FOUND AND cur.status = 'IN_PROGRESS' THEN RAISE EXCEPTION 'SHIPMENT_IN_DOCK' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO h FROM dock_occupancy                                          -- lock order 4
   WHERE shipment_id = o.shipment_id AND kind = 'HOLD' AND active AND ref_id = o.option_id::text FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'HOLD_NOT_FOUND' USING ERRCODE = 'P0001'; END IF;
  clk := clock_timestamp();                                                    -- §14: after the locks
  IF h.expires_at <= clk THEN RAISE EXCEPTION 'HOLD_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF h.dock_id <> o.dock_id OR h.during <> o.during THEN RAISE EXCEPTION 'HOLD_MISMATCH' USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO cfg FROM facility_config WHERE facility_id = fac AND active;
  shift := facility_shift(fac, clk);
  v_kind := CASE WHEN o.needs_approval OR slot_is_after_hours(fac, o.during) THEN 'AFTER_HOURS_START' ELSE 'STANDARD' END;
  inputs := jsonb_build_object('facility', fac, 'shift', shift, 'kind', v_kind, 'dock_id', o.dock_id,
                               'start', lower(o.during), 'end', upper(o.during), 'moves_booking', cur.appointment_id IS NOT NULL,
                               'schedule_version', (SELECT schedule_version FROM facilities WHERE facility_id = fac));
  rule := evaluate_confirmation_policy(fac, inputs);
  evidence := jsonb_build_object('inputs', inputs, 'attestation_id', att.attestation_id, 'confirmation_message_id', msg,
                                 'selection_id', att.selection_id, 'classifier_version', att.classifier_version,
                                 'rule_id', rule.rule_id, 'rule_version', rule.version, 'rule_condition', rule.condition,
                                 'rule_outcome', rule.outcome, 'evaluated_at', clk);
  appt := 'APT-' || replace(gen_random_uuid()::text, '-', '');

  IF rule.outcome = 'POLICY_CONFIRM' AND v_kind = 'STANDARD' THEN
    -- §10: a deterministic policy confirmation. Never an approval row, never "APPROVED", never a person.
    IF cur.appointment_id IS NOT NULL THEN
      UPDATE appointments SET status = 'CANCELLED', is_current = false, updated_at = clk, row_version = row_version + 1
       WHERE appointment_id = cur.appointment_id;
      UPDATE dock_occupancy SET active = false WHERE occupancy_id = cur.occupancy_id;
    END IF;
    INSERT INTO appointments (appointment_id, shipment_id, dock_id, during, occupancy_id, status, is_current, booking_source,
                              replaced_appointment_id, confirmation_basis, policy_rule_id, policy_rule_version, policy_evidence)
    VALUES (appt, o.shipment_id, o.dock_id, o.during, h.occupancy_id, 'CONFIRMED', true, 'DRIVER_CHAT',
            cur.appointment_id, 'POLICY', rule.rule_id, rule.version, evidence);
  ELSE
    ttl := (cfg.warehouse_confirm_ttl_min ->> shift)::integer;
    exp := LEAST(clk + make_interval(mins => COALESCE(ttl, 0)), lower(o.during) - make_interval(mins => cfg.confirm_lead_min));
    IF ttl IS NULL OR ttl = 0 OR exp <= clk THEN
      RAISE EXCEPTION 'NO_CONFIRMER_AVAILABLE: shift %, ttl %', shift, ttl USING ERRCODE = 'P0001'; END IF;
    INSERT INTO appointments (appointment_id, shipment_id, dock_id, during, occupancy_id, status, is_current,
                              booking_source, pending_expires_at, replaced_appointment_id, needs_approval_reason)
    VALUES (appt, o.shipment_id, o.dock_id, o.during, h.occupancy_id, 'PENDING_CONFIRMATION', false, 'DRIVER_CHAT',
            exp, cur.appointment_id, CASE WHEN v_kind <> 'STANDARD' THEN v_kind END);
    aid := gen_random_uuid();
    INSERT INTO approval_requests (approval_id, kind, facility_id, shipment_id, proposed_action, evidence, status, expires_at,
                                   appointment_id, claim_occupancy_id, thread_id)
    VALUES (aid, v_kind, fac, o.shipment_id,
            jsonb_build_object('dock_id', o.dock_id, 'start', lower(o.during), 'end', upper(o.during), 'replaces', cur.appointment_id),
            evidence, 'OPEN', exp, appt, h.occupancy_id, p_thread);
  END IF;
  -- the hold becomes the request's claim (same row: the slot is never free in between); it has no expiry of its own
  UPDATE dock_occupancy SET kind = 'APPOINTMENT', expires_at = NULL, ref_id = appt WHERE occupancy_id = h.occupancy_id;
  UPDATE confirmation_attestations SET consumed_by_appointment = appt WHERE attestation_id = att.attestation_id;

  resp := jsonb_build_object('status', CASE WHEN aid IS NULL THEN 'CONFIRMED' ELSE 'PENDING' END,
                             'appointment_id', appt, 'approval_id', aid, 'kind', v_kind, 'shipment_id', o.shipment_id,
                             'dock_id', o.dock_id, 'start', lower(o.during), 'end', upper(o.during),
                             'pending_expires_at', exp, 'needs_approval', v_kind <> 'STANDARD',
                             'replaces', cur.appointment_id, 'shift', shift,
                             'basis', CASE WHEN aid IS NULL THEN 'POLICY' END,
                             'policy_rule', CASE WHEN aid IS NULL THEN rule.rule_id || '@v' || rule.version END);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'appointment', appt, CASE WHEN aid IS NULL THEN 'BookingConfirmed' ELSE 'BookingRequested' END,
          CASE WHEN aid IS NULL THEN resp || jsonb_build_object('evidence', evidence) ELSE resp END);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (CASE WHEN aid IS NULL THEN 'policy:' || rule.rule_id || '@v' || rule.version ELSE p_principal END,
          CASE WHEN aid IS NULL THEN 'policy' ELSE 'agent' END,
          CASE WHEN aid IS NULL THEN 'POLICY_CONFIRM_BOOKING' ELSE 'SUBMIT_BOOKING_REQUEST' END,
          fac, o.shipment_id, to_jsonb(cur), CASE WHEN aid IS NULL THEN resp || jsonb_build_object('evidence', evidence) ELSE resp END,
          'driver confirmation message ' || msg);
  RETURN idem_finish(k, resp);
END $$;

-- ================================================================ approve / reject: a person decides (§3, §8, §14)
DROP FUNCTION decide_booking_request(text, text, text, text, text);
CREATE FUNCTION decide_booking_request(p_appointment text, p_decision text, p_idem_key text, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  ctx staff_context := staff_ctx();                                           -- UNTRUSTED_CONTEXT without one
  r approval_requests; a appointments; old appointments; claim dock_occupancy; s shipments; cfg facility_config;
  role text; clk timestamptz; resp jsonb; k text := 'decide:' || p_idem_key;
BEGIN
  IF p_decision NOT IN ('CONFIRM','REJECT') THEN RAISE EXCEPTION 'BAD_DECISION: %', p_decision USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_appointment, p_decision, ctx.principal)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;

  SELECT * INTO r FROM approval_requests WHERE appointment_id = p_appointment FOR UPDATE;     -- lock order 2
  IF NOT FOUND OR NOT (r.facility_id = ANY (ctx.facilities)) THEN
    RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT x.role INTO role FROM approval_role_rules x WHERE x.kind = r.kind AND x.role = ANY (ctx.roles)
   ORDER BY array_position(ARRAY['operations','planner','ops_lead'], x.role) LIMIT 1;
  IF role IS NULL THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: % requests need one of %', r.kind,
    (SELECT array_agg(x.role) FROM approval_role_rules x WHERE x.kind = r.kind) USING ERRCODE = 'P0001'; END IF;
  IF r.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_DECIDED: %', r.status USING ERRCODE = 'P0001'; END IF;

  IF p_decision = 'REJECT' THEN
    IF r.expires_at <= clock_timestamp() THEN RAISE EXCEPTION 'PENDING_EXPIRED' USING ERRCODE = 'P0001'; END IF;
    resp := close_approval(r.approval_id, 'REJECTED', coalesce(p_note, 'rejected'), ctx.principal, 'HUMAN', role, NULL, NULL, p_note);
    RETURN idem_finish(k, resp || jsonb_build_object('decided_by', ctx.principal));
  END IF;

  SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id FOR UPDATE;      -- lock order 3
  SELECT * INTO old FROM appointments
   WHERE shipment_id = a.shipment_id AND is_current AND status IN ('CONFIRMED','IN_PROGRESS') FOR UPDATE;
  SELECT * INTO claim FROM dock_occupancy WHERE occupancy_id = r.claim_occupancy_id FOR UPDATE;   -- lock order 4
  clk := clock_timestamp();                                                   -- §14: the deadline, after the locks
  IF r.expires_at <= clk THEN RAISE EXCEPTION 'PENDING_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF booking_hold_active(r.facility_id, a.dock_id) THEN
    RAISE EXCEPTION 'OPERATIONAL_HOLD: approvals are on hold at this facility or dock' USING ERRCODE = 'P0001'; END IF;
  -- §5: the database re-proves the booking; the application's feasibility check was advisory
  IF claim.occupancy_id IS NULL OR NOT claim.active OR claim.kind <> 'APPOINTMENT' OR claim.ref_id <> a.appointment_id
     OR claim.dock_id <> a.dock_id OR NOT (claim.during @> a.during) THEN
    RAISE EXCEPTION 'CAPACITY_LOST' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO cfg FROM facility_config WHERE facility_id = r.facility_id AND active;
  IF lower(a.during) - make_interval(mins => cfg.confirm_lead_min) < clk THEN
    RAISE EXCEPTION 'SLOT_TOO_LATE' USING ERRCODE = 'P0001'; END IF;
  IF r.kind = 'STANDARD' AND slot_is_after_hours(r.facility_id, a.during) THEN
    RAISE EXCEPTION 'OUTSIDE_HOURS' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id;
  IF s.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: %', s.current_status USING ERRCODE = 'P0001'; END IF;
  IF old.appointment_id IS NOT NULL AND old.status = 'IN_PROGRESS' THEN
    RAISE EXCEPTION 'SHIPMENT_IN_DOCK' USING ERRCODE = 'P0001'; END IF;

  IF old.appointment_id IS NOT NULL THEN
    UPDATE appointments SET status = 'CANCELLED', is_current = false, updated_at = clk, row_version = row_version + 1
     WHERE appointment_id = old.appointment_id;
    UPDATE dock_occupancy SET active = false WHERE occupancy_id = old.occupancy_id;
  END IF;
  UPDATE appointments SET status = 'CONFIRMED', is_current = true, warehouse_confirmation_ref = ctx.principal,
                          confirmation_basis = 'HUMAN', updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id;
  UPDATE approval_requests SET status = 'APPROVED', decided_at = clk, decided_by = ctx.principal, decided_by_type = 'HUMAN',
                               decided_by_role = role, outcome_reason = coalesce(p_note, 'approved'), decision_note = p_note
   WHERE approval_id = r.approval_id;
  resp := jsonb_build_object('approval_id', r.approval_id, 'appointment_id', a.appointment_id, 'shipment_id', a.shipment_id,
                             'outcome', 'APPROVED', 'status', 'CONFIRMED', 'replaced', old.appointment_id,
                             'decided_by', ctx.principal, 'decided_by_type', 'HUMAN', 'decided_by_role', role, 'decided_at', clk);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (r.facility_id, 'appointment', a.appointment_id, 'BookingConfirmed', resp),
         (r.facility_id, 'approval', r.approval_id::text, 'ApprovalDecided', resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'APPROVAL_APPROVED', r.facility_id, a.shipment_id, to_jsonb(a), resp, p_note);
  RETURN idem_finish(k, resp);
END $$;

-- ================================================================ withdraw (§7, Q4)
CREATE FUNCTION withdraw_booking_request(p_appointment text, p_reason_code text, p_note text, p_idem_key text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); r approval_requests; role text; resp jsonb; k text := 'withdraw:' || p_idem_key;
BEGIN
  IF p_reason_code NOT IN ('DRIVER_REQUESTED','OPERATIONS_DECISION','DATA_CORRECTION') THEN
    RAISE EXCEPTION 'BAD_REASON_CODE: %', p_reason_code USING ERRCODE = 'P0001'; END IF;
  IF p_note IS NULL OR length(p_note) < 3 THEN RAISE EXCEPTION 'NOTE_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_appointment, p_reason_code, ctx.principal)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;
  SELECT * INTO r FROM approval_requests WHERE appointment_id = p_appointment FOR UPDATE;
  IF NOT FOUND OR NOT (r.facility_id = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  role := (SELECT x FROM unnest(ctx.roles) x ORDER BY array_position(ARRAY['operations','planner','ops_lead'], x) LIMIT 1);
  IF r.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_DECIDED: %', r.status USING ERRCODE = 'P0001'; END IF;
  resp := close_approval(r.approval_id, 'WITHDRAWN', p_reason_code, ctx.principal, 'HUMAN', role, p_reason_code, NULL, p_note);
  RETURN idem_finish(k, resp);
END $$;

-- ================================================================ expire (§3, §6, §14): no auto-approval, ever
CREATE OR REPLACE FUNCTION expire_pending_requests() RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; n integer := 0;
BEGIN
  FOR r IN SELECT * FROM approval_requests WHERE status = 'OPEN' AND expires_at <= clock_timestamp()
            ORDER BY approval_id FOR UPDATE SKIP LOCKED LOOP
    IF r.status <> 'OPEN' OR r.expires_at > clock_timestamp() THEN CONTINUE; END IF;
    PERFORM close_approval(r.approval_id, 'EXPIRED', 'NO_DECISION_BY_DEADLINE', 'system:expiry', 'SYSTEM', 'system');
    INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, handoff, ack_due_at)
    VALUES (gen_random_uuid(), 'PENDING_EXPIRED', 'DECIDE', r.facility_id, r.shipment_id,
            jsonb_build_object('appointment_id', r.appointment_id, 'approval_id', r.approval_id, 'expired_at', clock_timestamp(),
                               'still_valid_booking', r.proposed_action ->> 'replaces'),
            clock_timestamp() + interval '10 minutes');
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- The workflow's exact-time timeout: expires one request, and changes nothing if it is closed or not yet due.
CREATE FUNCTION expire_approval(p_approval uuid) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests;
BEGIN
  SELECT * INTO r FROM approval_requests WHERE approval_id = p_approval FOR UPDATE;
  IF NOT FOUND THEN RETURN 'UNKNOWN'; END IF;
  IF r.status <> 'OPEN' THEN RETURN r.status; END IF;
  IF r.expires_at > clock_timestamp() THEN RETURN 'NOT_DUE'; END IF;
  PERFORM close_approval(r.approval_id, 'EXPIRED', 'NO_DECISION_BY_DEADLINE', 'system:expiry', 'SYSTEM', 'system');
  INSERT INTO escalations (escalation_id, reason_code, mode, facility_id, shipment_id, handoff, ack_due_at)
  VALUES (gen_random_uuid(), 'PENDING_EXPIRED', 'DECIDE', r.facility_id, r.shipment_id,
          jsonb_build_object('appointment_id', r.appointment_id, 'approval_id', r.approval_id, 'expired_at', clock_timestamp()),
          clock_timestamp() + interval '10 minutes');
  RETURN 'EXPIRED';
END $$;

-- Step Functions keeps its token here (step 6); the agent cannot read the column (existing test).
CREATE FUNCTION record_task_token(p_approval uuid, p_token text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  UPDATE approval_requests SET task_token = p_token WHERE approval_id = p_approval AND status = 'OPEN';
  RETURN FOUND;
END $$;

-- ================================================================ invalidation (§6, D)
CREATE FUNCTION invalidate_open_requests(p_where text, p_key text, p_reason text, p_slot tstzrange DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; ctx staff_context := staff_ctx_or_null(); n integer := 0; who text; typ text; role text;
BEGIN
  who  := CASE WHEN ctx.principal IS NOT NULL THEN ctx.principal ELSE 'system:' || lower(p_reason) END;
  typ  := CASE WHEN ctx.principal IS NOT NULL THEN 'HUMAN' ELSE 'SYSTEM' END;
  role := CASE WHEN ctx.principal IS NOT NULL
               THEN (SELECT x FROM unnest(ctx.roles) x ORDER BY array_position(ARRAY['operations','planner','ops_lead'], x) DESC LIMIT 1)
               ELSE 'system' END;
  FOR r IN SELECT ar.* FROM approval_requests ar JOIN dock_occupancy o ON o.occupancy_id = ar.claim_occupancy_id
            WHERE ar.status = 'OPEN'
              AND CASE p_where WHEN 'dock' THEN o.dock_id = p_key AND (p_slot IS NULL OR o.during && p_slot)
                               WHEN 'shipment' THEN ar.shipment_id = p_key
                               WHEN 'facility_hours' THEN ar.facility_id = p_key AND ar.kind = 'STANDARD'
                                                          AND slot_is_after_hours(ar.facility_id, o.during)
                               ELSE false END
            ORDER BY ar.approval_id FOR UPDATE OF ar LOOP
    PERFORM close_approval(r.approval_id, 'INVALIDATED', p_reason, who, typ, role);
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION block_dock(p_event_id text, p_dock text, p_from timestamptz, p_to timestamptz,
                                      p_type text, p_reason text, p_principal text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); fac text; occ bigint;
BEGIN
  IF p_type NOT IN ('MAINTENANCE','BREAKDOWN','MANUAL_BLOCK') THEN
    RAISE EXCEPTION 'BAD_BLOCK_TYPE: %', p_type USING ERRCODE = 'P0001'; END IF;
  IF p_principal IS DISTINCT FROM ctx.principal THEN
    RAISE EXCEPTION 'PRINCIPAL_MISMATCH: the audit principal comes from the staff identity' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = p_dock;
  IF NOT FOUND OR NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dock_events WHERE dock_event_id = p_event_id) THEN       -- idempotent by event id
    SELECT occupancy_id INTO occ FROM dock_occupancy WHERE kind = 'BLOCK' AND ref_id = p_event_id;
    RETURN occ;
  END IF;
  -- D: pending requests whose claim the block covers are invalidated first, in this transaction (lock order 2 → 4).
  -- Confirmed bookings still make the insert fail (23P01): the console must move them first, never silently.
  PERFORM invalidate_open_requests('dock', p_dock, 'CAPACITY_LOST', tstzrange(p_from, p_to, '[)'));
  INSERT INTO dock_events (dock_event_id, dock_id, event_type, during, reason, created_by)
  VALUES (p_event_id, p_dock, p_type, tstzrange(p_from, p_to, '[)'), p_reason, ctx.principal);
  INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, ref_id, created_by)
  VALUES (p_dock, fac, tstzrange(p_from, p_to, '[)'), 'BLOCK', p_event_id, ctx.principal)
  RETURNING occupancy_id INTO occ;
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'dock', p_dock, 'CapacityChanged', jsonb_build_object('dock_event_id', p_event_id, 'type', p_type));
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason)
  VALUES (ctx.principal, 'operations', 'BLOCK_DOCK', fac, p_reason);
  RETURN occ;
END $$;

-- data changes that make a pending request impossible invalidate it in the same transaction
CREATE FUNCTION trg_invalidate_on_shipment() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  IF NEW.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') OR NEW.vehicle_id IS DISTINCT FROM OLD.vehicle_id THEN
    PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities), true);
    PERFORM invalidate_open_requests('shipment', NEW.shipment_id, 'SHIPMENT_CHANGED');
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER invalidate_on_shipment AFTER UPDATE OF current_status, vehicle_id ON shipments
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_on_shipment();

CREATE FUNCTION trg_invalidate_on_dock() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', NEW.facility_id, true);
  PERFORM invalidate_open_requests('dock', NEW.dock_id, 'DOCK_UNAVAILABLE');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;
CREATE TRIGGER invalidate_on_dock AFTER UPDATE OF dock_type, supports_refrigerated, max_vehicle_weight_kg ON docks
  FOR EACH ROW WHEN ((NEW.dock_type, NEW.supports_refrigerated, NEW.max_vehicle_weight_kg)
                     IS DISTINCT FROM (OLD.dock_type, OLD.supports_refrigerated, OLD.max_vehicle_weight_kg))
  EXECUTE FUNCTION trg_invalidate_on_dock();

CREATE FUNCTION trg_invalidate_on_hours() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', NEW.facility_id, true);
  PERFORM invalidate_open_requests('facility_hours', NEW.facility_id, 'OUTSIDE_HOURS');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;
CREATE TRIGGER invalidate_on_hours AFTER UPDATE OF open_time, close_time, timezone ON facilities
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_on_hours();
CREATE TRIGGER invalidate_on_config_hours AFTER INSERT OR UPDATE OF last_new_start_local, active ON facility_config
  FOR EACH ROW EXECUTE FUNCTION trg_invalidate_on_hours();

-- §7 supersession: a newer confirmed booking for the same shipment replaces the open request
CREATE FUNCTION trg_supersede_on_new_booking() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r approval_requests; ctx staff_context := staff_ctx_or_null(); old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', (SELECT facility_id FROM docks WHERE dock_id = NEW.dock_id), true);
  FOR r IN SELECT * FROM approval_requests WHERE shipment_id = NEW.shipment_id AND status = 'OPEN'
              AND appointment_id <> NEW.appointment_id ORDER BY approval_id FOR UPDATE LOOP
    PERFORM close_approval(r.approval_id, 'SUPERSEDED', 'REPLACED_BY_NEWER_BOOKING',
                           coalesce(ctx.principal, 'system:supersede'), CASE WHEN ctx.principal IS NULL THEN 'SYSTEM' ELSE 'HUMAN' END,
                           CASE WHEN ctx.principal IS NULL THEN 'system' ELSE ctx.roles[1] END, NULL, NEW.appointment_id);
  END LOOP;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;
CREATE TRIGGER supersede_on_new_booking AFTER INSERT OR UPDATE OF status, is_current ON appointments
  FOR EACH ROW WHEN (NEW.is_current AND NEW.status = 'CONFIRMED')
  EXECUTE FUNCTION trg_supersede_on_new_booking();

-- ================================================================ holds (§11, Q3)
CREATE FUNCTION set_booking_hold(p_facility text, p_dock text, p_reason text) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); id bigint;
BEGIN
  IF NOT (p_facility = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  IF NOT ('ops_lead' = ANY (ctx.roles)) THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: holds need ops_lead' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO booking_holds (facility_id, dock_id, reason, set_by) VALUES (p_facility, p_dock, p_reason, ctx.principal)
  RETURNING hold_id INTO id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason)
  VALUES (ctx.principal, 'ops_lead', 'SET_BOOKING_HOLD', p_facility, p_reason);
  RETURN id;
END $$;

CREATE FUNCTION clear_booking_hold(p_hold bigint, p_note text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); h booking_holds;
BEGIN
  SELECT * INTO h FROM booking_holds WHERE hold_id = p_hold FOR UPDATE;
  IF NOT FOUND OR NOT (h.facility_id = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'HOLD_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF NOT ('ops_lead' = ANY (ctx.roles)) THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: holds need ops_lead' USING ERRCODE = 'P0001'; END IF;
  IF h.cleared_at IS NOT NULL THEN RETURN false; END IF;
  UPDATE booking_holds SET cleared_by = ctx.principal, cleared_at = clock_timestamp() WHERE hold_id = p_hold;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason)
  VALUES (ctx.principal, 'ops_lead', 'CLEAR_BOOKING_HOLD', h.facility_id, p_note);
  RETURN true;
END $$;

-- ================================================================ ownership and grants
DO $$ DECLARE f text; BEGIN
  FOREACH f IN ARRAY ARRAY[
    'begin_staff_context(text,text[],text[])', 'staff_ctx()', 'staff_ctx_or_null()', 'purge_staff_context()',
    'trg_default_confirmation_rule()',
    'booking_hold_active(text,text)', 'evaluate_confirmation_policy(text,jsonb)', 'consume_outbox_event(text,bigint)',
    'pending_outbox_events(text,text[],integer)',
    'check_approval_consistent(uuid,text)', 'trg_check_approval_row()', 'trg_check_appointment_approval()',
    'trg_check_claim_approval()', 'trg_attestation_snapshot()', 'verify_attestation(uuid,text,uuid)',
    'slot_is_after_hours(text,tstzrange)', 'close_approval(uuid,text,text,text,text,text,text,text,text)',
    'submit_booking_request(uuid,text,uuid,text,text)', 'decide_booking_request(text,text,text,text)',
    'withdraw_booking_request(text,text,text,text)', 'expire_pending_requests()', 'expire_approval(uuid)',
    'record_task_token(uuid,text)', 'invalidate_open_requests(text,text,text,tstzrange)',
    'block_dock(text,text,timestamptz,timestamptz,text,text,text)', 'trg_invalidate_on_shipment()',
    'trg_invalidate_on_dock()', 'trg_invalidate_on_hours()', 'trg_supersede_on_new_booking()',
    'set_booking_hold(text,text,text)', 'clear_booking_hold(bigint,text)'] LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO setuhaul_rpc', f);
  END LOOP;
END $$;

GRANT SELECT, INSERT, DELETE ON staff_context TO setuhaul_rpc;
GRANT INSERT ON confirmation_rules TO setuhaul_rpc;
GRANT SELECT ON approval_role_rules, confirmation_rules, shipments, docks, facilities, facility_config,
                confirmation_attestations, option_selections, shown_options TO setuhaul_rpc;
GRANT SELECT, INSERT ON booking_holds TO setuhaul_rpc;
GRANT UPDATE (cleared_by, cleared_at) ON booking_holds TO setuhaul_rpc;
GRANT SELECT, INSERT ON outbox_consumptions TO setuhaul_rpc;
GRANT SELECT ON outbox TO setuhaul_rpc;
GRANT SELECT, INSERT ON approval_requests TO setuhaul_rpc;
GRANT UPDATE (status, decided_at, decided_by, decided_by_type, decided_by_role, outcome_reason, decision_note,
              withdraw_reason_code, superseded_by, task_token) ON approval_requests TO setuhaul_rpc;
GRANT UPDATE (status, is_current, updated_at, row_version, warehouse_confirmation_ref, confirmation_basis) ON appointments TO setuhaul_rpc;
GRANT UPDATE (active, kind, expires_at, ref_id) ON dock_occupancy TO setuhaul_rpc;
GRANT UPDATE (consumed_by_appointment) ON confirmation_attestations TO setuhaul_rpc;

-- 0001 let the system role set approval_requests.status directly; outcomes now go only through the functions above
REVOKE UPDATE (status) ON approval_requests FROM setuhaul_system;

-- the console sees the new outcome columns and the holds; it writes nothing directly
GRANT SELECT (appointment_id, thread_id, outcome_reason, decided_by_type, decided_by_role, withdraw_reason_code, superseded_by)
  ON approval_requests TO setuhaul_console;
GRANT SELECT ON booking_holds TO setuhaul_console;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION begin_staff_context(text, text[], text[])         TO setuhaul_console;
GRANT EXECUTE ON FUNCTION decide_booking_request(text, text, text, text)    TO setuhaul_console;
GRANT EXECUTE ON FUNCTION withdraw_booking_request(text, text, text, text)  TO setuhaul_console;
GRANT EXECUTE ON FUNCTION set_booking_hold(text, text, text)                TO setuhaul_console;
GRANT EXECUTE ON FUNCTION clear_booking_hold(bigint, text)                  TO setuhaul_console;
GRANT EXECUTE ON FUNCTION expire_approval(uuid)                             TO setuhaul_system;
GRANT EXECUTE ON FUNCTION record_task_token(uuid, text)                     TO setuhaul_system;
GRANT EXECUTE ON FUNCTION consume_outbox_event(text, bigint)                TO setuhaul_system;
GRANT EXECUTE ON FUNCTION pending_outbox_events(text, text[], integer)    TO setuhaul_system;
GRANT EXECUTE ON FUNCTION purge_staff_context()                             TO setuhaul_system;
