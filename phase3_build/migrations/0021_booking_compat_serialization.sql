-- 0021 · Review R3.2 fix. Forward-only; 0001–0020 untouched.
--
-- R3.2-P1 A shipment change and a dock change to the same CONFIRMED booking could each check compatibility against
--   the other's OLD row (write skew): shipment 1,000 → 1,500 kg and dock limit 2,000 → 1,200 kg both pass, both
--   commit, and the booking is incompatible with no BOOKING_AT_RISK escalation.
--
--   Fix: every compatibility-affecting change takes a transaction-scoped advisory lock per dock,
--   "booking compatibility of dock D", before it checks:
--     * a dock change takes the key of its own dock;
--     * a shipment change takes the key of each dock that holds a current CONFIRMED booking for the shipment, in
--       dock_id order.
--   Two changes that can affect the same booking therefore share a key, and the second one waits until the first
--   commits. Its check runs in a fresh READ COMMITTED snapshot after the wait, so it sees the first change's
--   committed values. Either the final state is compatible, or the second change raises the escalation.
--
--   Lock order (extends 0020):
--     thread_lock → shipments (row) → docks (row) → booking-compat advisory (dock_id order) → approval_requests
--       → appointments → dock_occupancy
--   The AFTER triggers take NO row locks on the other table (no reverse order). They take only the advisory key
--   after their own row lock, and before the invalidation locks approval rows. decide_booking_request takes no
--   advisory key; it serialises with these changes through its SHARE locks on the shipment and dock rows (0020).
--   The set of docks a shipment trigger locks cannot grow underneath it: a new confirmation needs a SHARE lock on
--   the shipment row, which the UPDATE already holds exclusively.
--
--   Isolation: the re-read after the wait relies on a new snapshot per statement. READ COMMITTED gives that, and
--   SERIALIZABLE detects the write skew itself. REPEATABLE READ would keep the old snapshot, so a
--   compatibility-affecting change under REPEATABLE READ is refused (COMPAT_CHANGE_NEEDS_READ_COMMITTED). The app
--   and the tools all write at the default, READ COMMITTED.
SET search_path = setuhaul, public;

CREATE FUNCTION lock_booking_compat(p_docks text[]) RETURNS void LANGUAGE plpgsql
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE d text;
BEGIN
  IF current_setting('transaction_isolation') = 'repeatable read' THEN
    RAISE EXCEPTION 'COMPAT_CHANGE_NEEDS_READ_COMMITTED' USING ERRCODE = 'P0001';
  END IF;
  FOR d IN SELECT DISTINCT x FROM unnest(p_docks) x WHERE x IS NOT NULL ORDER BY x LOOP
    PERFORM pg_advisory_xact_lock(hashtext('setuhaul.booking_compat'), hashtext(d));
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION trg_invalidate_on_shipment() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  IF NEW.current_status IN ('IN_DOCK','COMPLETED','CANCELLED')
     OR (NEW.vehicle_id, NEW.destination_facility_id, NEW.load_weight_kg, NEW.required_dock_type, NEW.temperature_control_required)
        IS DISTINCT FROM
        (OLD.vehicle_id, OLD.destination_facility_id, OLD.load_weight_kg, OLD.required_dock_type, OLD.temperature_control_required)
  THEN
    PERFORM set_config('app.facility_scope', (SELECT string_agg(facility_id, ',') FROM facilities), true);
    IF NEW.current_status NOT IN ('IN_DOCK','COMPLETED','CANCELLED') THEN       -- R3.2: before any approval row lock
      PERFORM lock_booking_compat(ARRAY(SELECT dock_id FROM appointments
                                         WHERE shipment_id = NEW.shipment_id AND is_current AND status = 'CONFIRMED'));
    END IF;
    PERFORM invalidate_open_requests('shipment', NEW.shipment_id, 'SHIPMENT_CHANGED');
    IF NEW.current_status NOT IN ('IN_DOCK','COMPLETED','CANCELLED') THEN
      PERFORM flag_bookings_at_risk(NEW.shipment_id, NULL, 'SHIPMENT_CHANGED');
    END IF;
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION trg_invalidate_on_dock() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM lock_booking_compat(ARRAY[NEW.dock_id]);                             -- R3.2: before any approval row lock
  PERFORM set_config('app.facility_scope', NEW.facility_id, true);
  PERFORM invalidate_open_requests('dock', NEW.dock_id, 'DOCK_UNAVAILABLE');
  PERFORM flag_bookings_at_risk(NULL, NEW.dock_id, 'DOCK_CHANGED');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NULL;
END $$;

ALTER FUNCTION lock_booking_compat(text[]) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
