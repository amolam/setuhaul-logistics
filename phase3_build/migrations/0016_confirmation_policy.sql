-- 0016 · B-WF-2a: risk-based confirmation policy (backlog B-WF-2a; owner decisions 17 Nov 2026 and Q1, 19 Nov 2026).
-- Forward-only; 0001–0015 untouched. Builds on the agreed B-WF-2 boundary (design/B-WF-2_approval_design.md §10, §19).
--
-- The single per-shift switch was retired in 0015. Here the per-facility rule table gets its real rows:
--   person always   safety, contradictory data              → PERSON_ALWAYS: no request is created; a person takes over
--   planner         after last new start, after hours, dock downgrade, moving a confirmed booking earlier or into a
--                   busier time, critical or reefer loads    → NEEDS_PLANNER with a planner-level request kind
--   safe cases      a plain new booking; a move later        → NEEDS_PLANNER for now, marked auto_eligible
-- Q1: every row ships NEEDS_PLANNER or PERSON_ALWAYS. A row can become POLICY_CONFIRM only as a new version that is
-- auto_eligible and names its authorisation (authorised_by, a decision-register reference), and even then submit
-- confirms only when no risk flag is set. The backlog line "start GGN night shift on automatic" is NOT applied: it
-- needs the owner's explicit authorisation under Q1 (FINDINGS_LEDGER.md).
-- The risk inputs are computed by the database inside submit_booking_request from its own rows.
SET search_path = setuhaul, public;

-- ================================================================ rule table columns
ALTER TABLE confirmation_rules
  ADD COLUMN approval_kind  text NOT NULL DEFAULT 'STANDARD',
  ADD COLUMN auto_eligible  boolean NOT NULL DEFAULT false,
  ADD COLUMN rationale      text,
  ADD COLUMN authorised_by  text;
ALTER TABLE confirmation_rules DROP CONSTRAINT confirmation_rules_outcome_check;
ALTER TABLE confirmation_rules ADD CONSTRAINT confirmation_rules_outcome_check
  CHECK (outcome IN ('NEEDS_PLANNER','POLICY_CONFIRM','PERSON_ALWAYS'));
ALTER TABLE confirmation_rules ADD CONSTRAINT confirmation_rules_policy_needs_authorisation
  CHECK (outcome <> 'POLICY_CONFIRM' OR (auto_eligible AND authorised_by IS NOT NULL AND approval_kind = 'STANDARD'));
ALTER TABLE confirmation_rules ADD CONSTRAINT confirmation_rules_kind_check
  CHECK (approval_kind IN ('STANDARD','AFTER_HOURS_START','DOCK_DOWNGRADE','PLANNER_REVIEW'));

-- a planner-level kind for the planner rows that are not after-hours or a downgrade
ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_kind_check;
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_kind_check
  CHECK (kind IN ('STANDARD','AFTER_HOURS_START','MOVE_CONFIRMED_PROMISE','DOCK_DOWNGRADE','PRIORITY_OVERRIDE',
                  'SCHEDULE_PROPOSAL','PLANNER_REVIEW'));
INSERT INTO approval_role_rules VALUES ('PLANNER_REVIEW','planner'), ('PLANNER_REVIEW','ops_lead');

-- ================================================================ the rule rows (version 1, per facility)
CREATE FUNCTION install_confirmation_rules(p_facility text) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n integer;
BEGIN
  INSERT INTO confirmation_rules (rule_id, version, facility_id, priority, condition, outcome, approval_kind,
                                  auto_eligible, rationale, created_by)
  SELECT p_facility || ':' || r.id, 1, p_facility, r.prio, r.cond::jsonb, r.outcome, r.kind, r.auto, r.why, 'migration:0016'
    FROM (VALUES
      ('person-safety',              10, '{"safety": true}',               'PERSON_ALWAYS', 'STANDARD',          false, 'Safety concern open for this shipment: a person always'),
      ('person-contradictory-data',  11, '{"contradictory_data": true}',   'PERSON_ALWAYS', 'STANDARD',          false, 'Contradictory data open for this shipment: a person always'),
      ('planner-after-last-new-start',100,'{"after_last_new_start": true}', 'NEEDS_PLANNER', 'AFTER_HOURS_START', false, 'Starts after the facility''s last new start'),
      ('planner-after-hours',       101, '{"kind": "AFTER_HOURS_START"}',  'NEEDS_PLANNER', 'AFTER_HOURS_START', false, 'Outside operating hours'),
      ('planner-dock-downgrade',    102, '{"dock_downgrade": true}',       'NEEDS_PLANNER', 'DOCK_DOWNGRADE',    false, 'Moves to a lower-capability dock'),
      ('planner-move-earlier',      103, '{"move_earlier": true}',         'NEEDS_PLANNER', 'PLANNER_REVIEW',    false, 'Moves a confirmed booking earlier'),
      ('planner-busier-time',       104, '{"into_busier_time": true}',     'NEEDS_PLANNER', 'PLANNER_REVIEW',    false, 'Moves a booking into a busier time'),
      ('planner-critical-load',     105, '{"critical_load": true}',        'NEEDS_PLANNER', 'PLANNER_REVIEW',    false, 'Critical-priority load'),
      ('planner-reefer-load',       106, '{"reefer_load": true}',          'NEEDS_PLANNER', 'PLANNER_REVIEW',    false, 'Temperature-controlled load'),
      ('safe-new-booking',          500, '{"kind": "STANDARD", "moves_booking": false}', 'NEEDS_PLANNER', 'STANDARD', true,
                                         'Same facility, normal hours, compatible dock, free slot, no other booking moves'),
      ('safe-move-later',           501, '{"kind": "STANDARD", "move_later": true}', 'NEEDS_PLANNER', 'STANDARD', true,
                                         'Moves a booking later, not into a busier time')
    ) AS r(id, prio, cond, outcome, kind, auto, why)
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  INSERT INTO confirmation_rules (rule_id, version, facility_id, priority, condition, outcome, created_by, rationale)
  VALUES ('default-' || p_facility, 1, p_facility, 1000, '{}', 'NEEDS_PLANNER', 'migration:0016', 'Anything else')
  ON CONFLICT DO NOTHING;
  RETURN n;
END $$;
SELECT install_confirmation_rules(facility_id) FROM facilities;

CREATE OR REPLACE FUNCTION trg_default_confirmation_rule() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN PERFORM install_confirmation_rules(NEW.facility_id); RETURN NULL; END $$;

CREATE OR REPLACE FUNCTION evaluate_confirmation_policy(p_facility text, p_inputs jsonb) RETURNS confirmation_rules
LANGUAGE plpgsql STABLE SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r confirmation_rules;
BEGIN
  SELECT * INTO r FROM (SELECT DISTINCT ON (rule_id) * FROM confirmation_rules WHERE facility_id = p_facility
                         ORDER BY rule_id, version DESC) latest
   WHERE p_inputs @> condition ORDER BY priority, rule_id LIMIT 1;
  IF NOT FOUND THEN
    r.rule_id := 'none'; r.version := 0; r.facility_id := p_facility; r.outcome := 'NEEDS_PLANNER'; r.condition := '{}';
    r.approval_kind := 'STANDARD'; r.auto_eligible := false;
  END IF;
  RETURN r;
END $$;

-- ================================================================ submit with the risk inputs
CREATE OR REPLACE FUNCTION submit_booking_request(p_option uuid, p_thread text, p_attestation uuid, p_idem_key text,
                                                  p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  o shown_options; t chat_threads; h dock_occupancy; cfg facility_config; cur appointments; s shipments;
  att confirmation_attestations; rule confirmation_rules;
  msg bigint; fac text; shift text; ttl integer; clk timestamptz; exp timestamptz; appt text; aid uuid; v_kind text;
  inputs jsonb; evidence jsonb; resp jsonb; k text := 'submit:' || p_idem_key;
  ftz text; local_start time; load_new integer; load_old integer; cur_rank integer; new_rank integer;
  f_after_hours boolean; f_after_lns boolean; f_moves boolean; f_earlier boolean; f_later boolean; f_busier boolean;
  f_critical boolean; f_reefer boolean; f_downgrade boolean; f_safety boolean; f_contra boolean; f_any_risk boolean;
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
  -- B-WF-2a: the risk inputs, computed by the database from its own rows (never from the agent or the model)
  SELECT timezone INTO ftz FROM facilities WHERE facility_id = fac;
  local_start   := (lower(o.during) AT TIME ZONE ftz)::time;
  f_after_hours := o.needs_approval OR slot_is_after_hours(fac, o.during);
  f_after_lns   := cfg.last_new_start_local IS NOT NULL AND local_start > cfg.last_new_start_local;
  f_moves       := cur.appointment_id IS NOT NULL;
  f_earlier     := f_moves AND lower(o.during) < lower(cur.during);
  f_later       := f_moves AND lower(o.during) > lower(cur.during);
  SELECT count(*) INTO load_new FROM dock_occupancy x
   WHERE x.facility_id = fac AND x.active AND x.during && o.during AND x.shipment_id IS DISTINCT FROM o.shipment_id;
  IF f_moves THEN
    SELECT count(*) INTO load_old FROM dock_occupancy x
     WHERE x.facility_id = fac AND x.active AND x.during && cur.during AND x.shipment_id IS DISTINCT FROM o.shipment_id;
  END IF;
  f_busier      := f_moves AND load_new > coalesce(load_old, 0);
  f_critical    := s.priority_code = 'CRITICAL';
  f_reefer      := s.temperature_control_required;
  cur_rank := (SELECT CASE dock_type WHEN 'STANDARD' THEN 1 ELSE 2 END FROM docks WHERE dock_id = cur.dock_id);
  new_rank := (SELECT CASE dock_type WHEN 'STANDARD' THEN 1 ELSE 2 END FROM docks WHERE dock_id = o.dock_id);
  f_downgrade   := f_moves AND new_rank < cur_rank;
  f_safety      := EXISTS (SELECT 1 FROM escalations e WHERE e.shipment_id = o.shipment_id AND e.reason_code = 'SAFETY'
                             AND e.status IN ('OPEN','ACKNOWLEDGED'));
  f_contra      := EXISTS (SELECT 1 FROM escalations e WHERE e.shipment_id = o.shipment_id AND e.reason_code = 'CONTRADICTORY_DATA'
                             AND e.status IN ('OPEN','ACKNOWLEDGED'));
  f_any_risk    := f_after_hours OR f_after_lns OR f_earlier OR f_busier OR f_critical OR f_reefer OR f_downgrade
                   OR f_safety OR f_contra;
  inputs := jsonb_build_object('facility', fac, 'shift', shift,
                               'kind', CASE WHEN f_after_hours THEN 'AFTER_HOURS_START' ELSE 'STANDARD' END,
                               'dock_id', o.dock_id, 'start', lower(o.during), 'end', upper(o.during),
                               'moves_booking', f_moves, 'move_earlier', f_earlier, 'move_later', f_later,
                               'into_busier_time', f_busier, 'load_new', load_new, 'load_old', load_old,
                               'after_last_new_start', f_after_lns, 'critical_load', f_critical, 'reefer_load', f_reefer,
                               'dock_downgrade', f_downgrade, 'safety', f_safety, 'contradictory_data', f_contra,
                               'schedule_version', (SELECT schedule_version FROM facilities WHERE facility_id = fac));
  rule := evaluate_confirmation_policy(fac, inputs);
  -- person always: whatever the rule order, safety and contradictory data never become a request
  IF rule.outcome = 'PERSON_ALWAYS' OR f_safety OR f_contra THEN
    RAISE EXCEPTION 'REQUIRES_PERSON: rule %', CASE WHEN rule.outcome = 'PERSON_ALWAYS' THEN rule.rule_id
                                                     WHEN f_safety THEN 'safety' ELSE 'contradictory-data' END
      USING ERRCODE = 'P0001'; END IF;
  -- the request's kind (and so who may decide it) comes from the rule; an after-hours slot is never STANDARD
  v_kind := coalesce(rule.approval_kind, 'STANDARD');
  IF f_after_hours AND v_kind = 'STANDARD' THEN v_kind := 'AFTER_HOURS_START'; END IF;
  IF (f_earlier OR f_busier OR f_critical OR f_reefer) AND v_kind = 'STANDARD' THEN v_kind := 'PLANNER_REVIEW'; END IF;
  IF f_downgrade AND v_kind IN ('STANDARD','PLANNER_REVIEW') THEN v_kind := 'DOCK_DOWNGRADE'; END IF;
  evidence := jsonb_build_object('inputs', inputs, 'attestation_id', att.attestation_id, 'confirmation_message_id', msg,
                                 'selection_id', att.selection_id, 'classifier_version', att.classifier_version,
                                 'rule_id', rule.rule_id, 'rule_version', rule.version, 'rule_condition', rule.condition,
                                 'rule_outcome', rule.outcome, 'rule_auto_eligible', rule.auto_eligible,
                                 'rule_authorised_by', rule.authorised_by, 'evaluated_at', clk);
  appt := 'APT-' || replace(gen_random_uuid()::text, '-', '');

  -- a policy confirmation needs an authorised, auto-eligible rule AND no risk flag at all, whatever the rule order
  IF rule.outcome = 'POLICY_CONFIRM' AND rule.auto_eligible AND rule.authorised_by IS NOT NULL
     AND v_kind = 'STANDARD' AND NOT f_any_risk THEN
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

-- ================================================================ override-rate metric (backlog: "track planner override rate")
-- How people decided the requests each rule produced. For an auto-eligible rule, the override rate (rejected out of
-- approved + rejected) is the evidence the owner needs before authorising it to confirm automatically.
CREATE VIEW policy_rule_outcomes WITH (security_invoker = true) AS
SELECT r.facility_id,
       r.evidence ->> 'rule_id'                                  AS rule_id,
       (r.evidence ->> 'rule_version')::integer                  AS rule_version,
       coalesce((r.evidence ->> 'rule_auto_eligible')::boolean, false) AS auto_eligible,
       count(*)                                                  AS requests,
       count(*) FILTER (WHERE r.status = 'APPROVED')             AS approved,
       count(*) FILTER (WHERE r.status = 'REJECTED')             AS rejected,
       count(*) FILTER (WHERE r.status = 'EXPIRED')              AS expired,
       count(*) FILTER (WHERE r.status IN ('INVALIDATED','WITHDRAWN','SUPERSEDED')) AS closed_otherwise,
       count(*) FILTER (WHERE r.status = 'OPEN')                 AS open,
       round(count(*) FILTER (WHERE r.status = 'REJECTED')::numeric
             / nullif(count(*) FILTER (WHERE r.status IN ('APPROVED','REJECTED')), 0), 3) AS override_rate
  FROM approval_requests r
 WHERE r.evidence ? 'rule_id'
 GROUP BY 1, 2, 3, 4;
GRANT SELECT ON policy_rule_outcomes TO setuhaul_console, setuhaul_system;

ALTER FUNCTION install_confirmation_rules(text) OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_default_confirmation_rule() OWNER TO setuhaul_rpc;
ALTER FUNCTION evaluate_confirmation_policy(text, jsonb) OWNER TO setuhaul_rpc;
ALTER FUNCTION submit_booking_request(uuid, text, uuid, text, text) OWNER TO setuhaul_rpc;
GRANT SELECT ON escalations TO setuhaul_rpc;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
