-- 0047 · Facility approval mode: MANUAL (default) or AUTO, set per facility by an authorised planner.
--
-- MANUAL keeps today's workflow unchanged: every driver booking request goes to a planner.
-- AUTO confirms a driver's selected slot at once, through the existing deterministic confirmation path (the 0016
-- policy branch of submit_booking_request: CONFIRMED appointment, BookingConfirmed outbox event, booking_events row),
-- but only in the cases the 0016 rule table already marks auto-eligible (safe-new-booking, safe-move-later) and only
-- when no mandatory approval applies. Mandatory approvals always win:
--   * PERSON_ALWAYS (safety, contradictory data) still refuses with REQUIRES_PERSON;
--   * any risk flag (after hours, after the last new start, moving earlier or into a busier time, critical or reefer
--     load, dock downgrade) or a non-STANDARD request kind still creates a planner request;
--   * a dispatcher's accepted offer is still decided by a planner (0035), whatever the mode;
--   * a rule that is not auto-eligible (the default "anything else" row) still needs a planner.
-- Everything submit_booking_request checked before is checked in the same order: thread lock, idempotency,
-- attestation, human takeover, automation pause, booking holds, shipment state, open request, hold ownership and
-- expiry, the kill switch and the EXCLUDE constraint on dock_occupancy (unchanged objects, not touched here).
--
-- The mode is read once when the driver's confirmed selection is submitted and recorded in the request's or the
-- booking's evidence (approval_mode, approval_mode_version, approval_mode_set_by, approval_mode_set_at), so a later
-- change never rewrites how an earlier request was handled. An AUTO confirmation is audited as AUTO_CONFIRM_BOOKING
-- by "approval-mode:<facility>@v<version>"; the version points at the SET_APPROVAL_MODE audit row that enabled it.
--
-- set_facility_approval_mode: trusted staff context only (0015/0019), planner or ops_lead, inside the person's facility
-- scope; optimistic concurrency on approval_mode_version; idempotent when the mode is already the requested one;
-- every change audited in booking_events (before and after). Forward-only; no existing row changes: every facility
-- starts MANUAL.
SET search_path = setuhaul, public;

-- ================================================================ the setting
ALTER TABLE facilities
  ADD COLUMN approval_mode          text NOT NULL DEFAULT 'MANUAL' CHECK (approval_mode IN ('MANUAL','AUTO')),
  ADD COLUMN approval_mode_version  integer NOT NULL DEFAULT 0 CHECK (approval_mode_version >= 0),
  ADD COLUMN approval_mode_set_by   text,
  ADD COLUMN approval_mode_set_at   timestamptz;
-- AUTO always names who switched it on, and when
ALTER TABLE facilities ADD CONSTRAINT facilities_approval_mode_named
  CHECK (approval_mode = 'MANUAL' OR (approval_mode_set_by IS NOT NULL AND approval_mode_set_at IS NOT NULL));
GRANT UPDATE (approval_mode, approval_mode_version, approval_mode_set_by, approval_mode_set_at) ON facilities TO setuhaul_rpc;

CREATE FUNCTION set_facility_approval_mode(p_facility text, p_mode text, p_expected_version integer, p_principal text,
                                           p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context := staff_ctx(); role text; f facilities; clk timestamptz := clock_timestamp();
BEGIN
  PERFORM check_principal(ctx, p_principal);
  role := staff_role_for(ctx, ARRAY['planner','ops_lead'], 'setting the facility approval mode');
  IF p_facility IS NULL OR NOT (p_facility = ANY (ctx.facilities)) THEN
    RAISE EXCEPTION 'FORBIDDEN: facility outside your scope' USING ERRCODE = 'P0001'; END IF;
  IF p_mode IS NULL OR p_mode NOT IN ('MANUAL','AUTO') THEN
    RAISE EXCEPTION 'INVALID_MODE: % (MANUAL or AUTO)', p_mode USING ERRCODE = 'P0001'; END IF;
  IF nullif(btrim(coalesce(p_reason, '')), '') IS NULL THEN
    RAISE EXCEPTION 'REASON_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO f FROM facilities WHERE facility_id = p_facility FOR UPDATE;   -- serialises concurrent changes
  IF NOT FOUND THEN RAISE EXCEPTION 'FACILITY_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF f.approval_mode = p_mode THEN                                           -- idempotent: no change, no audit row
    RETURN jsonb_build_object('facility_id', p_facility, 'approval_mode', f.approval_mode,
                              'version', f.approval_mode_version, 'set_by', f.approval_mode_set_by,
                              'set_at', f.approval_mode_set_at, 'changed', false);
  END IF;
  IF p_expected_version IS DISTINCT FROM f.approval_mode_version THEN
    RAISE EXCEPTION 'VERSION_CONFLICT: the setting changed (now version %)', f.approval_mode_version USING ERRCODE = 'P0001';
  END IF;
  UPDATE facilities SET approval_mode = p_mode, approval_mode_version = f.approval_mode_version + 1,
                        approval_mode_set_by = ctx.principal, approval_mode_set_at = clk
   WHERE facility_id = p_facility;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, reason, before_state, after_state)
  VALUES (ctx.principal, role, 'SET_APPROVAL_MODE', p_facility, p_reason,
          jsonb_build_object('approval_mode', f.approval_mode, 'version', f.approval_mode_version),
          jsonb_build_object('approval_mode', p_mode, 'version', f.approval_mode_version + 1));
  RETURN jsonb_build_object('facility_id', p_facility, 'approval_mode', p_mode, 'version', f.approval_mode_version + 1,
                            'set_by', ctx.principal, 'set_at', clk, 'changed', true);
END $$;

-- ================================================================ submit: the mode decides only the auto-eligible cases

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
  v_mode text; v_mode_ver integer; v_mode_by text; v_mode_at timestamptz; f_dispatch boolean; v_auto boolean;
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
  -- 0047: the facility's approval mode, read and recorded once, now (the driver's confirmed selection is submitted)
  SELECT approval_mode, approval_mode_version, approval_mode_set_by, approval_mode_set_at
    INTO v_mode, v_mode_ver, v_mode_by, v_mode_at FROM facilities WHERE facility_id = fac;
  -- a dispatcher's accepted offer is always decided by a planner (0035), whatever the mode
  f_dispatch := EXISTS (SELECT 1 FROM dispatch_offers d JOIN shown_options so ON so.option_id = d.option_id
                        WHERE d.shipment_id = o.shipment_id AND d.status = 'ACCEPTED' AND d.appointment_id IS NULL
                          AND so.dock_id = o.dock_id AND so.during = o.during);
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
                                 'rule_authorised_by', rule.authorised_by, 'evaluated_at', clk,
                                 'approval_mode', v_mode, 'approval_mode_version', v_mode_ver,
                                 'approval_mode_set_by', v_mode_by, 'approval_mode_set_at', v_mode_at,
                                 'dispatcher_offer', f_dispatch);
  appt := 'APT-' || replace(gen_random_uuid()::text, '-', '');

  -- 0047: AUTO confirms exactly the cases a planner rule marks auto-eligible, and only when no mandatory approval
  -- applies: a STANDARD kind, no risk flag (after hours, after the last new start, earlier, busier, critical, reefer,
  -- downgrade, safety, contradictory data) and no dispatcher offer. Everything else goes to a planner as before.
  v_auto := v_mode = 'AUTO' AND rule.outcome = 'NEEDS_PLANNER' AND rule.auto_eligible
            AND v_kind = 'STANDARD' AND NOT f_any_risk AND NOT f_dispatch;
  -- a policy confirmation needs an authorised, auto-eligible rule AND no risk flag at all, whatever the rule order
  IF (rule.outcome = 'POLICY_CONFIRM' AND rule.auto_eligible AND rule.authorised_by IS NOT NULL
      AND v_kind = 'STANDARD' AND NOT f_any_risk) OR v_auto THEN
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
                             'policy_rule', CASE WHEN aid IS NULL THEN rule.rule_id || '@v' || rule.version END,
                             'approval_mode', v_mode, 'auto_approved', v_auto);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'appointment', appt, CASE WHEN aid IS NULL THEN 'BookingConfirmed' ELSE 'BookingRequested' END,
          CASE WHEN aid IS NULL THEN resp || jsonb_build_object('evidence', evidence) ELSE resp END);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (CASE WHEN v_auto THEN 'approval-mode:' || fac || '@v' || v_mode_ver
               WHEN aid IS NULL THEN 'policy:' || rule.rule_id || '@v' || rule.version ELSE p_principal END,
          CASE WHEN aid IS NULL THEN 'policy' ELSE 'agent' END,
          CASE WHEN v_auto THEN 'AUTO_CONFIRM_BOOKING'
               WHEN aid IS NULL THEN 'POLICY_CONFIRM_BOOKING' ELSE 'SUBMIT_BOOKING_REQUEST' END,
          fac, o.shipment_id, to_jsonb(cur), CASE WHEN aid IS NULL THEN resp || jsonb_build_object('evidence', evidence) ELSE resp END,
          'driver confirmation message ' || msg);
  RETURN idem_finish(k, resp);
END $$;

-- ================================================================ ownership and grants
ALTER FUNCTION set_facility_approval_mode(text, text, integer, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION submit_booking_request(uuid, text, uuid, text, text) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION set_facility_approval_mode(text, text, integer, text, text) TO setuhaul_console;
