-- 0048 · 27 Nov fix plan: the appointment lifecycle (gate, dock, completed, automatic no-show), the no-show
-- backfill, dispatcher reschedule requests for confirmed bookings, free drivers, an explicit offer target, and the
-- small reads the ops_lead Controls tab needs. Forward-only; 0001–0047 untouched.
--
-- Owner decisions (27 Nov 2026, docs/FIX_PLAN_27NOV.md):
--   D1  operations and ops_lead record gate arrival, at dock and completed. The dispatcher and the planner do not.
--   D2  no-show is automatic: no gate arrival by slot start + the facility's no_show_grace_min (Q4: per facility).
--   D3  after a no-show the shipment waits for the dispatcher (Rebook or Request cancellation); it is never put back
--       into "Needs booking" automatically, and no driver or dispatcher booking can start until the dispatcher decides.
--   Q1  the historical backlog is cleared in one go, but only by the explicit, audited backfill below, after its
--       dry-run report; the minute job handles appointments whose deadline falls after this migration was applied.
--   Q2  a dispatcher's time change always goes to a planner (PLANNER_REVIEW), whatever the confirmation rules say.
--
-- What does NOT change: submit_booking_request, decide_booking_request, close_approval, the confirmation policy, the
-- approval roles, the facility approval mode (0047), the kill switch and the evidence guard. A reschedule request is
-- an ordinary booking commitment: the kill switch and the evidence guard see it exactly as a driver's request.
-- Lifecycle steps are not booking commitments (no new claim on dock time: claims are only released), so they keep
-- working while booking is off, the same as a cancellation.
--
-- Concurrency: every lifecycle step and the no-show job lock the appointment row (FOR UPDATE; the job with SKIP
-- LOCKED) and then re-check its state, and the job's UPDATE is itself conditional (still CONFIRMED, still no arrival).
-- Gate arrival and no-show therefore cannot both win: whichever commits first, the other sees the new state.
-- The grace boundary is one function used by both: arrival is accepted while now <= start + grace; the no-show is due
-- only when now > start + grace.
SET search_path = setuhaul, public;

-- ================================================================ 1. lifecycle columns
ALTER TABLE appointments
  ADD COLUMN arrived_at          timestamptz,
  ADD COLUMN arrived_by          text,
  ADD COLUMN docked_at           timestamptz,
  ADD COLUMN docked_by           text,
  ADD COLUMN completed_at        timestamptz,
  ADD COLUMN completed_by        text,
  ADD COLUMN no_show_at          timestamptz,
  ADD COLUMN no_show_basis       text CHECK (no_show_basis IN ('AUTOMATIC','BACKFILL')),
  ADD COLUMN no_show_resolution  text CHECK (no_show_resolution IN ('REBOOK','CANCELLATION_REQUESTED','CANCELLED')),
  ADD COLUMN no_show_resolved_by text,
  ADD COLUMN no_show_resolved_at timestamptz,
  ADD CONSTRAINT appointment_lifecycle_order CHECK (
        (docked_at IS NULL OR arrived_at IS NOT NULL)
    AND (completed_at IS NULL OR docked_at IS NOT NULL)
    AND (no_show_basis IS NULL OR (no_show_at IS NOT NULL AND arrived_at IS NULL AND status = 'NO_SHOW'))
    AND (no_show_resolution IS NULL OR status = 'NO_SHOW'));
-- D3: the unresolved no-shows a dispatcher must decide
CREATE INDEX ix_appointments_no_show_open ON appointments (shipment_id) WHERE status = 'NO_SHOW' AND no_show_resolution IS NULL;
CREATE INDEX ix_appointments_confirmed_start ON appointments (lower(during)) WHERE status = 'CONFIRMED' AND is_current;

-- a dispatcher's reschedule request is its own booking source
ALTER TABLE appointments DROP CONSTRAINT appointments_booking_source_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_booking_source_check
  CHECK (booking_source IN ('PLANNER','DRIVER_CHAT','WAREHOUSE','SCHEDULING_TOOL','MANUAL_OVERRIDE','DISPATCHER'));

-- When the automatic no-show began. Appointments whose deadline passed BEFORE this are history: only the audited
-- backfill (dry run first) may mark them. One row, set when 0048 is applied, never changed.
CREATE TABLE lifecycle_cutover (
  id          smallint PRIMARY KEY CHECK (id = 1),
  started_at  timestamptz NOT NULL
);
INSERT INTO lifecycle_cutover VALUES (1, clock_timestamp());
ALTER TABLE lifecycle_cutover ENABLE ROW LEVEL SECURITY; ALTER TABLE lifecycle_cutover FORCE ROW LEVEL SECURITY;
CREATE POLICY p_lifecycle_cutover ON lifecycle_cutover USING (true) WITH CHECK (false);
CREATE FUNCTION trg_lifecycle_cutover_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'LIFECYCLE_CUTOVER_FIXED' USING ERRCODE = 'P0001'; END $$;
CREATE TRIGGER lifecycle_cutover_fixed BEFORE INSERT OR UPDATE OR DELETE ON lifecycle_cutover
  FOR EACH ROW EXECUTE FUNCTION trg_lifecycle_cutover_fixed();

-- One row per backfill run (dry runs included): who, when, what was found, what was changed. Append-only.
CREATE TABLE no_show_backfill_runs (
  run_id        uuid PRIMARY KEY,
  dry_run       boolean NOT NULL,
  principal     text NOT NULL,
  reason        text NOT NULL CHECK (length(reason) BETWEEN 5 AND 300),
  cutover_at    timestamptz NOT NULL,
  started_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  eligible      jsonb NOT NULL,             -- [{appointment_id, shipment_id, facility_id, start, deadline}]
  excluded      jsonb NOT NULL,             -- [{appointment_id, reason}]
  transitioned  integer NOT NULL CHECK (transitioned >= 0),
  CHECK (NOT dry_run OR transitioned = 0)
);
ALTER TABLE no_show_backfill_runs ENABLE ROW LEVEL SECURITY; ALTER TABLE no_show_backfill_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY p_no_show_backfill_runs ON no_show_backfill_runs USING (true) WITH CHECK (current_user = 'setuhaul_rpc');
CREATE TRIGGER no_show_backfill_runs_append_only BEFORE UPDATE OR DELETE ON no_show_backfill_runs
  FOR EACH ROW EXECUTE FUNCTION trg_append_only();

-- ================================================================ 2. the grace boundary (one definition)
-- Arrival is accepted while p_now <= start + grace; the no-show is due only when p_now > start + grace.
CREATE FUNCTION no_show_deadline(p_start timestamptz, p_grace_min integer) RETURNS timestamptz LANGUAGE sql IMMUTABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT p_start + make_interval(mins => greatest(coalesce(p_grace_min, 0), 0))
$$;
CREATE FUNCTION no_show_due(p_start timestamptz, p_grace_min integer, p_now timestamptz) RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT p_now > no_show_deadline(p_start, p_grace_min)
$$;

-- The facility's own grace (facility_config.no_show_grace_min, the active row). Never a shared default.
CREATE FUNCTION facility_no_show_grace(p_facility text) RETURNS integer LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT no_show_grace_min FROM facility_config WHERE facility_id = p_facility AND active
$$;

-- Why an appointment may NOT become a no-show (NULL = it may). The same rule for the minute job and the backfill:
-- only a current CONFIRMED appointment with no arrival, no dock activity, no completion and no cancellation (open or
-- approved), whose shipment the TMS does not already show at the gate, in a dock, completed or cancelled, and with no
-- open time-change request (that request is decided first; the planner sees it).
CREATE FUNCTION no_show_ineligibility(a appointments) RETURNS text LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT CASE
    WHEN a.status <> 'CONFIRMED' THEN 'STATUS_' || a.status
    WHEN NOT a.is_current THEN 'NOT_CURRENT'
    WHEN a.arrived_at IS NOT NULL THEN 'ARRIVED'
    WHEN a.docked_at IS NOT NULL THEN 'DOCK_ACTIVITY'
    WHEN a.completed_at IS NOT NULL THEN 'COMPLETED'
    WHEN EXISTS (SELECT 1 FROM cancellation_requests c WHERE c.appointment_id = a.appointment_id
                  AND c.status IN ('OPEN','APPROVED')) THEN 'CANCELLATION'
    WHEN (SELECT s.current_status FROM shipments s WHERE s.shipment_id = a.shipment_id)
         IN ('AT_GATE','WAITING','IN_DOCK','COMPLETED','CANCELLED')
      THEN 'SHIPMENT_' || (SELECT s.current_status FROM shipments s WHERE s.shipment_id = a.shipment_id)
    WHEN EXISTS (SELECT 1 FROM approval_requests r WHERE r.shipment_id = a.shipment_id AND r.status = 'OPEN')
      THEN 'TIME_CHANGE_PENDING'
    ELSE NULL END
$$;

-- D3: an unresolved no-show blocks every new booking path for the shipment until the dispatcher decides (Rebook), and
-- while a cancellation after it waits for a planner. Reads with every facility in scope (the caller may be a driver
-- session or CENTRAL), then restores the caller's.
CREATE FUNCTION no_show_unresolved(p_shipment text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r boolean; old_scope text := current_setting('app.facility_scope', true);
  old_driver text := current_setting('app.driver_id', true);
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  PERFORM set_config('app.driver_id', '', true);
  SELECT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = p_shipment AND a.status = 'NO_SHOW'
                  AND a.is_current AND (a.no_show_resolution IS NULL
                                        OR a.no_show_resolution = 'CANCELLATION_REQUESTED')) INTO r;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  PERFORM set_config('app.driver_id', coalesce(old_driver, ''), true);
  RETURN r;
END $$;

CREATE FUNCTION trg_no_show_blocks_booking() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF NEW.shipment_id IS NOT NULL AND (TG_TABLE_NAME <> 'dock_occupancy' OR NEW.kind = 'HOLD')
     AND no_show_unresolved(NEW.shipment_id) THEN
    RAISE EXCEPTION 'NO_SHOW_UNRESOLVED: the dispatcher decides first (Rebook or Request cancellation)'
      USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER no_show_blocks_hold BEFORE INSERT ON dock_occupancy
  FOR EACH ROW EXECUTE FUNCTION trg_no_show_blocks_booking();
CREATE TRIGGER no_show_blocks_offer BEFORE INSERT ON dispatch_offers
  FOR EACH ROW EXECUTE FUNCTION trg_no_show_blocks_booking();

-- ================================================================ 3. lifecycle steps (console: operations, ops_lead)
-- Shared step: the appointment in the caller's facility scope, locked. The caller has already checked the staff
-- identity and the role (D1).
CREATE FUNCTION lifecycle_lock(p_appointment text, ctx staff_context) RETURNS appointments
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a appointments; fac text;
BEGIN
  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment FOR UPDATE;        -- staff scope (RLS)
  IF NOT FOUND THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  RETURN a;
END $$;

CREATE FUNCTION lifecycle_json(a appointments, p_replayed boolean) RETURNS jsonb LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT jsonb_build_object('appointment_id', a.appointment_id, 'shipment_id', a.shipment_id, 'status', a.status,
                            'arrived_at', a.arrived_at, 'docked_at', a.docked_at, 'completed_at', a.completed_at,
                            'no_show_at', a.no_show_at, 'replayed', p_replayed)
$$;

CREATE FUNCTION record_gate_arrival(p_appointment text, p_principal text, p_note text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; fac text; clk timestamptz; grace integer; s shipments;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'recording a gate arrival');
  PERFORM check_principal(ctx, p_principal);
  a := lifecycle_lock(p_appointment, ctx);
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF a.arrived_at IS NOT NULL THEN RETURN lifecycle_json(a, true); END IF;           -- idempotent: the same answer
  IF a.status = 'NO_SHOW' THEN
    RAISE EXCEPTION 'ALREADY_NO_SHOW: the grace period had passed; the dispatcher decides (Rebook)' USING ERRCODE = 'P0001'; END IF;
  IF a.status <> 'CONFIRMED' OR NOT a.is_current THEN
    RAISE EXCEPTION 'NOT_ARRIVABLE: appointment is %', a.status USING ERRCODE = 'P0001'; END IF;
  clk := clock_timestamp();                                                           -- after the lock
  grace := facility_no_show_grace(fac);
  IF no_show_due(lower(a.during), grace, clk) THEN
    RAISE EXCEPTION 'GRACE_PASSED: arrival after start + % min; the dispatcher rebooks', grace USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id FOR UPDATE;
  IF s.current_status IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_ARRIVABLE: %', s.current_status USING ERRCODE = 'P0001'; END IF;
  -- the truck came for this booking: a time change still waiting for a planner no longer applies
  PERFORM invalidate_open_requests('shipment', a.shipment_id, 'SHIPMENT_CHANGED');
  UPDATE appointments SET arrived_at = clk, arrived_by = ctx.principal, updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id RETURNING * INTO a;
  UPDATE shipments SET current_status = 'AT_GATE', updated_at = clk WHERE shipment_id = a.shipment_id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'GATE_ARRIVAL_RECORDED', fac, a.shipment_id,
          jsonb_build_object('appointment_status', 'CONFIRMED', 'shipment_status', s.current_status),
          jsonb_build_object('appointment_id', a.appointment_id, 'arrived_at', clk, 'shipment_status', 'AT_GATE',
                             'deadline', no_show_deadline(lower(a.during), grace), 'grace_min', grace), p_note);
  RETURN lifecycle_json(a, false);
END $$;

CREATE FUNCTION record_dock_start(p_appointment text, p_principal text, p_note text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; fac text; clk timestamptz;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'recording a truck at dock');
  PERFORM check_principal(ctx, p_principal);
  a := lifecycle_lock(p_appointment, ctx);
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF a.docked_at IS NOT NULL THEN RETURN lifecycle_json(a, true); END IF;
  IF a.status <> 'CONFIRMED' OR NOT a.is_current THEN
    RAISE EXCEPTION 'NOT_DOCKABLE: appointment is %', a.status USING ERRCODE = 'P0001'; END IF;
  IF a.arrived_at IS NULL THEN RAISE EXCEPTION 'NOT_ARRIVED: record the gate arrival first' USING ERRCODE = 'P0001'; END IF;
  clk := clock_timestamp();
  PERFORM 1 FROM shipments WHERE shipment_id = a.shipment_id FOR UPDATE;
  UPDATE appointments SET status = 'IN_PROGRESS', docked_at = clk, docked_by = ctx.principal, updated_at = clk,
                          row_version = row_version + 1
   WHERE appointment_id = a.appointment_id RETURNING * INTO a;
  UPDATE shipments SET current_status = 'IN_DOCK', updated_at = clk WHERE shipment_id = a.shipment_id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'DOCK_START_RECORDED', fac, a.shipment_id,
          jsonb_build_object('appointment_status', 'CONFIRMED', 'shipment_status', 'AT_GATE'),
          jsonb_build_object('appointment_id', a.appointment_id, 'docked_at', clk, 'appointment_status', 'IN_PROGRESS',
                             'shipment_status', 'IN_DOCK'), p_note);
  RETURN lifecycle_json(a, false);
END $$;

CREATE FUNCTION record_completion(p_appointment text, p_principal text, p_note text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; fac text; clk timestamptz; s shipments; released boolean;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'recording a completion');
  PERFORM check_principal(ctx, p_principal);
  a := lifecycle_lock(p_appointment, ctx);
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF a.completed_at IS NOT NULL THEN RETURN lifecycle_json(a, true); END IF;
  IF a.status <> 'IN_PROGRESS' THEN
    RAISE EXCEPTION 'NOT_IN_DOCK: appointment is %; record at dock first', a.status USING ERRCODE = 'P0001'; END IF;
  clk := clock_timestamp();
  -- the rest of the dock time is released: the claim is ended (a release, never a new commitment, so it works while
  -- booking is off); the appointment keeps its booked window as the record
  UPDATE dock_occupancy SET active = false WHERE occupancy_id = a.occupancy_id AND active;
  released := FOUND;
  UPDATE appointments SET status = 'COMPLETED', completed_at = clk, completed_by = ctx.principal, updated_at = clk,
                          row_version = row_version + 1
   WHERE appointment_id = a.appointment_id RETURNING * INTO a;
  UPDATE shipments SET current_status = 'COMPLETED', updated_at = clk WHERE shipment_id = a.shipment_id
  RETURNING * INTO s;
  UPDATE thread_flow SET state = 'CLOSED', appointment_id = NULL, version = version + 1, updated_at = now()
   WHERE appointment_id = a.appointment_id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'COMPLETION_RECORDED', fac, a.shipment_id,
          jsonb_build_object('appointment_status', 'IN_PROGRESS', 'shipment_status', 'IN_DOCK'),
          jsonb_build_object('appointment_id', a.appointment_id, 'completed_at', clk, 'appointment_status', 'COMPLETED',
                             'shipment_status', 'COMPLETED', 'dock_time_released', released,
                             'released_from', CASE WHEN clk < upper(a.during) THEN clk END), p_note);
  PERFORM notify_driver(s.driver_id, fac,
    'Unloading complete for order ' || s.order_reference || '. Thank you. You are free for your next trip.');
  RETURN lifecycle_json(a, false);
END $$;

-- ================================================================ 4. automatic no-show (system: the recovery worker)
-- One appointment, already locked by the caller and re-checked here. Returns true when it became NO_SHOW.
CREATE FUNCTION mark_no_show(p_appointment text, p_basis text, p_principal text, p_batch uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE a appointments; fac text; grace integer; clk timestamptz := clock_timestamp(); s shipments; k text; why text;
BEGIN
  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  grace := facility_no_show_grace(fac);
  why := no_show_ineligibility(a);
  IF why IS NOT NULL OR grace IS NULL OR NOT no_show_due(lower(a.during), grace, clk) THEN RETURN false; END IF;
  -- conditional transition: still CONFIRMED and still no arrival, whatever the snapshot said
  UPDATE appointments SET status = 'NO_SHOW', no_show_at = clk, no_show_basis = p_basis, updated_at = clk,
                          row_version = row_version + 1
   WHERE appointment_id = a.appointment_id AND status = 'CONFIRMED' AND arrived_at IS NULL;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE dock_occupancy SET active = false WHERE occupancy_id = a.occupancy_id AND active;   -- dock time released
  UPDATE thread_flow SET state = 'CLOSED', appointment_id = NULL, version = version + 1, updated_at = now()
   WHERE appointment_id = a.appointment_id;
  SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id;
  SELECT dock_code INTO k FROM docks WHERE dock_id = a.dock_id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (p_principal, 'system', CASE WHEN p_basis = 'BACKFILL' THEN 'NO_SHOW_BACKFILLED' ELSE 'NO_SHOW_RECORDED' END,
          fac, a.shipment_id, jsonb_build_object('appointment_status', 'CONFIRMED'),
          jsonb_build_object('appointment_id', a.appointment_id, 'start', lower(a.during), 'grace_min', grace,
                             'deadline', no_show_deadline(lower(a.during), grace), 'no_show_at', clk,
                             'dock_time_released', true, 'basis', p_basis, 'batch', p_batch,
                             'next', 'dispatcher decides: Rebook or Request cancellation'),
          'no gate arrival by start + grace');
  -- the driver is told; the dispatcher sees it in the Dispatch tab (No-show filter). A backfilled one is history:
  -- its driver is not messaged about a slot that passed before the lifecycle existed.
  IF p_basis = 'AUTOMATIC' THEN
    PERFORM notify_driver(s.driver_id, fac,
      'You did not reach the gate for order ' || s.order_reference || ' (dock ' || k || ', '
      || to_char(lower(a.during) AT TIME ZONE (SELECT timezone FROM facilities WHERE facility_id = fac),
                 'FMHH12:MI AM "on" FMDD Mon')
      || ') within the grace period, so the slot was released. Your dispatcher will arrange the next step.');
  END IF;
  RETURN true;
END $$;

-- The minute job. Only appointments whose deadline falls after the cutover (history goes through the backfill).
-- SKIP LOCKED: an appointment a person is recording an arrival for right now is left for the next minute.
CREATE FUNCTION process_no_shows(p_limit integer DEFAULT 200) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r record; n integer := 0; seen integer := 0; cut timestamptz; old_scope text := current_setting('app.facility_scope', true);
  batch uuid := gen_random_uuid();
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT started_at INTO cut FROM lifecycle_cutover WHERE id = 1;
  FOR r IN SELECT a.appointment_id FROM appointments a JOIN docks d ON d.dock_id = a.dock_id
            JOIN facility_config c ON c.facility_id = d.facility_id AND c.active
           WHERE a.status = 'CONFIRMED' AND a.is_current AND a.arrived_at IS NULL
             AND no_show_due(lower(a.during), c.no_show_grace_min, clock_timestamp())
             AND no_show_deadline(lower(a.during), c.no_show_grace_min) > cut
           ORDER BY lower(a.during), a.appointment_id
           LIMIT greatest(least(coalesce(p_limit, 200), 1000), 1)
           FOR UPDATE OF a SKIP LOCKED LOOP
    seen := seen + 1;
    IF mark_no_show(r.appointment_id, 'AUTOMATIC', 'system:no-show', batch) THEN n := n + 1; END IF;
  END LOOP;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN jsonb_build_object('checked', seen, 'no_shows', n, 'batch', CASE WHEN n > 0 THEN batch END);
END $$;

-- ================================================================ 5. historical backfill (owner-run, dry run first)
-- Every current CONFIRMED appointment whose deadline passed BEFORE the cutover, each with the eligibility rule above.
-- p_dry_run = true: records the report and changes nothing. p_dry_run = false: needs the run id of a dry run whose
-- eligible list is exactly what is eligible now (so the owner applies what they reviewed); transitions those, records
-- the run. Idempotent: a second apply finds nothing eligible (they are NO_SHOW) and transitions 0.
CREATE FUNCTION backfill_no_shows(p_dry_run boolean, p_principal text, p_reason text, p_reviewed_run uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE cut timestamptz; elig jsonb := '[]'; excl jsonb := '[]'; r record; why text; n integer := 0; rid uuid := gen_random_uuid();
  reviewed no_show_backfill_runs; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  IF p_principal IS NULL OR p_principal !~ '^(owner|staff|system):' THEN
    RAISE EXCEPTION 'BAD_PRINCIPAL: owner:<name>, staff:<sub> or system:<job>' USING ERRCODE = 'P0001'; END IF;
  IF p_reason IS NULL OR length(p_reason) NOT BETWEEN 5 AND 300 THEN RAISE EXCEPTION 'REASON_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('setuhaul.no_show_backfill', 0));      -- one backfill at a time
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT started_at INTO cut FROM lifecycle_cutover WHERE id = 1;
  FOR r IN SELECT a.*, d.facility_id AS fac, c.no_show_grace_min AS grace
             FROM appointments a JOIN docks d ON d.dock_id = a.dock_id
             JOIN facility_config c ON c.facility_id = d.facility_id AND c.active
            WHERE a.status = 'CONFIRMED' AND a.is_current
              AND no_show_deadline(lower(a.during), c.no_show_grace_min) <= cut
            ORDER BY lower(a.during), a.appointment_id
            FOR UPDATE OF a LOOP
    why := no_show_ineligibility((SELECT x FROM appointments x WHERE x.appointment_id = r.appointment_id));
    IF why IS NULL THEN
      elig := elig || jsonb_build_object('appointment_id', r.appointment_id, 'shipment_id', r.shipment_id,
                                         'facility_id', r.fac, 'start', lower(r.during),
                                         'deadline', no_show_deadline(lower(r.during), r.grace), 'grace_min', r.grace);
    ELSE
      excl := excl || jsonb_build_object('appointment_id', r.appointment_id, 'shipment_id', r.shipment_id, 'reason', why);
    END IF;
  END LOOP;
  IF NOT p_dry_run THEN
    IF p_reviewed_run IS NULL THEN RAISE EXCEPTION 'DRY_RUN_REQUIRED: pass the run id of the reviewed dry run' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO reviewed FROM no_show_backfill_runs WHERE run_id = p_reviewed_run AND dry_run;
    IF NOT FOUND THEN RAISE EXCEPTION 'DRY_RUN_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
    IF (SELECT coalesce(jsonb_agg(x ->> 'appointment_id' ORDER BY x ->> 'appointment_id'), '[]') FROM jsonb_array_elements(elig) x)
       IS DISTINCT FROM
       (SELECT coalesce(jsonb_agg(x ->> 'appointment_id' ORDER BY x ->> 'appointment_id'), '[]') FROM jsonb_array_elements(reviewed.eligible) x)
       AND jsonb_array_length(elig) > 0 THEN
      RAISE EXCEPTION 'DRY_RUN_STALE: the eligible set changed since the reviewed dry run; run a new dry run'
        USING ERRCODE = 'P0001'; END IF;
    FOR r IN SELECT x ->> 'appointment_id' AS id FROM jsonb_array_elements(elig) x LOOP
      IF mark_no_show(r.id, 'BACKFILL', p_principal, rid) THEN n := n + 1; END IF;
    END LOOP;
  END IF;
  INSERT INTO no_show_backfill_runs (run_id, dry_run, principal, reason, cutover_at, eligible, excluded, transitioned)
  VALUES (rid, p_dry_run, p_principal, p_reason, cut, elig, excl, n);
  INSERT INTO booking_events (principal, principal_role, action, after_state, reason)
  VALUES (p_principal, 'system', CASE WHEN p_dry_run THEN 'NO_SHOW_BACKFILL_DRY_RUN' ELSE 'NO_SHOW_BACKFILL_APPLIED' END,
          jsonb_build_object('run_id', rid, 'eligible', jsonb_array_length(elig), 'excluded', jsonb_array_length(excl),
                             'transitioned', n, 'cutover_at', cut, 'reviewed_run', p_reviewed_run), p_reason);
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN jsonb_build_object('run_id', rid, 'dry_run', p_dry_run, 'cutover_at', cut, 'eligible', elig, 'excluded', excl,
                            'transitioned', n);
END $$;

-- ================================================================ 6. after a no-show: the dispatcher decides (D3)
-- REBOOK: the shipment is back in "Needs booking" (the no-show stays in the record). A planner still approves the new
-- booking as today. Request cancellation goes through request_cancellation (below) and a planner's decision.
CREATE FUNCTION resolve_no_show(p_appointment text, p_decision text, p_principal text, p_note text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; fac text; clk timestamptz := clock_timestamp();
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'deciding after a no-show');
  PERFORM check_principal(ctx, p_principal);
  IF p_decision IS DISTINCT FROM 'REBOOK' THEN RAISE EXCEPTION 'BAD_DECISION: REBOOK (or request a cancellation)' USING ERRCODE = 'P0001'; END IF;
  IF p_note IS NULL OR length(p_note) NOT BETWEEN 3 AND 300 THEN RAISE EXCEPTION 'NOTE_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF a.status <> 'NO_SHOW' THEN RAISE EXCEPTION 'NOT_A_NO_SHOW: appointment is %', a.status USING ERRCODE = 'P0001'; END IF;
  IF a.no_show_resolution = 'REBOOK' THEN
    RETURN jsonb_build_object('appointment_id', a.appointment_id, 'resolution', 'REBOOK', 'replayed', true); END IF;
  IF a.no_show_resolution IS NOT NULL THEN
    RAISE EXCEPTION 'ALREADY_RESOLVED: %', a.no_show_resolution USING ERRCODE = 'P0001'; END IF;
  UPDATE appointments SET no_show_resolution = 'REBOOK', no_show_resolved_by = ctx.principal, no_show_resolved_at = clk,
                          is_current = false, updated_at = clk, row_version = row_version + 1
   WHERE appointment_id = a.appointment_id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'NO_SHOW_REBOOK', fac, a.shipment_id, jsonb_build_object('appointment_status', 'NO_SHOW'),
          jsonb_build_object('appointment_id', a.appointment_id, 'resolution', 'REBOOK',
                             'next', 'shipment back in Needs booking; a planner approves the new booking'), p_note);
  RETURN jsonb_build_object('appointment_id', a.appointment_id, 'resolution', 'REBOOK', 'replayed', false);
END $$;

-- request_cancellation (0037 body) also accepts an unresolved NO_SHOW appointment; decide_cancellation (0036 body)
-- approves it without a capacity release (already released) and rejects it back to "waiting for the dispatcher".
CREATE OR REPLACE FUNCTION request_cancellation(p_appointment text, p_reason text, p_note text, p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; fac text; r cancellation_requests; rid uuid := gen_random_uuid();
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'requesting a cancellation');
  PERFORM check_principal(ctx, p_principal);
  IF p_idem_key IS NULL OR length(p_idem_key) NOT BETWEEN 8 AND 200 THEN
    RAISE EXCEPTION 'BAD_IDEMPOTENCY_KEY' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO r FROM cancellation_requests WHERE idem_key = p_idem_key;
  IF FOUND THEN
    IF r.appointment_id IS DISTINCT FROM p_appointment THEN RAISE EXCEPTION 'IDEMPOTENCY_KEY_REUSED' USING ERRCODE = 'P0001'; END IF;
    RETURN to_jsonb(r) || jsonb_build_object('replayed', true);
  END IF;
  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment FOR UPDATE;       -- staff scope (RLS)
  IF NOT FOUND THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF a.status = 'NO_SHOW' AND a.is_current AND a.no_show_resolution IS NULL THEN
    NULL;                                                                                -- 0048: after a no-show (D3)
  ELSE
    IF a.status <> 'CONFIRMED' OR NOT a.is_current THEN
      RAISE EXCEPTION 'NOT_CANCELLABLE: appointment is %', a.status USING ERRCODE = 'P0001'; END IF;
    IF lower(a.during) <= clock_timestamp() THEN RAISE EXCEPTION 'APPOINTMENT_STARTED' USING ERRCODE = 'P0001'; END IF;
    IF a.arrived_at IS NOT NULL THEN RAISE EXCEPTION 'ALREADY_ARRIVED' USING ERRCODE = 'P0001'; END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM cancellation_requests c WHERE c.appointment_id = p_appointment AND c.status = 'OPEN') THEN
    RAISE EXCEPTION 'CANCELLATION_ALREADY_PENDING' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dispatch_offers o WHERE o.appointment_id = p_appointment AND o.status = 'OFFERED'
              AND o.expires_at > clock_timestamp()) THEN
    RAISE EXCEPTION 'REASSIGNMENT_IN_PROGRESS' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO cancellation_requests (request_id, appointment_id, shipment_id, facility_id, dock_id, during, reason_code,
                                     note, requested_by, requested_role, idem_key)
  VALUES (rid, p_appointment, a.shipment_id, fac, a.dock_id, a.during, p_reason, p_note, ctx.principal, role, p_idem_key)
  RETURNING * INTO r;
  IF a.status = 'NO_SHOW' THEN
    UPDATE appointments SET no_show_resolution = 'CANCELLATION_REQUESTED', no_show_resolved_by = ctx.principal,
                            no_show_resolved_at = clock_timestamp() WHERE appointment_id = a.appointment_id;
  END IF;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'CANCELLATION_REQUESTED', fac, a.shipment_id,
          jsonb_build_object('request_id', rid, 'appointment_id', p_appointment, 'reason_code', p_reason,
                             'after_no_show', a.status = 'NO_SHOW'), p_note);
  RETURN to_jsonb(r) || jsonb_build_object('replayed', false);
END $$;

CREATE OR REPLACE FUNCTION decide_cancellation(p_request uuid, p_decision text, p_note text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; r cancellation_requests; a appointments; s shipments; k text; ns boolean;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['planner','ops_lead'], 'deciding a cancellation');
  PERFORM check_principal(ctx, p_principal);
  IF p_decision NOT IN ('APPROVE','REJECT') THEN RAISE EXCEPTION 'BAD_DECISION' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO r FROM cancellation_requests WHERE request_id = p_request;                 -- staff scope (RLS)
  IF NOT FOUND OR NOT (r.facility_id = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'REQUEST_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  -- lock order: the appointment, then the request (as request_cancellation), so the two never deadlock
  SELECT * INTO a FROM appointments WHERE appointment_id = r.appointment_id FOR UPDATE;
  SELECT * INTO r FROM cancellation_requests WHERE request_id = p_request FOR UPDATE;
  IF r.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_DECIDED: %', r.status USING ERRCODE = 'P0001'; END IF;
  IF r.requested_by = ctx.principal THEN
    RAISE EXCEPTION 'ROLE_NOT_ALLOWED: the person who requested a cancellation cannot decide it' USING ERRCODE = 'P0001'; END IF;
  ns := a.status = 'NO_SHOW';
  IF ns THEN
    -- 0048: after a no-show. The dock time is already released; approval closes the booking, rejection hands it back
    -- to the dispatcher (Rebook or another request).
    UPDATE appointments SET no_show_resolution = CASE WHEN p_decision = 'APPROVE' THEN 'CANCELLED' END,
                            no_show_resolved_by = CASE WHEN p_decision = 'APPROVE' THEN ctx.principal END,
                            no_show_resolved_at = CASE WHEN p_decision = 'APPROVE' THEN clock_timestamp() END,
                            is_current = CASE WHEN p_decision = 'APPROVE' THEN false ELSE is_current END
     WHERE appointment_id = a.appointment_id;
  ELSIF p_decision = 'APPROVE' THEN
    IF a.status <> 'CONFIRMED' OR NOT a.is_current OR a.dock_id <> r.dock_id OR a.during <> r.during
       OR a.arrived_at IS NOT NULL THEN
      RAISE EXCEPTION 'APPOINTMENT_CHANGED: the appointment changed since the request; reject it and ask again'
        USING ERRCODE = 'P0001'; END IF;
    -- cancellation and capacity release: one transaction
    UPDATE appointments SET status = 'CANCELLED', is_current = false WHERE appointment_id = a.appointment_id;
    UPDATE dock_occupancy SET active = false WHERE occupancy_id = a.occupancy_id;
    UPDATE thread_flow SET state = 'CLOSED', appointment_id = NULL, version = version + 1, updated_at = now()
     WHERE appointment_id = a.appointment_id;
  END IF;
  UPDATE cancellation_requests SET status = CASE WHEN p_decision = 'APPROVE' THEN 'APPROVED' ELSE 'REJECTED' END,
         decided_by = ctx.principal, decided_role = role, decided_at = clock_timestamp(), decision_note = p_note
   WHERE request_id = p_request RETURNING * INTO r;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, CASE WHEN p_decision = 'APPROVE' THEN 'APPOINTMENT_CANCELLED' ELSE 'CANCELLATION_REJECTED' END,
          r.facility_id, r.shipment_id, jsonb_build_object('appointment_id', a.appointment_id, 'status', a.status),
          jsonb_build_object('request_id', r.request_id, 'decision', p_decision, 'after_no_show', ns,
                             'capacity_released', p_decision = 'APPROVE' AND NOT ns), coalesce(p_note, r.note));
  IF NOT ns THEN                                   -- after a no-show the driver was already told the slot was released
    SELECT * INTO s FROM shipments WHERE shipment_id = r.shipment_id;
    SELECT dock_code INTO k FROM docks WHERE dock_id = a.dock_id;
    PERFORM notify_driver(s.driver_id, r.facility_id, CASE WHEN p_decision = 'APPROVE'
      THEN 'Your dock appointment for order ' || s.order_reference || ' (dock ' || k || ', '
           || to_char(lower(a.during) AT TIME ZONE 'Asia/Kolkata', 'FMHH12:MI AM "on" FMDD Mon')
           || ') has been cancelled by the warehouse, as agreed with your dispatcher.'
      ELSE 'Your dock appointment for order ' || s.order_reference || ' (dock ' || k || ', '
           || to_char(lower(a.during) AT TIME ZONE 'Asia/Kolkata', 'FMHH12:MI AM "on" FMDD Mon')
           || ') stays confirmed. The cancellation request was not approved.' END);
  END IF;
  RETURN to_jsonb(r);
END $$;

-- ================================================================ 7. dispatcher reschedule request (issue 6, Q2)
-- For a CONFIRMED booking before its start: the dispatcher picks a feasible slot; this creates a PENDING request for a
-- planner (always PLANNER_REVIEW, Q2) that REPLACES the booking only when approved. The new slot is claimed at once
-- (an APPOINTMENT claim, like a driver's request), so no competing booking can take it; the original stays CONFIRMED
-- with its own claim until decide_booking_request approves, which cancels it and confirms the new one in one
-- transaction (unchanged). Rejection or expiry releases the new claim and leaves the original as it was.
CREATE FUNCTION request_reschedule(p_appointment text, p_dock text, p_start timestamptz, p_end timestamptz,
                                   p_idem_key text, p_principal text, p_note text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; s shipments; d docks; fac text; cfg facility_config; clk timestamptz;
  shift text; ttl integer; exp timestamptz; occ bigint; appt text := 'APT-' || replace(gen_random_uuid()::text, '-', '');
  aid uuid := gen_random_uuid(); resp jsonb; k text := 'reschedule:' || coalesce(p_idem_key, ''); slot tstzrange; why text;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'requesting a new time');
  PERFORM check_principal(ctx, p_principal);
  IF p_idem_key IS NULL OR length(p_idem_key) NOT BETWEEN 8 AND 200 THEN
    RAISE EXCEPTION 'BAD_IDEMPOTENCY_KEY' USING ERRCODE = 'P0001'; END IF;
  IF p_note IS NULL OR length(p_note) NOT BETWEEN 3 AND 300 THEN RAISE EXCEPTION 'NOTE_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  resp := idem_begin(k, md5(concat_ws('|', p_appointment, p_dock, p_start, p_end, ctx.principal)));
  IF resp IS NOT NULL THEN RETURN resp || jsonb_build_object('replayed', true); END IF;
  IF p_start IS NULL OR p_end IS NULL OR p_end <= p_start THEN RAISE EXCEPTION 'BAD_SLOT' USING ERRCODE = 'P0001'; END IF;
  slot := tstzrange(p_start, p_end, '[)');

  SELECT * INTO a FROM appointments WHERE appointment_id = p_appointment FOR UPDATE;       -- staff scope (RLS)
  IF NOT FOUND THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO fac FROM docks WHERE dock_id = a.dock_id;
  IF NOT (fac = ANY (ctx.facilities)) THEN RAISE EXCEPTION 'APPOINTMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = a.shipment_id FOR SHARE;
  SELECT * INTO d FROM docks WHERE dock_id = p_dock FOR SHARE;
  IF NOT FOUND OR d.facility_id <> fac THEN RAISE EXCEPTION 'DOCK_NOT_AT_FACILITY' USING ERRCODE = 'P0001'; END IF;
  clk := clock_timestamp();
  IF a.status <> 'CONFIRMED' OR NOT a.is_current THEN
    RAISE EXCEPTION 'NOT_RESCHEDULABLE: appointment is %', a.status USING ERRCODE = 'P0001'; END IF;
  IF lower(a.during) <= clk THEN RAISE EXCEPTION 'APPOINTMENT_STARTED' USING ERRCODE = 'P0001'; END IF;
  IF a.arrived_at IS NOT NULL THEN RAISE EXCEPTION 'ALREADY_ARRIVED' USING ERRCODE = 'P0001'; END IF;
  IF slot = a.during AND p_dock = a.dock_id THEN RAISE EXCEPTION 'SAME_SLOT' USING ERRCODE = 'P0001'; END IF;
  IF p_start <= clk THEN RAISE EXCEPTION 'BAD_SLOT: the new time has passed' USING ERRCODE = 'P0001'; END IF;
  IF s.current_status NOT IN ('PLANNED','ASSIGNED','IN_TRANSIT') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: status %', s.current_status USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM cancellation_requests c WHERE c.appointment_id = a.appointment_id AND c.status = 'OPEN') THEN
    RAISE EXCEPTION 'CANCELLATION_PENDING' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dispatch_offers o WHERE o.shipment_id = a.shipment_id AND o.status = 'OFFERED'
              AND o.expires_at > clk) THEN
    RAISE EXCEPTION 'REASSIGNMENT_IN_PROGRESS' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM approval_requests r WHERE r.shipment_id = a.shipment_id AND r.status = 'OPEN') THEN
    RAISE EXCEPTION 'TIME_CHANGE_PENDING: a request for this shipment is already with a planner' USING ERRCODE = 'P0001'; END IF;
  IF (SELECT automation_paused FROM facilities WHERE facility_id = fac) THEN
    RAISE EXCEPTION 'AUTOMATION_PAUSED' USING ERRCODE = 'P0001'; END IF;
  IF booking_hold_active(fac, p_dock) THEN
    RAISE EXCEPTION 'OPERATIONAL_HOLD: bookings at this facility or dock are on hold' USING ERRCODE = 'P0001'; END IF;
  why := booking_incompatibility(s, d);
  IF why IS NOT NULL THEN RAISE EXCEPTION '%', why USING ERRCODE = 'P0001'; END IF;
  why := driver_trip_conflict(s.driver_id, s.shipment_id, slot);
  IF why IS NOT NULL THEN RAISE EXCEPTION '%', why USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO cfg FROM facility_config WHERE facility_id = fac AND active;
  IF p_start - make_interval(mins => cfg.confirm_lead_min) < clk THEN
    RAISE EXCEPTION 'SLOT_TOO_LATE: a planner needs % min before the start', cfg.confirm_lead_min USING ERRCODE = 'P0001'; END IF;
  shift := facility_shift(fac, clk);
  ttl := (cfg.warehouse_confirm_ttl_min ->> shift)::integer;
  exp := LEAST(clk + make_interval(mins => COALESCE(ttl, 0)), p_start - make_interval(mins => cfg.confirm_lead_min),
               lower(a.during));                     -- decided before the original slot starts, or it expires
  IF ttl IS NULL OR ttl = 0 OR exp <= clk THEN
    RAISE EXCEPTION 'NO_CONFIRMER_AVAILABLE: shift %, ttl %', shift, ttl USING ERRCODE = 'P0001'; END IF;

  -- the claim (serialized per dock like place_hold; the EXCLUDE constraint refuses an overlapping booking: 23P01)
  PERFORM pg_advisory_xact_lock(hashtext(p_dock));
  INSERT INTO dock_occupancy (dock_id, facility_id, during, kind, shipment_id, ref_id, created_by)
  VALUES (p_dock, fac, slot, 'APPOINTMENT', a.shipment_id, appt, ctx.principal) RETURNING occupancy_id INTO occ;
  INSERT INTO appointments (appointment_id, shipment_id, dock_id, during, occupancy_id, status, is_current,
                            booking_source, pending_expires_at, replaced_appointment_id, needs_approval_reason)
  VALUES (appt, a.shipment_id, p_dock, slot, occ, 'PENDING_CONFIRMATION', false, 'DISPATCHER', exp, a.appointment_id,
          'PLANNER_REVIEW');
  INSERT INTO approval_requests (approval_id, kind, facility_id, shipment_id, proposed_action, evidence, status, expires_at,
                                 appointment_id, claim_occupancy_id, thread_id)
  VALUES (aid, 'PLANNER_REVIEW', fac, a.shipment_id,
          jsonb_build_object('dock_id', p_dock, 'start', p_start, 'end', p_end, 'replaces', a.appointment_id),
          jsonb_build_object('source', 'DISPATCHER_RESCHEDULE', 'requested_by', ctx.principal, 'requested_role', role,
                             'note', p_note, 'from_dock_id', a.dock_id, 'from_start', lower(a.during),
                             'from_end', upper(a.during), 'move_earlier', p_start < lower(a.during),
                             'rule', 'Q2: a dispatcher time change always goes to a planner', 'evaluated_at', clk),
          'OPEN', exp, appt, occ, (SELECT thread_id FROM chat_threads WHERE shipment_id = a.shipment_id
                                    ORDER BY opened_at DESC LIMIT 1));
  resp := jsonb_build_object('status', 'PENDING', 'appointment_id', appt, 'approval_id', aid, 'kind', 'PLANNER_REVIEW',
                             'shipment_id', a.shipment_id, 'dock_id', p_dock, 'dock_code', d.dock_code,
                             'start', p_start, 'end', p_end, 'pending_expires_at', exp, 'needs_approval', true,
                             'replaces', a.appointment_id, 'original_status', 'CONFIRMED', 'shift', shift);
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (fac, 'appointment', appt, 'BookingRequested', resp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
  VALUES (ctx.principal, role, 'RESCHEDULE_REQUESTED', fac, a.shipment_id, to_jsonb(a), resp, p_note);
  RETURN idem_finish(k, resp) || jsonb_build_object('replayed', false);
END $$;

-- Q2 + separation of duties: the person who asked for a time change never approves it
CREATE FUNCTION trg_requester_cannot_approve() RETURNS trigger LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF NEW.status = 'APPROVED' AND OLD.status = 'OPEN' AND NEW.evidence ->> 'requested_by' IS NOT NULL
     AND NEW.decided_by = NEW.evidence ->> 'requested_by' THEN
    RAISE EXCEPTION 'ROLE_NOT_ALLOWED: the person who requested a time change cannot approve it' USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER requester_cannot_approve BEFORE UPDATE OF status ON approval_requests
  FOR EACH ROW EXECUTE FUNCTION trg_requester_cannot_approve();

-- ================================================================ 8. free drivers and the explicit offer target
-- A driver is free for a slot when ACTIVE, of the shipment's carrier, not the assigned driver, with no shipment at the
-- gate or in a dock, no live appointment overlapping the slot (+/- 2 h, driver_trip_conflict: COMPLETED and NO_SHOW
-- no longer count) and no live offer for another shipment. Staff scope: the shipment must be in the caller's scope.
CREATE FUNCTION free_drivers_for(p_shipment text, p_start timestamptz, p_end timestamptz) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE s shipments; out jsonb := '[]'; old_scope text := current_setting('app.facility_scope', true); slot tstzrange;
BEGIN
  SELECT * INTO s FROM shipments WHERE shipment_id = p_shipment;                            -- caller's scope (RLS)
  IF NOT FOUND THEN RAISE EXCEPTION 'SHIPMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF p_start IS NULL OR p_end IS NULL OR p_end <= p_start THEN RAISE EXCEPTION 'BAD_SLOT' USING ERRCODE = 'P0001'; END IF;
  slot := tstzrange(p_start, p_end, '[)');
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);                    -- other trips anywhere
  SELECT coalesce(jsonb_agg(jsonb_build_object('driver_id', d.driver_id, 'driver_name', d.driver_name,
                  'telegram_linked', EXISTS (SELECT 1 FROM driver_channel_links l WHERE l.driver_id = d.driver_id
                                              AND l.channel = 'TELEGRAM' AND l.revoked_at IS NULL))
                  ORDER BY d.driver_id), '[]')
    INTO out
    FROM drivers d
   WHERE d.status = 'ACTIVE' AND d.carrier_id = s.carrier_id AND d.driver_id <> s.driver_id
     AND NOT EXISTS (SELECT 1 FROM shipments x WHERE x.driver_id = d.driver_id
                      AND x.current_status IN ('AT_GATE','WAITING','IN_DOCK'))
     AND driver_trip_conflict(d.driver_id, p_shipment, slot) IS NULL
     AND NOT EXISTS (SELECT 1 FROM dispatch_offers o WHERE o.driver_id = d.driver_id AND o.status = 'OFFERED'
                      AND o.expires_at > clock_timestamp() AND o.shipment_id <> p_shipment);
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN out;
END $$;

-- Issue 5: the dispatcher states who the offer is for. ASSIGNED must be the assigned driver; OTHER can never be.
CREATE FUNCTION create_dispatch_offer_to(p_target text, p_shipment text, p_driver text, p_vehicle text, p_dock text,
                                         p_start timestamptz, p_end timestamptz, p_needs_approval boolean,
                                         p_schedule_version bigint, p_ttl_s integer, p_text text, p_idem_key text,
                                         p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE assigned text;
BEGIN
  IF p_target NOT IN ('ASSIGNED','OTHER') THEN RAISE EXCEPTION 'BAD_TARGET: ASSIGNED or OTHER' USING ERRCODE = 'P0001'; END IF;
  SELECT driver_id INTO assigned FROM shipments WHERE shipment_id = p_shipment;             -- caller's scope (RLS)
  IF assigned IS NULL THEN RAISE EXCEPTION 'SHIPMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF p_target = 'ASSIGNED' AND p_driver IS DISTINCT FROM assigned THEN
    RAISE EXCEPTION 'TARGET_MISMATCH: "This driver" must be the assigned driver' USING ERRCODE = 'P0001'; END IF;
  IF p_target = 'OTHER' AND p_driver = assigned THEN
    RAISE EXCEPTION 'SAME_DRIVER: "Another driver" cannot be the assigned driver' USING ERRCODE = 'P0001'; END IF;
  RETURN create_dispatch_offer(p_shipment, p_driver, p_vehicle, p_dock, p_start, p_end, p_needs_approval,
                               p_schedule_version, p_ttl_s, p_text, p_idem_key, p_principal)
         || jsonb_build_object('target', p_target);
END $$;

-- ================================================================ 9. reads for the console
-- Who last paused / resumed automation at each facility in scope (the Controls tab)
CREATE FUNCTION facility_controls(p_facilities text[]) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
           'facility_id', f.facility_id, 'facility_name', f.facility_name, 'timezone', f.timezone,
           'automation_paused', f.automation_paused,
           'no_show_grace_min', (SELECT no_show_grace_min FROM facility_config c WHERE c.facility_id = f.facility_id AND c.active),
           'last_change', (SELECT jsonb_build_object('action', e.action, 'by', e.principal, 'role', e.principal_role,
                                                      'at', e.at, 'reason', e.reason)
                             FROM booking_events e WHERE e.facility_id = f.facility_id
                              AND e.action IN ('PAUSE_AUTOMATION','RESUME_AUTOMATION')
                            ORDER BY e.at DESC, e.event_id DESC LIMIT 1)) ORDER BY f.facility_id), '[]')
    FROM facilities f WHERE f.facility_id = ANY (p_facilities) AND scope_allows(f.facility_id)
$$;

-- The kill switch's last change (who / when / why) for the Controls tab
CREATE FUNCTION kill_switch_detail() RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT jsonb_build_object('engaged', engaged, 'changed_at', changed_at, 'changed_by', changed_by, 'reason', reason)
    FROM booking_kill_switch WHERE id = 1
$$;

-- ================================================================ ownership and privileges
ALTER FUNCTION no_show_deadline(timestamptz, integer)                         OWNER TO setuhaul_rpc;
ALTER FUNCTION no_show_due(timestamptz, integer, timestamptz)                  OWNER TO setuhaul_rpc;
ALTER FUNCTION facility_no_show_grace(text)                                    OWNER TO setuhaul_rpc;
ALTER FUNCTION no_show_ineligibility(appointments)                             OWNER TO setuhaul_rpc;
ALTER FUNCTION no_show_unresolved(text)                                        OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_no_show_blocks_booking()                                    OWNER TO setuhaul_rpc;
ALTER FUNCTION lifecycle_lock(text, staff_context)                             OWNER TO setuhaul_rpc;
ALTER FUNCTION lifecycle_json(appointments, boolean)                           OWNER TO setuhaul_rpc;
ALTER FUNCTION record_gate_arrival(text, text, text)                           OWNER TO setuhaul_rpc;
ALTER FUNCTION record_dock_start(text, text, text)                             OWNER TO setuhaul_rpc;
ALTER FUNCTION record_completion(text, text, text)                             OWNER TO setuhaul_rpc;
ALTER FUNCTION mark_no_show(text, text, text, uuid)                            OWNER TO setuhaul_rpc;
ALTER FUNCTION process_no_shows(integer)                                       OWNER TO setuhaul_rpc;
ALTER FUNCTION backfill_no_shows(boolean, text, text, uuid)                    OWNER TO setuhaul_rpc;
ALTER FUNCTION resolve_no_show(text, text, text, text)                         OWNER TO setuhaul_rpc;
ALTER FUNCTION request_reschedule(text, text, timestamptz, timestamptz, text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION free_drivers_for(text, timestamptz, timestamptz)                OWNER TO setuhaul_rpc;
ALTER FUNCTION create_dispatch_offer_to(text, text, text, text, text, timestamptz, timestamptz, boolean, bigint, integer, text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION facility_controls(text[])                                       OWNER TO setuhaul_rpc;
ALTER FUNCTION kill_switch_detail()                                            OWNER TO setuhaul_rpc;

GRANT UPDATE (arrived_at, arrived_by, docked_at, docked_by, completed_at, completed_by, no_show_at, no_show_basis,
              no_show_resolution, no_show_resolved_by, no_show_resolved_at, status, is_current, updated_at, row_version)
  ON appointments TO setuhaul_rpc;
GRANT UPDATE (current_status, updated_at) ON shipments TO setuhaul_rpc;
GRANT SELECT ON lifecycle_cutover, booking_kill_switch, driver_channel_links, facility_config, booking_events TO setuhaul_rpc;
GRANT SELECT, INSERT ON no_show_backfill_runs TO setuhaul_rpc;
GRANT SELECT ON lifecycle_cutover, no_show_backfill_runs TO setuhaul_console, setuhaul_system;
GRANT SELECT, UPDATE ON thread_flow TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_gate_arrival(text, text, text)                TO setuhaul_console;
GRANT EXECUTE ON FUNCTION record_dock_start(text, text, text)                  TO setuhaul_console;
GRANT EXECUTE ON FUNCTION record_completion(text, text, text)                  TO setuhaul_console;
GRANT EXECUTE ON FUNCTION resolve_no_show(text, text, text, text)              TO setuhaul_console;
GRANT EXECUTE ON FUNCTION request_reschedule(text, text, timestamptz, timestamptz, text, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION free_drivers_for(text, timestamptz, timestamptz)     TO setuhaul_console;
GRANT EXECUTE ON FUNCTION create_dispatch_offer_to(text, text, text, text, text, timestamptz, timestamptz, boolean, bigint, integer, text, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION facility_controls(text[])                            TO setuhaul_console;
GRANT EXECUTE ON FUNCTION kill_switch_detail()                                 TO setuhaul_console;
GRANT EXECUTE ON FUNCTION no_show_deadline(timestamptz, integer)               TO setuhaul_console, setuhaul_system;
GRANT EXECUTE ON FUNCTION no_show_due(timestamptz, integer, timestamptz)        TO setuhaul_console, setuhaul_system;
GRANT EXECUTE ON FUNCTION process_no_shows(integer)                            TO setuhaul_system;
GRANT EXECUTE ON FUNCTION backfill_no_shows(boolean, text, text, uuid)         TO setuhaul_system;
