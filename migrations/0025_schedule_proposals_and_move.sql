-- 0025 · Facility scheduling tool (brief p.10, optional) and the console mutation move_appointment (B-DB-5, deferred
-- until this tool existed). Forward-only; 0001–0024 untouched.
--
-- The boundary is the same as for every other booking change: the planner (setuhaul/domain/scheduling.py) PROPOSES;
-- a person applies ONE move; this database decides. A stored proposal is evidence of what was proposed, never a
-- booking: it reserves no dock time, and nothing here trusts its rows beyond identifying which move a person chose.
--
--   record_schedule_proposal()  console (planner, ops_lead): stores the service's proposal for one facility, with the
--                               sha256 fingerprint of the planner input and an expiry (60 s … 30 min). Rows are
--                               validated: every shipment is at that facility, a MOVE names the shipment's current
--                               CONFIRMED appointment exactly as it stands, and no two rows overlap on a dock.
--   move_appointment()          console (ops_lead: MOVE_CONFIRMED_PROMISE in approval_role_rules, B-WF-2 §8): applies
--                               one MOVE row of a current, unexpired proposal. Under the booking locks it re-proves
--                               everything a booking needs (compatibility, hours, holds, the truck not in a dock, the
--                               appointment unchanged since the proposal, the dock time free), replaces the appointment
--                               (old → CANCELLED, new CONFIRMED, booking_source SCHEDULING_TOOL, replaced_appointment_id),
--                               moves its claim on dock time, writes booking_events and an AppointmentMoved outbox event.
--   enqueue_notifications()     now also turns AppointmentMoved into the existing driver template booking_superseded
--                               ("Your booking was changed by the warehouse…") on the existing outbox → relay path.
--
-- Lock order (extends 0020/0021; the same order decide_booking_request and the invalidation triggers use):
--   shipments (row, SHARE) → docks (rows, SHARE, dock_id order) → booking-compat advisory (dock_id order; refuses
--   REPEATABLE READ) → per-dock writer advisory of the TARGET dock (the key place_hold takes) → approval_requests
--   (the shipment's OPEN rows) → appointments → schedule_proposal_rows → dock_occupancy.
-- Two moves to the same dock time therefore serialise on the target dock's keys; the second re-reads in a fresh
-- READ COMMITTED snapshot after the wait and is refused SLOT_TAKEN. A concurrent block or hold that takes no such key
-- still meets the ex_one_truck_per_dock exclusion constraint: exactly one of them commits (23P01 for the other).
SET search_path = setuhaul, public;

-- ================================================================ stored proposals (evidence, not bookings)
CREATE TABLE schedule_proposals (
  proposal_id        uuid PRIMARY KEY,
  facility_id        text NOT NULL REFERENCES facilities,
  rule               text NOT NULL CHECK (rule IN ('FCFS','EARLIEST_ETA','FACILITY_SCORE')),
  engine             text NOT NULL CHECK (length(engine) BETWEEN 3 AND 60),
  input_fingerprint  text NOT NULL CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
  schedule_version   bigint NOT NULL,                  -- facility schedule version the input was read at
  metrics            jsonb NOT NULL,
  created_by         text NOT NULL,
  created_role       text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at         timestamptz NOT NULL,
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '30 minutes')
);
CREATE INDEX ix_schedule_proposals_facility ON schedule_proposals (facility_id, created_at DESC);

CREATE TABLE schedule_proposal_rows (
  proposal_id             uuid NOT NULL REFERENCES schedule_proposals,
  seq                     integer NOT NULL CHECK (seq > 0),
  facility_id             text NOT NULL,
  shipment_id             text NOT NULL REFERENCES shipments,
  appointment_id          text REFERENCES appointments,
  appointment_row_version integer,
  action                  text NOT NULL CHECK (action IN ('KEEP','MOVE','PLAN')),
  dock_id                 text NOT NULL,
  start_at                timestamptz NOT NULL,
  end_at                  timestamptz NOT NULL,
  from_dock_id            text,
  from_start_at           timestamptz,
  score                   jsonb NOT NULL,
  reason                  text NOT NULL CHECK (length(reason) <= 500),
  applied_at              timestamptz,
  applied_appointment_id  text REFERENCES appointments,
  PRIMARY KEY (proposal_id, seq),
  UNIQUE (proposal_id, shipment_id),
  FOREIGN KEY (dock_id, facility_id) REFERENCES docks (dock_id, facility_id),
  CHECK (end_at > start_at),
  CHECK (action <> 'MOVE' OR (appointment_id IS NOT NULL AND appointment_row_version IS NOT NULL
                              AND from_dock_id IS NOT NULL AND from_start_at IS NOT NULL)),
  CHECK ((applied_at IS NULL) = (applied_appointment_id IS NULL)),
  CHECK (applied_at IS NULL OR action = 'MOVE')
);
CREATE INDEX ix_schedule_proposal_rows_appt ON schedule_proposal_rows (appointment_id) WHERE action = 'MOVE';

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['schedule_proposals','schedule_proposal_rows'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY p_%s_facility ON %I USING (scope_allows(facility_id)) WITH CHECK (scope_allows(facility_id))', t, t);
  END LOOP;
END $$;

-- a stored proposal is immutable; a MOVE row records once that it was applied
CREATE FUNCTION trg_schedule_proposal_immutable() RETURNS trigger LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'PROPOSAL_IMMUTABLE: proposals are never deleted' USING ERRCODE = 'P0001'; END IF;
  IF TG_TABLE_NAME = 'schedule_proposals' THEN
    RAISE EXCEPTION 'PROPOSAL_IMMUTABLE' USING ERRCODE = 'P0001'; END IF;
  IF OLD.applied_at IS NOT NULL
     OR (NEW.proposal_id, NEW.seq, NEW.facility_id, NEW.shipment_id, NEW.appointment_id, NEW.appointment_row_version,
         NEW.action, NEW.dock_id, NEW.start_at, NEW.end_at, NEW.from_dock_id, NEW.from_start_at, NEW.score, NEW.reason)
        IS DISTINCT FROM
        (OLD.proposal_id, OLD.seq, OLD.facility_id, OLD.shipment_id, OLD.appointment_id, OLD.appointment_row_version,
         OLD.action, OLD.dock_id, OLD.start_at, OLD.end_at, OLD.from_dock_id, OLD.from_start_at, OLD.score, OLD.reason) THEN
    RAISE EXCEPTION 'PROPOSAL_IMMUTABLE: only an unapplied MOVE row may be marked applied, once' USING ERRCODE = 'P0001'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER schedule_proposal_immutable BEFORE UPDATE OR DELETE ON schedule_proposals
  FOR EACH ROW EXECUTE FUNCTION trg_schedule_proposal_immutable();
CREATE TRIGGER schedule_proposal_rows_immutable BEFORE UPDATE OR DELETE ON schedule_proposal_rows
  FOR EACH ROW EXECUTE FUNCTION trg_schedule_proposal_immutable();

-- ================================================================ store a proposal (console: planner, ops_lead)
CREATE FUNCTION record_schedule_proposal(p_facility text, p_rule text, p_engine text, p_fingerprint text,
                                         p_schedule_version bigint, p_metrics jsonb, p_rows jsonb, p_ttl_s integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  ctx staff_context := staff_ctx(); role text; pid uuid := gen_random_uuid(); clk timestamptz := clock_timestamp();
  exp timestamptz; r jsonb; n integer := 0; moves integer := 0; s shipments; a appointments; act text;
BEGIN
  role := staff_role_for(ctx, ARRAY['planner','ops_lead'], 'storing a schedule proposal');
  IF p_facility IS NULL OR NOT (p_facility = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  IF p_ttl_s IS NULL OR p_ttl_s NOT BETWEEN 60 AND 1800 THEN RAISE EXCEPTION 'BAD_TTL: %', p_ttl_s USING ERRCODE = 'P0001'; END IF;
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' OR jsonb_array_length(p_rows) > 500 THEN
    RAISE EXCEPTION 'BAD_ROWS' USING ERRCODE = 'P0001'; END IF;
  exp := clk + make_interval(secs => p_ttl_s);
  INSERT INTO schedule_proposals (proposal_id, facility_id, rule, engine, input_fingerprint, schedule_version, metrics,
                                  created_by, created_role, created_at, expires_at)
  VALUES (pid, p_facility, p_rule, p_engine, p_fingerprint, p_schedule_version, coalesce(p_metrics, '{}'),
          ctx.principal, role, clk, exp);
  FOR r IN SELECT x FROM jsonb_array_elements(p_rows) x LOOP
    n := n + 1;
    act := r ->> 'action';
    SELECT * INTO s FROM shipments WHERE shipment_id = r ->> 'shipment_id';
    IF NOT FOUND OR s.destination_facility_id <> p_facility THEN
      RAISE EXCEPTION 'SHIPMENT_NOT_IN_FACILITY: %', r ->> 'shipment_id' USING ERRCODE = 'P0001'; END IF;
    a := NULL;
    IF act = 'MOVE' THEN
      SELECT * INTO a FROM appointments WHERE appointment_id = r ->> 'appointment_id';
      IF NOT FOUND OR a.shipment_id <> s.shipment_id OR a.status <> 'CONFIRMED' OR NOT a.is_current THEN
        RAISE EXCEPTION 'APPOINTMENT_NOT_MOVABLE: %', r ->> 'appointment_id' USING ERRCODE = 'P0001'; END IF;
      IF a.dock_id <> r ->> 'from_dock_id' OR lower(a.during) <> (r ->> 'from_start')::timestamptz THEN
        RAISE EXCEPTION 'APPOINTMENT_CHANGED: % changed after the proposal was computed', a.appointment_id
          USING ERRCODE = 'P0001'; END IF;
      moves := moves + 1;
    END IF;
    INSERT INTO schedule_proposal_rows (proposal_id, seq, facility_id, shipment_id, appointment_id, appointment_row_version,
                                        action, dock_id, start_at, end_at, from_dock_id, from_start_at, score, reason)
    VALUES (pid, n, p_facility, s.shipment_id, a.appointment_id, a.row_version, act, r ->> 'dock_id',
            (r ->> 'start')::timestamptz, (r ->> 'end')::timestamptz,
            CASE WHEN act = 'MOVE' THEN r ->> 'from_dock_id' END,
            CASE WHEN act = 'MOVE' THEN (r ->> 'from_start')::timestamptz END,
            coalesce(r -> 'score', '{}'), left(coalesce(r ->> 'reason', ''), 500));
  END LOOP;
  -- the planner's own invariant, re-proved: no two rows of one proposal claim the same dock time
  IF EXISTS (SELECT 1 FROM schedule_proposal_rows x JOIN schedule_proposal_rows y
                ON y.proposal_id = x.proposal_id AND x.seq < y.seq AND x.dock_id = y.dock_id
               AND x.start_at < y.end_at AND y.start_at < x.end_at
             WHERE x.proposal_id = pid) THEN
    RAISE EXCEPTION 'PROPOSAL_ROWS_OVERLAP' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, after_state)
  VALUES (ctx.principal, role, 'SCHEDULE_PROPOSAL_STORED', p_facility,
          jsonb_build_object('proposal_id', pid, 'rule', p_rule, 'input_fingerprint', p_fingerprint, 'rows', n,
                             'moves', moves, 'expires_at', exp));
  RETURN jsonb_build_object('proposal_id', pid, 'expires_at', exp, 'rows', n, 'moves', moves,
                            'input_fingerprint', p_fingerprint, 'status', 'PROPOSAL');
END $$;

-- ================================================================ apply one move (console: ops_lead)
CREATE FUNCTION move_appointment(p_proposal uuid, p_appointment text, p_idem_key text, p_note text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE
  ctx staff_context := staff_ctx();                                            -- UNTRUSTED_CONTEXT without one
  pr schedule_proposals; rw schedule_proposal_rows; a appointments; s shipments; d docks; role text; clk timestamptz;
  k text := 'move:' || p_idem_key; resp jsonb; why text; v_ship text; v_from text; v_fac text; slot tstzrange;
  new_id text; occ bigint; hit text;
BEGIN
  resp := idem_begin(k, md5(concat_ws('|', p_proposal, p_appointment, ctx.principal)));
  IF resp IS NOT NULL THEN RETURN resp; END IF;
  SELECT x.role INTO role FROM approval_role_rules x WHERE x.kind = 'MOVE_CONFIRMED_PROMISE' AND x.role = ANY (ctx.roles)
   ORDER BY array_position(ARRAY['operations','planner','ops_lead'], x.role) LIMIT 1;
  IF role IS NULL THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: moving a confirmed appointment needs one of %',
    (SELECT array_agg(x.role) FROM approval_role_rules x WHERE x.kind = 'MOVE_CONFIRMED_PROMISE') USING ERRCODE = 'P0001'; END IF;

  -- which proposal row, shipment and docks (read without a lock; every fact is re-read under the locks below)
  SELECT * INTO pr FROM schedule_proposals WHERE proposal_id = p_proposal;   -- RLS: the staff member's facilities only
  IF NOT FOUND OR NOT (pr.facility_id = ANY (ctx.facilities)) THEN
    RAISE EXCEPTION 'PROPOSAL_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO rw FROM schedule_proposal_rows WHERE proposal_id = p_proposal AND appointment_id = p_appointment;
  IF NOT FOUND OR rw.action <> 'MOVE' THEN
    RAISE EXCEPTION 'NOT_A_PROPOSED_MOVE: % is not moved by proposal %', p_appointment, p_proposal USING ERRCODE = 'P0001'; END IF;
  SELECT ap.shipment_id, ap.dock_id, dk.facility_id INTO v_ship, v_from, v_fac
    FROM appointments ap JOIN docks dk ON dk.dock_id = ap.dock_id WHERE ap.appointment_id = p_appointment;
  IF v_ship IS NULL THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF v_fac <> pr.facility_id OR rw.facility_id <> pr.facility_id THEN
    RAISE EXCEPTION 'CROSS_FACILITY: the appointment is not at the proposal''s facility' USING ERRCODE = 'P0001'; END IF;

  -- lock order: shipment → docks → compat advisory → target dock writer key → approvals → appointment → proposal row
  SELECT * INTO s FROM shipments WHERE shipment_id = v_ship FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SHIPMENT_NOT_VISIBLE' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM docks WHERE dock_id IN (v_from, rw.dock_id) ORDER BY dock_id FOR SHARE;
  PERFORM lock_booking_compat(ARRAY[v_from, rw.dock_id]);
  PERFORM pg_advisory_xact_lock(hashtext(rw.dock_id));
  PERFORM 1 FROM approval_requests WHERE shipment_id = v_ship AND status = 'OPEN' ORDER BY approval_id FOR UPDATE;
  IF FOUND THEN RAISE EXCEPTION 'REQUEST_PENDING: decide the driver''s open request first' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment FOR UPDATE;
  SELECT * INTO rw FROM schedule_proposal_rows WHERE proposal_id = p_proposal AND seq = rw.seq FOR UPDATE;
  clk := clock_timestamp();                                                    -- deadlines after the locks (§14)

  -- the proposal must still describe today's appointment
  IF pr.expires_at <= clk THEN RAISE EXCEPTION 'PROPOSAL_EXPIRED' USING ERRCODE = 'P0001'; END IF;
  IF rw.applied_at IS NOT NULL THEN RAISE EXCEPTION 'ALREADY_APPLIED: %', rw.applied_appointment_id USING ERRCODE = 'P0001'; END IF;
  -- the truck is fixed once it is in a dock or unloading: never moved
  IF a.status = 'IN_PROGRESS' THEN RAISE EXCEPTION 'APPOINTMENT_IN_PROGRESS' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = v_ship;                   -- re-read under the SHARE lock
  IF s.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
    RAISE EXCEPTION 'SHIPMENT_IN_DOCK: %', s.current_status USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dock_occupancy WHERE shipment_id = v_ship AND active AND kind = 'FIXED') THEN
    RAISE EXCEPTION 'SHIPMENT_IN_DOCK: the truck is at a dock' USING ERRCODE = 'P0001'; END IF;
  IF a.status <> 'CONFIRMED' OR NOT a.is_current OR a.row_version <> rw.appointment_row_version
     OR a.dock_id <> rw.from_dock_id OR lower(a.during) <> rw.from_start_at THEN
    RAISE EXCEPTION 'APPOINTMENT_CHANGED: the appointment changed after the proposal; compute a new one'
      USING ERRCODE = 'P0001'; END IF;

  -- every rule a booking needs, re-proved now
  slot := tstzrange(rw.start_at, rw.end_at, '[)');
  IF rw.start_at < clk THEN RAISE EXCEPTION 'SLOT_IN_PAST' USING ERRCODE = 'P0001'; END IF;
  IF booking_hold_active(pr.facility_id, rw.dock_id) THEN
    RAISE EXCEPTION 'OPERATIONAL_HOLD: bookings are on hold at this facility or dock' USING ERRCODE = 'P0001'; END IF;
  IF slot_is_after_hours(pr.facility_id, slot) THEN RAISE EXCEPTION 'OUTSIDE_HOURS' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO d FROM docks WHERE dock_id = rw.dock_id;
  why := booking_incompatibility(s, d);
  IF why IS NOT NULL THEN RAISE EXCEPTION '%', why USING ERRCODE = 'P0001'; END IF;
  -- the appointment's own claim ends first (a failure below rolls this back); expired holds in the way are released
  -- (as place_hold does); then the dock time must be free
  UPDATE dock_occupancy SET active = false
   WHERE active AND (occupancy_id = a.occupancy_id OR (kind = 'APPOINTMENT' AND ref_id = a.appointment_id));
  UPDATE dock_occupancy SET active = false
   WHERE active AND kind = 'HOLD' AND dock_id = rw.dock_id AND during && slot AND expires_at < clk;
  SELECT o.kind || ' ' || coalesce(o.ref_id, o.occupancy_id::text) INTO hit FROM dock_occupancy o
   WHERE o.active AND o.dock_id = rw.dock_id AND o.during && slot ORDER BY lower(o.during) LIMIT 1;
  IF hit IS NOT NULL THEN RAISE EXCEPTION 'SLOT_TAKEN: %', hit USING ERRCODE = 'P0001'; END IF;

  -- replace: the new claim and appointment start in the same transaction (the EXCLUDE constraint is the net)
  new_id := 'APT-' || replace(gen_random_uuid()::text, '-', '');
  INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, shipment_id, ref_id, created_by)
  VALUES (rw.dock_id, pr.facility_id, slot, 'APPOINTMENT', v_ship, new_id, ctx.principal)
  RETURNING occupancy_id INTO occ;
  UPDATE appointments SET status = 'CANCELLED', is_current = false, updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id;
  INSERT INTO appointments (appointment_id, shipment_id, dock_id, during, occupancy_id, status, is_current, booking_source,
                            replaced_appointment_id, warehouse_confirmation_ref, confirmation_basis)
  VALUES (new_id, v_ship, rw.dock_id, slot, occ, 'CONFIRMED', true, 'SCHEDULING_TOOL', a.appointment_id,
          ctx.principal, 'HUMAN');
  UPDATE schedule_proposal_rows SET applied_at = clk, applied_appointment_id = new_id
   WHERE proposal_id = p_proposal AND seq = rw.seq;
  resp := jsonb_build_object('outcome', 'MOVED', 'status', 'CONFIRMED', 'appointment_id', new_id,
                             'replaced', a.appointment_id, 'shipment_id', v_ship, 'facility_id', pr.facility_id,
                             'dock_id', rw.dock_id, 'dock_code', d.dock_code, 'start', rw.start_at, 'end', rw.end_at,
                             'from_dock_id', a.dock_id, 'from_start', lower(a.during), 'proposal_id', p_proposal,
                             'input_fingerprint', pr.input_fingerprint, 'rule', pr.rule,
                             'moved_by', ctx.principal, 'moved_by_role', role, 'moved_at', clk);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (pr.facility_id, 'appointment', new_id, 'AppointmentMoved', resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'APPOINTMENT_MOVED', pr.facility_id, v_ship, to_jsonb(a), resp, p_note);
  RETURN idem_finish(k, resp);
END $$;

-- ================================================================ the driver hears about it on the existing path
-- 0018's body, plus AppointmentMoved → the fixed template booking_superseded (setuhaul/notify/outbound.py).
CREATE OR REPLACE FUNCTION enqueue_notifications(p_limit integer DEFAULT 200) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE e record; drv text; sid text; tid text; n integer := 0; ins integer;
BEGIN
  FOR e IN SELECT * FROM pending_outbox_events('notification-enqueue',
                                               ARRAY['AgentReplyStored','ApprovalDecided','AppointmentMoved'], p_limit) LOOP
    drv := NULL; sid := NULL; tid := NULL;
    IF e.event_type = 'AgentReplyStored' THEN
      drv := e.payload ->> 'driver_id'; tid := e.payload ->> 'thread_id';
      SELECT shipment_id INTO sid FROM chat_threads WHERE thread_id = tid;
      INSERT INTO notifications (notification_id, facility_id, source_event_id, shipment_id, channel, recipient_ref,
                                 template_id, payload, chat_message_id, content_hash, thread_id)
      SELECT notification_id_for(e.event_id, 'WHATSAPP', 'driver:' || drv), o.facility_id, e.event_id, sid, 'WHATSAPP',
             'driver:' || drv, 'agent_reply',
             jsonb_build_object('message_id', (e.payload ->> 'message_id')::bigint),    -- never the text itself
             (e.payload ->> 'message_id')::bigint, e.payload ->> 'content_hash', tid
        FROM outbox o WHERE o.event_id = e.event_id
      ON CONFLICT DO NOTHING;
    ELSIF e.event_type IN ('ApprovalDecided', 'AppointmentMoved') THEN
      sid := e.payload ->> 'shipment_id';
      SELECT driver_id INTO drv FROM shipments WHERE shipment_id = sid;
      SELECT thread_id INTO tid FROM chat_threads WHERE shipment_id = sid ORDER BY opened_at DESC LIMIT 1;
      INSERT INTO notifications (notification_id, facility_id, source_event_id, shipment_id, channel, recipient_ref,
                                 template_id, payload, thread_id)
      SELECT notification_id_for(e.event_id, 'WHATSAPP', 'driver:' || drv), o.facility_id, e.event_id, sid, 'WHATSAPP',
             'driver:' || drv,
             CASE WHEN e.event_type = 'AppointmentMoved' THEN 'booking_superseded'
                  ELSE 'booking_' || lower(coalesce(e.payload ->> 'outcome', 'CONFIRMED')) END,
             jsonb_build_object('appointment_id', e.payload ->> 'appointment_id',
                                'outcome', CASE WHEN e.event_type = 'AppointmentMoved' THEN 'MOVED'
                                                ELSE coalesce(e.payload ->> 'outcome', 'CONFIRMED') END,
                                'dock_id', e.payload ->> 'dock_id', 'start', e.payload ->> 'start',
                                'still_valid_booking', e.payload ->> 'still_valid_booking'),
             tid
        FROM outbox o WHERE o.event_id = e.event_id AND drv IS NOT NULL
      ON CONFLICT DO NOTHING;
    END IF;
    GET DIAGNOSTICS ins = ROW_COUNT;
    n := n + ins;
    PERFORM consume_outbox_event('notification-enqueue', e.event_id);       -- same transaction as the insert (§17)
  END LOOP;
  RETURN n;
END $$;

-- ================================================================ ownership and grants
ALTER FUNCTION trg_schedule_proposal_immutable() OWNER TO setuhaul_rpc;
ALTER FUNCTION record_schedule_proposal(text, text, text, text, bigint, jsonb, jsonb, integer) OWNER TO setuhaul_rpc;
ALTER FUNCTION move_appointment(uuid, text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION enqueue_notifications(integer) OWNER TO setuhaul_rpc;

-- only the definer functions write proposals; the console reads its own facilities' proposals (RLS)
GRANT SELECT, INSERT ON schedule_proposals, schedule_proposal_rows TO setuhaul_rpc;
GRANT UPDATE (applied_at, applied_appointment_id) ON schedule_proposal_rows TO setuhaul_rpc;
GRANT SELECT ON schedule_proposals, schedule_proposal_rows TO setuhaul_console;
GRANT SELECT ON approval_requests TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_schedule_proposal(text, text, text, text, bigint, jsonb, jsonb, integer) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION move_appointment(uuid, text, text, text)                                    TO setuhaul_console;
GRANT EXECUTE ON FUNCTION enqueue_notifications(integer)                                              TO setuhaul_system;
