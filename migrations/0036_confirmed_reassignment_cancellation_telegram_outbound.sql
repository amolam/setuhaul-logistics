-- 0036 · Confirmed-appointment reassignment, confirmed-appointment cancellation, Telegram outbound routing support,
-- link-code throttling, escalation acknowledge/resolve. Forward-only; 0001–0035 untouched.
--
-- Contracts (unchanged, restated because the new functions rely on them):
--   * offer TTL (SETUHAUL_OPTION_TTL_S, 300 s)  -> how long a driver may accept an offer; reserves nothing.
--   * dock hold (180 s)                           -> from the driver's yes to the booking request, in one flow.
--   * the request's claim (pending_expires_at)    -> at submit the hold row BECOMES the request's APPOINTMENT claim
--                                                    (0016); it holds the capacity for the planner's decision window,
--                                                    and decide_booking_request re-checks expiry and the claim under
--                                                    row locks (PENDING_EXPIRED / CAPACITY_LOST), so a stale approval
--                                                    can never succeed.
--
-- Confirmed reassignment: a CONFIRMED_REASSIGNMENT offer carries the EXISTING appointment's dock and time. The new
-- driver's explicit yes switches shipments.driver_id in the attestation's transaction after re-checking (under row
-- locks) that the appointment is unchanged and still confirmed, no cancellation is pending and the new driver has no
-- conflicting trip. No hold, no request, no new appointment: the appointment row and its dock claim are untouched, so
-- capacity is neither released nor reserved twice. The truck is unchanged (vehicle changes stay out of scope), so
-- compatibility is unchanged and no planner re-approval is needed (owner rule, 25 Nov 2026).
--
-- Cancellation: a dispatcher (operations / ops_lead) requests it with a reason; a planner or ops_lead who is NOT the
-- requester decides. Approval cancels the appointment and releases its dock claim in the same transaction, after
-- re-checking that the appointment is unchanged. Rejection changes nothing. Drivers cannot cancel.
SET search_path = setuhaul, public;

-- ================================================================ helpers
-- One AGENT message to a driver in their latest open conversation, queued like the 0022 hand-back notice (so the
-- relay delivers it to the test sink, WhatsApp or Telegram, with retries). No open conversation: nothing is sent.
CREATE FUNCTION notify_driver(p_driver text, p_facility text, p_text text) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE tid text; mid bigint; h text := encode(sha256(convert_to(p_text, 'UTF8')), 'hex');
BEGIN
  SELECT thread_id INTO tid FROM chat_threads WHERE driver_id = p_driver AND status NOT IN ('CLOSED','RESOLVED')
   ORDER BY opened_at DESC LIMIT 1;
  IF tid IS NULL THEN RETURN NULL; END IF;
  INSERT INTO chat_messages (thread_id, sender_type, channel, content_hash, body_masked)
  VALUES (tid, 'AGENT', 'WHATSAPP', h, p_text) RETURNING message_id INTO mid;
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (p_facility, 'chat_message', mid::text, 'AgentReplyStored',
          jsonb_build_object('message_id', mid, 'thread_id', tid, 'driver_id', p_driver, 'content_hash', h,
                             'channel', 'WHATSAPP'));
  RETURN mid;
END $$;

-- Does this driver have another trip whose live appointment overlaps the window (+/- 2 h for travel between
-- trips)? The only travel constraint the data supports; NULL = no conflict found, else the reason.
CREATE FUNCTION driver_trip_conflict(p_driver text, p_shipment text, p_during tstzrange) RETURNS text LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT 'DRIVER_HAS_OVERLAPPING_TRIP: ' || s.order_reference
    FROM shipments s JOIN appointments a ON a.shipment_id = s.shipment_id
   WHERE s.driver_id = p_driver AND s.shipment_id <> p_shipment AND s.current_status NOT IN ('COMPLETED','CANCELLED')
     AND (a.is_current OR a.status = 'PENDING_CONFIRMATION')
     AND a.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
     AND a.during && tstzrange(lower(p_during) - interval '2 hours', upper(p_during) + interval '2 hours', '[)')
   ORDER BY lower(a.during) LIMIT 1
$$;

-- ================================================================ confirmed reassignment
ALTER TABLE dispatch_offers DROP CONSTRAINT dispatch_offers_kind_check;
ALTER TABLE dispatch_offers ADD CONSTRAINT dispatch_offers_kind_check
  CHECK (kind IN ('INITIAL_BOOKING','REASSIGNMENT','CONFIRMED_REASSIGNMENT'));
ALTER TABLE dispatch_offers DROP CONSTRAINT dispatch_offers_check;
ALTER TABLE dispatch_offers ADD CONSTRAINT dispatch_offers_previous_driver
  CHECK ((kind IN ('REASSIGNMENT','CONFIRMED_REASSIGNMENT')) = (previous_driver_id IS NOT NULL));
ALTER TABLE dispatch_offers ADD CONSTRAINT dispatch_offers_confirmed_names_appointment
  CHECK (kind <> 'CONFIRMED_REASSIGNMENT' OR appointment_id IS NOT NULL);

CREATE TABLE cancellation_requests (
  request_id      uuid PRIMARY KEY,
  appointment_id  text NOT NULL REFERENCES appointments,
  shipment_id     text NOT NULL REFERENCES shipments,
  facility_id     text NOT NULL REFERENCES facilities,
  dock_id         text NOT NULL REFERENCES docks,
  during          tstzrange NOT NULL,                  -- the appointment as it was when requested
  reason_code     text NOT NULL CHECK (reason_code IN ('DRIVER_UNAVAILABLE','VEHICLE_BREAKDOWN','SHIPMENT_CANCELLED',
                                                        'CUSTOMER_REQUEST','OPERATIONS_DECISION')),
  note            text NOT NULL CHECK (length(note) BETWEEN 3 AND 500),
  status          text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','APPROVED','REJECTED')),
  requested_by    text NOT NULL,
  requested_role  text NOT NULL,
  idem_key        text NOT NULL UNIQUE,
  created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  decided_by      text,
  decided_role    text,
  decided_at      timestamptz,
  decision_note   text,
  CHECK ((status = 'OPEN') = (decided_at IS NULL)),
  CHECK (decided_by IS NULL OR decided_by <> requested_by)          -- nobody decides their own request
);
CREATE UNIQUE INDEX ux_one_open_cancellation ON cancellation_requests (appointment_id) WHERE status = 'OPEN';
ALTER TABLE cancellation_requests ENABLE ROW LEVEL SECURITY; ALTER TABLE cancellation_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY p_cancellation_requests ON cancellation_requests
  USING (scope_allows(facility_id)) WITH CHECK (scope_allows(facility_id));

CREATE FUNCTION create_reassignment_offer(p_shipment text, p_driver text, p_ttl_s integer, p_text text, p_idem_key text,
                                          p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; s shipments; d drivers; a appointments; t chat_threads; o dispatch_offers;
  oid uuid := gen_random_uuid(); offer uuid := gen_random_uuid(); mid bigint; sel bigint; fstate text; why text;
  h text; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'reassigning a confirmed appointment');
  PERFORM check_principal(ctx, p_principal);
  IF p_idem_key IS NULL OR length(p_idem_key) NOT BETWEEN 8 AND 200 THEN
    RAISE EXCEPTION 'BAD_IDEMPOTENCY_KEY' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO o FROM dispatch_offers WHERE idem_key = p_idem_key;
  IF FOUND THEN
    IF o.shipment_id IS DISTINCT FROM p_shipment OR o.driver_id IS DISTINCT FROM p_driver THEN
      RAISE EXCEPTION 'IDEMPOTENCY_KEY_REUSED' USING ERRCODE = 'P0001'; END IF;
    RETURN dispatch_offer_json(o) || jsonb_build_object('replayed', true);
  END IF;
  IF p_text IS NULL OR length(p_text) NOT BETWEEN 10 AND 1000 THEN RAISE EXCEPTION 'BAD_OFFER_TEXT' USING ERRCODE = 'P0001'; END IF;
  IF p_ttl_s IS NULL OR p_ttl_s NOT BETWEEN 30 AND 3600 THEN RAISE EXCEPTION 'BAD_OFFER_TTL' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = p_shipment FOR UPDATE;
  IF NOT FOUND OR NOT (s.destination_facility_id = ANY (ctx.facilities)) THEN
    RAISE EXCEPTION 'SHIPMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF s.current_status NOT IN ('PLANNED','ASSIGNED','IN_TRANSIT') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: status %', s.current_status USING ERRCODE = 'P0001'; END IF;
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT * INTO a FROM appointments WHERE shipment_id = p_shipment AND is_current AND status = 'CONFIRMED' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_CONFIRMED_APPOINTMENT: use the booking offer instead' USING ERRCODE = 'P0001'; END IF;
  IF lower(a.during) <= clock_timestamp() THEN RAISE EXCEPTION 'APPOINTMENT_STARTED' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM cancellation_requests c WHERE c.appointment_id = a.appointment_id AND c.status = 'OPEN') THEN
    RAISE EXCEPTION 'CANCELLATION_PENDING' USING ERRCODE = 'P0001'; END IF;
  IF p_driver = s.driver_id THEN RAISE EXCEPTION 'SAME_DRIVER' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO d FROM drivers WHERE driver_id = p_driver;
  IF NOT FOUND OR d.status <> 'ACTIVE' THEN RAISE EXCEPTION 'DRIVER_NOT_AVAILABLE' USING ERRCODE = 'P0001'; END IF;
  why := driver_trip_conflict(p_driver, p_shipment, a.during);
  IF why IS NOT NULL THEN RAISE EXCEPTION '%', why USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO t FROM chat_threads WHERE driver_id = p_driver AND status NOT IN ('CLOSED','RESOLVED')
   ORDER BY opened_at DESC LIMIT 1;
  IF NOT FOUND THEN
    INSERT INTO chat_threads (thread_id, driver_id, shipment_id, status)
    VALUES ('THR-' || substr(md5(gen_random_uuid()::text), 1, 12), p_driver, p_shipment, 'OPEN') RETURNING * INTO t;
  END IF;
  PERFORM thread_lock(t.thread_id);
  SELECT * INTO t FROM chat_threads WHERE thread_id = t.thread_id;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE: a coordinator owns this conversation' USING ERRCODE = 'P0001'; END IF;
  SELECT state INTO fstate FROM thread_flow WHERE thread_id = t.thread_id;
  IF fstate IN ('CONFIRMED_BY_DRIVER','HELD','SUBMITTED') THEN
    RAISE EXCEPTION 'DRIVER_BUSY: the driver is finishing a booking' USING ERRCODE = 'P0001'; END IF;
  IF t.shipment_id IS DISTINCT FROM p_shipment THEN
    UPDATE chat_threads SET shipment_id = p_shipment WHERE thread_id = t.thread_id;
  END IF;

  UPDATE dispatch_offers SET status = 'EXPIRED', status_reason = 'expired before a newer offer'
   WHERE shipment_id = p_shipment AND status = 'OFFERED' AND expires_at <= clock_timestamp();
  UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp())
   WHERE option_id IN (SELECT option_id FROM dispatch_offers WHERE shipment_id = p_shipment AND status = 'OFFERED');
  UPDATE dispatch_offers SET status = 'SUPERSEDED', superseded_by = offer, status_reason = 'a newer offer was sent'
   WHERE shipment_id = p_shipment AND status = 'OFFERED';

  -- the option names the EXISTING slot; it is read back to the new driver, never held or booked
  INSERT INTO shown_options (option_id, thread_id, shipment_id, dock_id, during, needs_approval, rank, schedule_version,
                             expires_at, created_at)
  VALUES (oid, t.thread_id, p_shipment, a.dock_id, a.during, false, 1,
          (SELECT schedule_version FROM facilities WHERE facility_id = s.destination_facility_id),
          clock_timestamp() + make_interval(secs => p_ttl_s), now());
  h := encode(sha256(convert_to(p_text, 'UTF8')), 'hex');
  INSERT INTO chat_messages (thread_id, sender_type, channel, content_hash, body_masked)
  VALUES (t.thread_id, 'AGENT', 'WHATSAPP', h, p_text) RETURNING message_id INTO mid;
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (s.destination_facility_id, 'chat_message', mid::text, 'AgentReplyStored',
          jsonb_build_object('message_id', mid, 'thread_id', t.thread_id, 'driver_id', p_driver, 'content_hash', h,
                             'channel', 'WHATSAPP'));
  INSERT INTO option_selections (thread_id, option_id, read_back_message_id) VALUES (t.thread_id, oid, mid)
  RETURNING selection_id INTO sel;
  INSERT INTO thread_flow (thread_id, state, selected_option) VALUES (t.thread_id, 'SELECTED', oid)
  ON CONFLICT (thread_id) DO UPDATE SET state = 'SELECTED', selected_option = oid, yes_message_id = NULL,
     hold_option = NULL, appointment_id = NULL, attestation_id = NULL, version = thread_flow.version + 1, updated_at = now();
  INSERT INTO dispatch_offers (offer_id, kind, shipment_id, facility_id, driver_id, previous_driver_id, vehicle_id,
                               thread_id, option_id, selection_id, read_back_message_id, offered_by, offered_by_role,
                               idem_key, expires_at, appointment_id)
  VALUES (offer, 'CONFIRMED_REASSIGNMENT', p_shipment, s.destination_facility_id, p_driver, s.driver_id, s.vehicle_id,
          t.thread_id, oid, sel, mid, ctx.principal, role, p_idem_key,
          clock_timestamp() + make_interval(secs => p_ttl_s), a.appointment_id);
  SELECT * INTO o FROM dispatch_offers WHERE offer_id = offer;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'REASSIGNMENT_OFFER_SENT', s.destination_facility_id, p_shipment,
          jsonb_build_object('offer_id', offer, 'appointment_id', a.appointment_id, 'from_driver', s.driver_id,
                             'to_driver', p_driver, 'validation', 'no overlapping trip within 2 h; vehicle unchanged'),
          'confirmed appointment reassignment offered');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN dispatch_offer_json(o) || jsonb_build_object('replayed', false);
END $$;

-- The driver's reply moves the offer (0035 behaviour kept for INITIAL_BOOKING / REASSIGNMENT; REASSIGNMENT now also
-- refuses a new driver with an overlapping trip; CONFIRMED_REASSIGNMENT switches the driver on the existing booking).
CREATE OR REPLACE FUNCTION trg_dispatch_offer_attested() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o dispatch_offers; s shipments; a appointments; so shown_options; why text; ref text;
  old_scope text := current_setting('app.facility_scope', true);
  old_driver text := current_setting('app.driver_id', true);
BEGIN
  IF NEW.verdict NOT IN ('YES','NO') THEN RETURN NULL; END IF;     -- an unclear reply is never guessed
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  PERFORM set_config('app.driver_id', '', true);                   -- see the shipment as it is (0035)
  SELECT * INTO o FROM dispatch_offers WHERE selection_id = NEW.selection_id FOR UPDATE;
  IF FOUND AND o.status = 'OFFERED' THEN
    IF o.expires_at <= clock_timestamp() THEN
      UPDATE dispatch_offers SET status = 'EXPIRED', status_reason = 'reply after expiry' WHERE offer_id = o.offer_id;
    ELSIF NEW.verdict = 'NO' THEN
      UPDATE dispatch_offers SET status = 'DECLINED', responded_at = clock_timestamp(),
             response_message_id = NEW.confirmation_message_id WHERE offer_id = o.offer_id;
    ELSE
      why := NULL;
      IF o.kind IN ('REASSIGNMENT','CONFIRMED_REASSIGNMENT') THEN
        SELECT * INTO s FROM shipments WHERE shipment_id = o.shipment_id FOR UPDATE;
        SELECT * INTO so FROM shown_options WHERE option_id = o.option_id;
        IF s.driver_id IS DISTINCT FROM o.previous_driver_id THEN
          why := 'the shipment was assigned to someone else after the offer';
        ELSIF (SELECT status FROM drivers WHERE driver_id = o.driver_id) <> 'ACTIVE' THEN
          why := 'the new driver is no longer active';
        ELSE
          why := driver_trip_conflict(o.driver_id, o.shipment_id, so.during);
        END IF;
        IF why IS NULL AND o.kind = 'REASSIGNMENT' AND (
             EXISTS (SELECT 1 FROM appointments x WHERE x.shipment_id = o.shipment_id
                      AND (x.is_current OR x.status = 'PENDING_CONFIRMATION')
                      AND x.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'))
          OR EXISTS (SELECT 1 FROM dock_occupancy x WHERE x.shipment_id = o.shipment_id AND x.kind = 'HOLD' AND x.active
                      AND x.expires_at > clock_timestamp())) THEN
          why := 'a booking was made for the shipment after the offer';
        END IF;
        IF why IS NULL AND o.kind = 'CONFIRMED_REASSIGNMENT' THEN
          SELECT * INTO a FROM appointments WHERE appointment_id = o.appointment_id FOR UPDATE;
          IF a.status <> 'CONFIRMED' OR NOT a.is_current OR a.dock_id <> so.dock_id OR a.during <> so.during THEN
            why := 'the appointment changed or was cancelled after the offer';
          ELSIF lower(a.during) <= clock_timestamp() THEN
            why := 'the appointment has already started';
          ELSIF EXISTS (SELECT 1 FROM cancellation_requests c WHERE c.appointment_id = a.appointment_id AND c.status = 'OPEN') THEN
            why := 'a cancellation is pending for the appointment';
          END IF;
        END IF;
        IF why IS NOT NULL THEN
          -- the yes is recorded, the assignment and the appointment are not touched
          UPDATE dispatch_offers SET status = 'UNAVAILABLE', responded_at = clock_timestamp(),
                 response_message_id = NEW.confirmation_message_id, status_reason = left(why, 300)
           WHERE offer_id = o.offer_id;
          UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp()) WHERE option_id = o.option_id;
        ELSE
          UPDATE shipments SET driver_id = o.driver_id, updated_at = now() WHERE shipment_id = o.shipment_id;
          UPDATE chat_threads SET shipment_id = NULL WHERE shipment_id = o.shipment_id AND driver_id = o.previous_driver_id;
          INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state,
                                      after_state, reason)
          VALUES ('driver:' || o.driver_id, 'driver', 'SHIPMENT_REASSIGNED', o.facility_id, o.shipment_id,
                  jsonb_build_object('driver_id', o.previous_driver_id),
                  jsonb_build_object('driver_id', o.driver_id, 'appointment_id', o.appointment_id,
                                     'acknowledged_time', lower(so.during), 'offered_by', o.offered_by),
                  'new driver accepted offer ' || o.offer_id);
          IF o.kind = 'CONFIRMED_REASSIGNMENT' THEN
            -- the new driver's conversation now follows the confirmed booking; the read-back option can never be
            -- held or booked a second time
            UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp()) WHERE option_id = o.option_id;
            UPDATE thread_flow SET state = 'BOOKED', selected_option = NULL, appointment_id = o.appointment_id,
                   attestation_id = NULL, version = version + 1, updated_at = now() WHERE thread_id = o.thread_id;
            SELECT order_reference INTO ref FROM shipments WHERE shipment_id = o.shipment_id;
            PERFORM notify_driver(o.previous_driver_id, o.facility_id,
              'Order ' || ref || ' has been reassigned to another driver by your dispatcher. You do not need to go '
              || 'to the warehouse for it.');
          END IF;
          UPDATE dispatch_offers SET status = 'ACCEPTED', responded_at = clock_timestamp(),
                 response_message_id = NEW.confirmation_message_id WHERE offer_id = o.offer_id;
        END IF;
      ELSE
        UPDATE dispatch_offers SET status = 'ACCEPTED', responded_at = clock_timestamp(),
               response_message_id = NEW.confirmation_message_id WHERE offer_id = o.offer_id;
      END IF;
    END IF;
    INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
    VALUES ('driver:' || o.driver_id, 'driver', 'DISPATCH_OFFER_ANSWERED', o.facility_id, o.shipment_id,
            jsonb_build_object('offer_id', o.offer_id, 'kind', o.kind, 'verdict', NEW.verdict,
                               'status', (SELECT status FROM dispatch_offers WHERE offer_id = o.offer_id)), 'driver reply');
  END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  PERFORM set_config('app.driver_id', coalesce(old_driver, ''), true);
  RETURN NULL;
END $$;

-- ================================================================ cancellation of a confirmed appointment
CREATE FUNCTION request_cancellation(p_appointment text, p_reason text, p_note text, p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; a appointments; fac text; r cancellation_requests; rid uuid := gen_random_uuid();
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'requesting a cancellation');
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
  IF a.status <> 'CONFIRMED' OR NOT a.is_current THEN
    RAISE EXCEPTION 'NOT_CANCELLABLE: appointment is %', a.status USING ERRCODE = 'P0001'; END IF;
  IF lower(a.during) <= clock_timestamp() THEN RAISE EXCEPTION 'APPOINTMENT_STARTED' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM cancellation_requests c WHERE c.appointment_id = p_appointment AND c.status = 'OPEN') THEN
    RAISE EXCEPTION 'CANCELLATION_ALREADY_PENDING' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dispatch_offers o WHERE o.appointment_id = p_appointment AND o.status = 'OFFERED'
              AND o.expires_at > clock_timestamp()) THEN
    RAISE EXCEPTION 'REASSIGNMENT_IN_PROGRESS' USING ERRCODE = 'P0001'; END IF;
  INSERT INTO cancellation_requests (request_id, appointment_id, shipment_id, facility_id, dock_id, during, reason_code,
                                     note, requested_by, requested_role, idem_key)
  VALUES (rid, p_appointment, a.shipment_id, fac, a.dock_id, a.during, p_reason, p_note, ctx.principal, role, p_idem_key)
  RETURNING * INTO r;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'CANCELLATION_REQUESTED', fac, a.shipment_id,
          jsonb_build_object('request_id', rid, 'appointment_id', p_appointment, 'reason_code', p_reason), p_note);
  RETURN to_jsonb(r) || jsonb_build_object('replayed', false);
END $$;

CREATE FUNCTION decide_cancellation(p_request uuid, p_decision text, p_note text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; r cancellation_requests; a appointments; s shipments; k text;
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
  IF p_decision = 'APPROVE' THEN
    IF a.status <> 'CONFIRMED' OR NOT a.is_current OR a.dock_id <> r.dock_id OR a.during <> r.during THEN
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
          jsonb_build_object('request_id', r.request_id, 'decision', p_decision,
                             'capacity_released', p_decision = 'APPROVE'), coalesce(p_note, r.note));
  SELECT * INTO s FROM shipments WHERE shipment_id = r.shipment_id;
  SELECT dock_code INTO k FROM docks WHERE dock_id = a.dock_id;
  PERFORM notify_driver(s.driver_id, r.facility_id, CASE WHEN p_decision = 'APPROVE'
    THEN 'Your dock appointment for order ' || s.order_reference || ' (dock ' || k || ', '
         || to_char(lower(a.during) AT TIME ZONE 'Asia/Kolkata', 'FMHH12:MI AM "on" FMDD Mon')
         || ') has been cancelled by the warehouse, as agreed with your dispatcher.'
    ELSE 'Your dock appointment for order ' || s.order_reference || ' (dock ' || k || ', '
         || to_char(lower(a.during) AT TIME ZONE 'Asia/Kolkata', 'FMHH12:MI AM "on" FMDD Mon')
         || ') stays confirmed. The cancellation request was not approved.' END);
  RETURN to_jsonb(r);
END $$;

-- ================================================================ Telegram link-code throttling and status
CREATE TABLE driver_link_attempts (
  attempt_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  external_user_id  text NOT NULL,
  at                timestamptz NOT NULL DEFAULT clock_timestamp(),
  outcome           text NOT NULL CHECK (outcome IN ('LINKED','REPLAYED','INVALID','THROTTLED','ACCOUNT_ALREADY_LINKED',
                                                     'DRIVER_ALREADY_LINKED'))
  -- no code, no hash: a failed attempt leaves nothing that helps guess a code
);
CREATE INDEX ix_link_attempts ON driver_link_attempts (external_user_id, at DESC);
CREATE INDEX ix_link_attempts_at ON driver_link_attempts (at DESC);
ALTER TABLE driver_link_attempts ENABLE ROW LEVEL SECURITY; ALTER TABLE driver_link_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY p_driver_link_attempts ON driver_link_attempts USING (current_user = 'setuhaul_rpc')
  WITH CHECK (current_user = 'setuhaul_rpc');

-- Per account: 5 failed attempts in 15 minutes -> refused until the window passes (never indefinitely). For all
-- accounts together (Telegram is the only source address): 100 failed attempts in 5 minutes -> refused for everyone.
-- Concurrent redemptions are serialized per account and per code, so one account never links two drivers and one
-- driver never gets two accounts.
CREATE OR REPLACE FUNCTION redeem_driver_link_code(p_code_hash text, p_external_user text, p_chat_ref text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE c driver_link_codes; l driver_channel_links; v_outcome text; res jsonb;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('setuhaul.tglink:' || p_external_user, 0));
  IF (SELECT count(*) FROM driver_link_attempts WHERE external_user_id = p_external_user
        AND at > clock_timestamp() - interval '15 minutes' AND outcome IN ('INVALID','THROTTLED')) >= 5
     OR (SELECT count(*) FROM driver_link_attempts WHERE at > clock_timestamp() - interval '5 minutes'
          AND outcome IN ('INVALID','THROTTLED')) >= 100 THEN
    INSERT INTO driver_link_attempts (external_user_id, outcome) VALUES (p_external_user, 'THROTTLED');
    RETURN jsonb_build_object('linked', false, 'reason', 'TOO_MANY_ATTEMPTS');
  END IF;
  SELECT * INTO l FROM driver_channel_links WHERE channel = 'TELEGRAM' AND external_user_id = p_external_user
     AND revoked_at IS NULL;
  SELECT * INTO c FROM driver_link_codes WHERE code_hash = p_code_hash FOR UPDATE;
  IF FOUND AND l.link_id IS NOT NULL AND l.driver_id = c.driver_id AND c.consumed_at IS NOT NULL THEN
    v_outcome := 'REPLAYED'; res := jsonb_build_object('linked', true, 'driver_id', l.driver_id, 'replayed', true);
  ELSIF NOT FOUND OR c.consumed_at IS NOT NULL OR c.revoked_at IS NOT NULL OR c.expires_at <= clock_timestamp() THEN
    v_outcome := 'INVALID'; res := jsonb_build_object('linked', false, 'reason', 'INVALID_OR_EXPIRED_CODE');
  ELSIF l.link_id IS NOT NULL THEN
    v_outcome := 'ACCOUNT_ALREADY_LINKED'; res := jsonb_build_object('linked', false, 'reason', v_outcome);
  ELSE
    PERFORM pg_advisory_xact_lock(hashtextextended('setuhaul.tglink-driver:' || c.driver_id, 0));
    IF EXISTS (SELECT 1 FROM driver_channel_links WHERE driver_id = c.driver_id AND channel = 'TELEGRAM'
                AND revoked_at IS NULL) THEN
      v_outcome := 'DRIVER_ALREADY_LINKED'; res := jsonb_build_object('linked', false, 'reason', v_outcome);
    ELSE
      UPDATE driver_link_codes SET consumed_at = clock_timestamp() WHERE code_hash = p_code_hash;
      INSERT INTO driver_channel_links (driver_id, channel, external_user_id, chat_ref, link_code_hash)
      VALUES (c.driver_id, 'TELEGRAM', p_external_user, p_chat_ref, p_code_hash);
      INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
      VALUES ('driver:' || c.driver_id, 'driver', 'DRIVER_CHANNEL_LINKED', NULL, NULL,
              jsonb_build_object('driver_id', c.driver_id, 'channel', 'TELEGRAM', 'code_issued_by', c.issued_by),
              'one-time code redeemed');
      v_outcome := 'LINKED'; res := jsonb_build_object('linked', true, 'driver_id', c.driver_id, 'replayed', false);
    END IF;
  END IF;
  INSERT INTO driver_link_attempts (external_user_id, outcome) VALUES (p_external_user, v_outcome);
  RETURN res;
END $$;

-- What a dispatcher may know about drivers' Telegram links: only for drivers with a shipment in their scope (RLS on
-- shipments, read in the caller's facility scope). Never the Telegram id or chat.
CREATE FUNCTION driver_link_status(p_drivers text[]) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE out jsonb := '{}'::jsonb; drv text;
BEGIN
  FOR drv IN SELECT DISTINCT s.driver_id FROM shipments s WHERE s.driver_id = ANY (p_drivers) LOOP
    out := out || jsonb_build_object(drv, jsonb_build_object(
      'linked', EXISTS (SELECT 1 FROM driver_channel_links l WHERE l.driver_id = drv AND l.revoked_at IS NULL),
      'linked_at', (SELECT max(linked_at) FROM driver_channel_links l WHERE l.driver_id = drv AND l.revoked_at IS NULL),
      'code_expires_at', (SELECT max(expires_at) FROM driver_link_codes k WHERE k.driver_id = drv
                            AND k.consumed_at IS NULL AND k.revoked_at IS NULL AND k.expires_at > clock_timestamp())));
  END LOOP;
  RETURN out;
END $$;

-- ================================================================ Telegram outbound: routing facts for one notification
CREATE TABLE telegram_location_prompts (
  exception_id     text PRIMARY KEY REFERENCES driver_exceptions,
  notification_id  uuid NOT NULL,
  asked_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE telegram_location_prompts ENABLE ROW LEVEL SECURITY; ALTER TABLE telegram_location_prompts FORCE ROW LEVEL SECURITY;
CREATE POLICY p_telegram_location_prompts ON telegram_location_prompts USING (current_user = 'setuhaul_rpc')
  WITH CHECK (current_user = 'setuhaul_rpc');

-- For the relay, just before a send: the linked Telegram chat (if any), the open offer this message carries (buttons),
-- and whether to offer the one-off location button (once per open delay report without a shared location).
CREATE FUNCTION telegram_delivery_for(p_notification uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE n notifications; drv text; chat text; offer uuid; okind text; exc text; ask boolean := false;
  old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT * INTO n FROM notifications WHERE notification_id = p_notification;
  IF NOT FOUND OR n.recipient_ref NOT LIKE 'driver:%' THEN
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
    RETURN jsonb_build_object('chat_ref', NULL);
  END IF;
  drv := substr(n.recipient_ref, 8);
  SELECT l.chat_ref INTO chat FROM driver_channel_links l JOIN drivers d USING (driver_id)
   WHERE l.driver_id = drv AND l.channel = 'TELEGRAM' AND l.revoked_at IS NULL AND d.status <> 'INACTIVE';
  IF chat IS NOT NULL AND n.chat_message_id IS NOT NULL THEN
    SELECT o.offer_id, o.kind INTO offer, okind FROM dispatch_offers o
     WHERE o.read_back_message_id = n.chat_message_id AND o.driver_id = drv AND o.status = 'OFFERED'
       AND o.expires_at > clock_timestamp();
    IF offer IS NULL AND n.thread_id IS NOT NULL THEN
      SELECT e.exception_id INTO exc FROM driver_exceptions e JOIN chat_threads t ON t.thread_id = e.thread_id
       WHERE e.thread_id = n.thread_id AND e.driver_id = drv AND e.status = 'OPEN'
         AND e.shipment_id = t.shipment_id AND e.created_at > clock_timestamp() - interval '30 minutes'
         AND NOT EXISTS (SELECT 1 FROM driver_location_reports r WHERE r.exception_id = e.exception_id)
       ORDER BY e.created_at DESC LIMIT 1;
      IF exc IS NOT NULL THEN
        INSERT INTO telegram_location_prompts (exception_id, notification_id) VALUES (exc, p_notification)
        ON CONFLICT (exception_id) DO NOTHING;
        ask := EXISTS (SELECT 1 FROM telegram_location_prompts WHERE exception_id = exc
                        AND notification_id = p_notification);      -- the same notification on a retry
      END IF;
    END IF;
  END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN jsonb_build_object('chat_ref', chat, 'offer_id', offer, 'offer_kind', okind, 'ask_location', ask);
END $$;

-- ================================================================ escalations: acknowledge and resolve are separate
CREATE FUNCTION acknowledge_escalation(p_escalation uuid, p_note text, p_principal text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; e escalations;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','planner','ops_lead'], 'acknowledging an escalation');
  PERFORM check_principal(ctx, p_principal);
  SELECT * INTO e FROM escalations WHERE escalation_id = p_escalation FOR UPDATE;
  IF NOT FOUND OR NOT (coalesce(e.facility_id, 'CENTRAL') = ANY (ctx.facilities || ARRAY['CENTRAL'])) THEN
    RAISE EXCEPTION 'ESCALATION_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF e.status <> 'OPEN' THEN RAISE EXCEPTION 'ALREADY_%', e.status USING ERRCODE = 'P0001'; END IF;
  UPDATE escalations SET status = 'ACKNOWLEDGED', acknowledged_at = clock_timestamp(), assigned_to = ctx.principal
   WHERE escalation_id = p_escalation RETURNING * INTO e;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'ESCALATION_ACKNOWLEDGED', e.facility_id, e.shipment_id,
          jsonb_build_object('escalation_id', e.escalation_id, 'reason_code', e.reason_code), p_note);
  RETURN jsonb_build_object('escalation_id', e.escalation_id, 'status', e.status, 'assigned_to', e.assigned_to);
END $$;

CREATE FUNCTION resolve_escalation(p_escalation uuid, p_resolution text, p_principal text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; e escalations;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','planner','ops_lead'], 'resolving an escalation');
  PERFORM check_principal(ctx, p_principal);
  IF p_resolution IS NULL OR length(p_resolution) NOT BETWEEN 5 AND 500 THEN
    RAISE EXCEPTION 'RESOLUTION_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO e FROM escalations WHERE escalation_id = p_escalation FOR UPDATE;
  IF NOT FOUND OR NOT (coalesce(e.facility_id, 'CENTRAL') = ANY (ctx.facilities || ARRAY['CENTRAL'])) THEN
    RAISE EXCEPTION 'ESCALATION_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF e.status IN ('RESOLVED','REROUTED') THEN RAISE EXCEPTION 'ALREADY_%', e.status USING ERRCODE = 'P0001'; END IF;
  UPDATE escalations SET status = 'RESOLVED', resolved_at = clock_timestamp(),
         acknowledged_at = coalesce(acknowledged_at, clock_timestamp()), assigned_to = coalesce(assigned_to, ctx.principal),
         resolution = jsonb_build_object('by', ctx.principal, 'role', role, 'text', p_resolution)
   WHERE escalation_id = p_escalation RETURNING * INTO e;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'ESCALATION_RESOLVED', e.facility_id, e.shipment_id,
          jsonb_build_object('escalation_id', e.escalation_id), p_resolution);
  RETURN jsonb_build_object('escalation_id', e.escalation_id, 'status', e.status);
END $$;

-- ================================================================ the driver's open offer (verified inbound path)
-- A typed reply on Telegram answers the offer the driver is looking at: their newest live offer, and only while it is
-- the latest read-back in their conversation. Their own offers only.
CREATE FUNCTION open_offer_for_driver(p_driver text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE r uuid; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT o.offer_id INTO r FROM dispatch_offers o
   WHERE o.driver_id = p_driver AND o.status = 'OFFERED' AND o.expires_at > clock_timestamp()
     AND o.selection_id = (SELECT max(selection_id) FROM option_selections WHERE thread_id = o.thread_id)
   ORDER BY o.created_at DESC LIMIT 1;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN r;
END $$;

-- ================================================================ ownership and privileges
ALTER FUNCTION notify_driver(text, text, text)                                OWNER TO setuhaul_rpc;
ALTER FUNCTION driver_trip_conflict(text, text, tstzrange)                     OWNER TO setuhaul_rpc;
ALTER FUNCTION create_reassignment_offer(text, text, integer, text, text, text) OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_dispatch_offer_attested()                                  OWNER TO setuhaul_rpc;
ALTER FUNCTION request_cancellation(text, text, text, text, text)             OWNER TO setuhaul_rpc;
ALTER FUNCTION decide_cancellation(uuid, text, text, text)                    OWNER TO setuhaul_rpc;
ALTER FUNCTION redeem_driver_link_code(text, text, text)                      OWNER TO setuhaul_rpc;
ALTER FUNCTION driver_link_status(text[])                                     OWNER TO setuhaul_rpc;
ALTER FUNCTION telegram_delivery_for(uuid)                                    OWNER TO setuhaul_rpc;
ALTER FUNCTION acknowledge_escalation(uuid, text, text)                       OWNER TO setuhaul_rpc;
ALTER FUNCTION resolve_escalation(uuid, text, text)                           OWNER TO setuhaul_rpc;
ALTER FUNCTION open_offer_for_driver(text)                                    OWNER TO setuhaul_rpc;

GRANT SELECT, INSERT, UPDATE ON cancellation_requests TO setuhaul_rpc;
GRANT SELECT ON cancellation_requests TO setuhaul_console;
GRANT SELECT, INSERT ON driver_link_attempts, telegram_location_prompts TO setuhaul_rpc;
GRANT SELECT ON notifications, driver_location_reports, cancellation_requests TO setuhaul_rpc;
GRANT SELECT, UPDATE ON escalations TO setuhaul_rpc;
GRANT UPDATE (status, is_current) ON appointments TO setuhaul_rpc;
GRANT UPDATE (active) ON dock_occupancy TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION create_reassignment_offer(text, text, integer, text, text, text) TO setuhaul_console;
GRANT EXECUTE ON FUNCTION request_cancellation(text, text, text, text, text)             TO setuhaul_console;
GRANT EXECUTE ON FUNCTION decide_cancellation(uuid, text, text, text)                    TO setuhaul_console;
GRANT EXECUTE ON FUNCTION driver_link_status(text[])                                     TO setuhaul_console;
GRANT EXECUTE ON FUNCTION acknowledge_escalation(uuid, text, text)                       TO setuhaul_console;
GRANT EXECUTE ON FUNCTION resolve_escalation(uuid, text, text)                           TO setuhaul_console;
GRANT EXECUTE ON FUNCTION telegram_delivery_for(uuid)                                    TO setuhaul_system;
GRANT EXECUTE ON FUNCTION open_offer_for_driver(text)                                    TO setuhaul_ingest;
