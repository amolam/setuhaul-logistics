-- 0035 · Dispatcher-initiated booking offers, pre-confirmation reassignment, driver location reports, Telegram links.
-- Forward-only; 0001–0034 untouched. Roadmap "Accelerated Implementation" (25 Nov 2026), Phases 1–5 backend.
--
-- Design (no second lifecycle):
--   * A dispatcher offer IS a read-back in the driver's own conversation: one shown_options row, one option_selections
--     row and one AGENT message (which the 0017 outbox trigger delivers through the existing relay / test sink). The
--     driver's reply is attested by the existing inbound path (attest_confirmation), and the booking continues through
--     the existing hold_option -> submit_booking_request -> planner approval. Nothing here creates or confirms an
--     appointment, and sending an offer reserves no dock capacity.
--   * dispatch_offers records the offer's own state (OFFERED / ACCEPTED / DECLINED / ALTERNATIVE_REQUESTED / EXPIRED /
--     SUPERSEDED / UNAVAILABLE). Expiry is the shown option's expires_at (SETUHAUL_OPTION_TTL_S, default 300 s), so an
--     expired or superseded offer is refused by the same checks that refuse a stale driver-chat option.
--   * Temporary role mapping (owner decision, 25 Nov 2026): dispatcher actions use the existing `operations` role (and
--     ops_lead). A booking that comes from a dispatcher offer is decided as PLANNER_REVIEW (planner / ops_lead only),
--     so the operations role that sends offers can never approve them. Review when a dispatcher group is introduced.
--   * Pre-confirmation reassignment: the offer goes to the NEW driver; the shipment keeps its current driver until the
--     new driver's explicit yes, which switches the assignment atomically in the attestation's transaction. A decline
--     or expiry changes nothing. Confirmed appointments are out of scope here (refused).
--   * Location: an explicit, one-off share, stored coarsely (geohash, ~150 m) with its assessment; never continuous.
--   * Telegram: identity is a one-time, expiring, hashed link code issued by staff, bound to Telegram's numeric user id;
--     usernames are never stored or trusted.
SET search_path = setuhaul, public;

-- ================================================================ channels
ALTER TABLE chat_messages DROP CONSTRAINT chat_messages_channel_check;
ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_channel_check
  CHECK (channel IN ('WHATSAPP','WEB','SMS','CONSOLE','TELEGRAM'));

-- ================================================================ dispatch offers
CREATE TABLE dispatch_offers (
  offer_id            uuid PRIMARY KEY,
  kind                text NOT NULL CHECK (kind IN ('INITIAL_BOOKING','REASSIGNMENT')),
  shipment_id         text NOT NULL REFERENCES shipments,
  facility_id         text NOT NULL REFERENCES facilities,
  driver_id           text NOT NULL REFERENCES drivers,          -- the driver the offer is FOR
  previous_driver_id  text REFERENCES drivers,                   -- REASSIGNMENT: the driver assigned when it was sent
  vehicle_id          text NOT NULL REFERENCES vehicles,
  thread_id           text NOT NULL REFERENCES chat_threads,
  option_id           uuid NOT NULL REFERENCES shown_options,
  selection_id        bigint NOT NULL REFERENCES option_selections,
  read_back_message_id bigint NOT NULL REFERENCES chat_messages,
  status              text NOT NULL DEFAULT 'OFFERED'
                      CHECK (status IN ('OFFERED','ACCEPTED','DECLINED','ALTERNATIVE_REQUESTED','EXPIRED','SUPERSEDED',
                                        'UNAVAILABLE')),
  status_reason       text,
  offered_by          text NOT NULL,
  offered_by_role     text NOT NULL,
  idem_key            text NOT NULL UNIQUE,
  expires_at          timestamptz NOT NULL,
  responded_at        timestamptz,
  response_message_id bigint REFERENCES chat_messages,
  superseded_by       uuid REFERENCES dispatch_offers DEFERRABLE INITIALLY DEFERRED,
  appointment_id      text REFERENCES appointments,              -- set when the accepted offer becomes a request
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((kind = 'REASSIGNMENT') = (previous_driver_id IS NOT NULL)),
  CHECK (status = 'OFFERED' OR status = 'EXPIRED' OR status = 'SUPERSEDED' OR responded_at IS NOT NULL
         OR status = 'UNAVAILABLE'),
  CHECK (status <> 'SUPERSEDED' OR superseded_by IS NOT NULL)
);
-- at most one live offer per shipment: a new offer supersedes the old one in the same transaction
CREATE UNIQUE INDEX ux_one_open_offer_per_shipment ON dispatch_offers (shipment_id) WHERE status = 'OFFERED';
CREATE UNIQUE INDEX ux_offer_selection ON dispatch_offers (selection_id);
CREATE INDEX ix_offers_facility ON dispatch_offers (facility_id, created_at DESC);
ALTER TABLE dispatch_offers ENABLE ROW LEVEL SECURITY; ALTER TABLE dispatch_offers FORCE ROW LEVEL SECURITY;
CREATE POLICY p_dispatch_offers ON dispatch_offers
  USING (scope_allows(facility_id) OR driver_id = current_setting('app.driver_id', true))
  WITH CHECK (scope_allows(facility_id));

CREATE FUNCTION dispatch_offer_json(o dispatch_offers) RETURNS jsonb LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT jsonb_build_object('offer_id', o.offer_id, 'kind', o.kind, 'shipment_id', o.shipment_id,
           'facility_id', o.facility_id, 'driver_id', o.driver_id, 'previous_driver_id', o.previous_driver_id,
           'vehicle_id', o.vehicle_id, 'thread_id', o.thread_id, 'option_id', o.option_id,
           'selection_id', o.selection_id, 'read_back_message_id', o.read_back_message_id,
           -- an OFFERED row past its expiry IS expired: expiry needs no background worker
           'status', CASE WHEN o.status = 'OFFERED' AND o.expires_at <= clock_timestamp() THEN 'EXPIRED' ELSE o.status END,
           'status_reason', o.status_reason, 'expires_at', o.expires_at, 'responded_at', o.responded_at,
           'superseded_by', o.superseded_by, 'appointment_id', o.appointment_id, 'offered_by', o.offered_by,
           'created_at', o.created_at)
$$;

CREATE FUNCTION all_facility_scope() RETURNS text LANGUAGE sql STABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT coalesce(string_agg(facility_id, ','), '') || ',CENTRAL' FROM facilities
$$;

-- ---------------------------------------------------------------- create (console: operations / ops_lead)
CREATE FUNCTION create_dispatch_offer(p_shipment text, p_driver text, p_vehicle text, p_dock text, p_start timestamptz,
                                      p_end timestamptz, p_needs_approval boolean, p_schedule_version bigint,
                                      p_ttl_s integer, p_text text, p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; s shipments; d drivers; t chat_threads; o dispatch_offers; prev record;
  k text; oid uuid := gen_random_uuid(); offer uuid := gen_random_uuid(); mid bigint; sel bigint; fstate text;
  dock_fac text; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'sending a booking offer');
  PERFORM check_principal(ctx, p_principal);
  IF p_idem_key IS NULL OR length(p_idem_key) NOT BETWEEN 8 AND 200 THEN
    RAISE EXCEPTION 'BAD_IDEMPOTENCY_KEY' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO o FROM dispatch_offers WHERE idem_key = p_idem_key;
  IF FOUND THEN                                                    -- a retried request: the same answer, nothing new
    IF o.shipment_id IS DISTINCT FROM p_shipment OR o.driver_id IS DISTINCT FROM p_driver THEN
      RAISE EXCEPTION 'IDEMPOTENCY_KEY_REUSED' USING ERRCODE = 'P0001'; END IF;
    RETURN dispatch_offer_json(o) || jsonb_build_object('replayed', true);
  END IF;
  IF p_text IS NULL OR length(p_text) NOT BETWEEN 10 AND 1000 THEN
    RAISE EXCEPTION 'BAD_OFFER_TEXT' USING ERRCODE = 'P0001'; END IF;
  IF p_ttl_s IS NULL OR p_ttl_s NOT BETWEEN 30 AND 3600 THEN
    RAISE EXCEPTION 'BAD_OFFER_TTL' USING ERRCODE = 'P0001'; END IF;
  IF p_start IS NULL OR p_end IS NULL OR p_end <= p_start OR p_start <= clock_timestamp() THEN
    RAISE EXCEPTION 'BAD_SLOT' USING ERRCODE = 'P0001'; END IF;

  SELECT * INTO s FROM shipments WHERE shipment_id = p_shipment FOR UPDATE;        -- staff scope (RLS) applies here
  IF NOT FOUND OR NOT (s.destination_facility_id = ANY (ctx.facilities)) THEN
    RAISE EXCEPTION 'SHIPMENT_UNKNOWN' USING ERRCODE = 'P0001'; END IF;
  IF s.current_status NOT IN ('PLANNED','ASSIGNED','IN_TRANSIT') THEN
    RAISE EXCEPTION 'SHIPMENT_NOT_BOOKABLE: status %', s.current_status USING ERRCODE = 'P0001'; END IF;
  IF p_vehicle IS DISTINCT FROM s.vehicle_id THEN                                   -- vehicle changes: not in this scope
    RAISE EXCEPTION 'VEHICLE_CHANGE_NOT_SUPPORTED' USING ERRCODE = 'P0001'; END IF;
  SELECT facility_id INTO dock_fac FROM docks WHERE dock_id = p_dock;
  IF dock_fac IS DISTINCT FROM s.destination_facility_id THEN
    RAISE EXCEPTION 'DOCK_NOT_AT_FACILITY' USING ERRCODE = 'P0001'; END IF;

  -- from here the function reads the driver's conversation, which may be bound to no shipment (CENTRAL) yet
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  IF EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = p_shipment
              AND (a.is_current OR a.status = 'PENDING_CONFIRMATION')
              AND a.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')) THEN
    RAISE EXCEPTION 'SHIPMENT_ALREADY_BOOKED: initial booking only; use reschedule or move' USING ERRCODE = 'P0001'; END IF;
  IF EXISTS (SELECT 1 FROM dock_occupancy x WHERE x.shipment_id = p_shipment AND x.kind = 'HOLD' AND x.active
              AND x.expires_at > clock_timestamp()) THEN
    RAISE EXCEPTION 'BOOKING_IN_PROGRESS: a slot is being held for this shipment' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO d FROM drivers WHERE driver_id = p_driver;
  IF NOT FOUND OR d.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'DRIVER_NOT_AVAILABLE' USING ERRCODE = 'P0001'; END IF;
  k := CASE WHEN p_driver = s.driver_id THEN 'INITIAL_BOOKING' ELSE 'REASSIGNMENT' END;

  -- the driver's open conversation (as the inbound path finds it), or a new one bound to this shipment
  SELECT * INTO t FROM chat_threads WHERE driver_id = p_driver AND status NOT IN ('CLOSED','RESOLVED')
   ORDER BY opened_at DESC LIMIT 1;
  IF NOT FOUND THEN
    INSERT INTO chat_threads (thread_id, driver_id, shipment_id, status)
    VALUES ('THR-' || substr(md5(gen_random_uuid()::text), 1, 12), p_driver, p_shipment, 'OPEN') RETURNING * INTO t;
  END IF;
  PERFORM thread_lock(t.thread_id);                                 -- serialized with replies, holds and requests
  SELECT * INTO t FROM chat_threads WHERE thread_id = t.thread_id;
  IF t.human_active THEN RAISE EXCEPTION 'HUMAN_ACTIVE: a coordinator owns this conversation' USING ERRCODE = 'P0001'; END IF;
  SELECT state INTO fstate FROM thread_flow WHERE thread_id = t.thread_id;
  IF t.shipment_id IS DISTINCT FROM p_shipment THEN
    IF fstate IN ('CONFIRMED_BY_DRIVER','HELD','SUBMITTED') THEN
      RAISE EXCEPTION 'DRIVER_BUSY: the driver is finishing a booking for another shipment' USING ERRCODE = 'P0001'; END IF;
    UPDATE chat_threads SET shipment_id = p_shipment WHERE thread_id = t.thread_id;
  ELSIF fstate IN ('CONFIRMED_BY_DRIVER','HELD','SUBMITTED') THEN
    RAISE EXCEPTION 'BOOKING_IN_PROGRESS: the driver is already booking this shipment' USING ERRCODE = 'P0001'; END IF;

  -- an older live offer for this shipment: expired if past its time, else superseded by this one
  UPDATE dispatch_offers SET status = 'EXPIRED', status_reason = 'expired before a newer offer'
   WHERE shipment_id = p_shipment AND status = 'OFFERED' AND expires_at <= clock_timestamp();
  FOR prev IN SELECT offer_id, option_id FROM dispatch_offers WHERE shipment_id = p_shipment AND status = 'OFFERED' FOR UPDATE LOOP
    UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp()) WHERE option_id = prev.option_id;
  END LOOP;
  -- superseded before the new row exists (one live offer per shipment); the reference is checked at commit
  UPDATE dispatch_offers SET status = 'SUPERSEDED', superseded_by = offer, status_reason = 'a newer offer was sent'
   WHERE shipment_id = p_shipment AND status = 'OFFERED';

  INSERT INTO shown_options (option_id, thread_id, shipment_id, dock_id, during, needs_approval, rank, schedule_version,
                             expires_at, created_at)
  VALUES (oid, t.thread_id, p_shipment, p_dock, tstzrange(p_start, p_end, '[)'), coalesce(p_needs_approval, false), 1,
          coalesce(p_schedule_version, 0), clock_timestamp() + make_interval(secs => p_ttl_s), now());
  INSERT INTO chat_messages (thread_id, sender_type, channel, content_hash, body_masked)
  VALUES (t.thread_id, 'AGENT', 'WHATSAPP', encode(sha256(convert_to(p_text, 'UTF8')), 'hex'), p_text)
  RETURNING message_id INTO mid;
  -- delivered like the 0022 hand-back notice: the reply trigger (0018) sends only answers to a driver message, so the
  -- offer is queued explicitly, and the relay sends the stored text checked against this hash (test sink in the pilot)
  INSERT INTO outbox (facility_id, aggregate, aggregate_id, event_type, payload)
  VALUES (s.destination_facility_id, 'chat_message', mid::text, 'AgentReplyStored',
          jsonb_build_object('message_id', mid, 'thread_id', t.thread_id, 'driver_id', p_driver,
                             'content_hash', encode(sha256(convert_to(p_text, 'UTF8')), 'hex'), 'channel', 'WHATSAPP'));
  INSERT INTO option_selections (thread_id, option_id, read_back_message_id) VALUES (t.thread_id, oid, mid)
  RETURNING selection_id INTO sel;
  INSERT INTO thread_flow (thread_id, state, selected_option) VALUES (t.thread_id, 'SELECTED', oid)
  ON CONFLICT (thread_id) DO UPDATE SET state = 'SELECTED', selected_option = oid, yes_message_id = NULL,
     hold_option = NULL, appointment_id = NULL, attestation_id = NULL, version = thread_flow.version + 1, updated_at = now();

  INSERT INTO dispatch_offers (offer_id, kind, shipment_id, facility_id, driver_id, previous_driver_id, vehicle_id,
                               thread_id, option_id, selection_id, read_back_message_id, offered_by, offered_by_role,
                               idem_key, expires_at)
  VALUES (offer, k, p_shipment, s.destination_facility_id, p_driver,
          CASE WHEN k = 'REASSIGNMENT' THEN s.driver_id END, s.vehicle_id, t.thread_id, oid, sel, mid, ctx.principal, role,
          p_idem_key, clock_timestamp() + make_interval(secs => p_ttl_s));
  SELECT * INTO o FROM dispatch_offers WHERE offer_id = offer;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'DISPATCH_OFFER_SENT', s.destination_facility_id, p_shipment,
          jsonb_build_object('offer_id', offer, 'kind', k, 'driver_id', p_driver, 'dock_id', p_dock,
                             'start', p_start, 'end', p_end, 'expires_at', o.expires_at), 'dispatcher offer');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN dispatch_offer_json(o) || jsonb_build_object('replayed', false);
END $$;

-- ---------------------------------------------------------------- the driver's reply (attestation) moves the offer
CREATE FUNCTION trg_dispatch_offer_attested() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o dispatch_offers; s shipments; old_scope text := current_setting('app.facility_scope', true);
  old_driver text := current_setting('app.driver_id', true);
BEGIN
  IF NEW.verdict NOT IN ('YES','NO') THEN RETURN NULL; END IF;     -- an unclear reply is never guessed
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  -- the driver's session sees only their own shipments; the re-check must see the shipment as it is (still the
  -- previous driver's, during a reassignment). Restored before returning.
  PERFORM set_config('app.driver_id', '', true);
  SELECT * INTO o FROM dispatch_offers WHERE selection_id = NEW.selection_id FOR UPDATE;
  IF NOT FOUND OR o.status <> 'OFFERED' THEN
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
    PERFORM set_config('app.driver_id', coalesce(old_driver, ''), true);
    RETURN NULL;
  END IF;
  IF o.expires_at <= clock_timestamp() THEN
    UPDATE dispatch_offers SET status = 'EXPIRED', status_reason = 'reply after expiry' WHERE offer_id = o.offer_id;
  ELSIF NEW.verdict = 'NO' THEN
    UPDATE dispatch_offers SET status = 'DECLINED', responded_at = clock_timestamp(),
           response_message_id = NEW.confirmation_message_id WHERE offer_id = o.offer_id;
  ELSE
    IF o.kind = 'REASSIGNMENT' THEN
      SELECT * INTO s FROM shipments WHERE shipment_id = o.shipment_id FOR UPDATE;
      IF s.driver_id IS DISTINCT FROM o.previous_driver_id
         OR EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = o.shipment_id
                     AND (a.is_current OR a.status = 'PENDING_CONFIRMATION')
                     AND a.status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'))
         OR EXISTS (SELECT 1 FROM dock_occupancy x WHERE x.shipment_id = o.shipment_id AND x.kind = 'HOLD' AND x.active
                     AND x.expires_at > clock_timestamp()) THEN
        -- the situation changed since the offer: the yes is recorded, the assignment is not touched
        UPDATE dispatch_offers SET status = 'UNAVAILABLE', responded_at = clock_timestamp(),
               response_message_id = NEW.confirmation_message_id,
               status_reason = 'assignment or booking changed after the offer' WHERE offer_id = o.offer_id;
        UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp()) WHERE option_id = o.option_id;
        PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
    PERFORM set_config('app.driver_id', coalesce(old_driver, ''), true);
        RETURN NULL;
      END IF;
      -- the switch: atomically with the new driver's explicit yes. The previous driver's conversation lets go of
      -- the shipment, so nothing they send can act on it any more.
      UPDATE shipments SET driver_id = o.driver_id, updated_at = now() WHERE shipment_id = o.shipment_id;
      UPDATE chat_threads SET shipment_id = NULL WHERE shipment_id = o.shipment_id AND driver_id = o.previous_driver_id;
      INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, before_state, after_state, reason)
      VALUES ('driver:' || o.driver_id, 'driver', 'SHIPMENT_REASSIGNED', o.facility_id, o.shipment_id,
              jsonb_build_object('driver_id', o.previous_driver_id), jsonb_build_object('driver_id', o.driver_id),
              'new driver accepted offer ' || o.offer_id);
    END IF;
    UPDATE dispatch_offers SET status = 'ACCEPTED', responded_at = clock_timestamp(),
           response_message_id = NEW.confirmation_message_id WHERE offer_id = o.offer_id;
  END IF;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES ('driver:' || o.driver_id, 'driver', 'DISPATCH_OFFER_ANSWERED', o.facility_id, o.shipment_id,
          jsonb_build_object('offer_id', o.offer_id, 'verdict', NEW.verdict,
                             'status', (SELECT status FROM dispatch_offers WHERE offer_id = o.offer_id)), 'driver reply');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
    PERFORM set_config('app.driver_id', coalesce(old_driver, ''), true);
  RETURN NULL;
END $$;
CREATE TRIGGER dispatch_offer_attested AFTER INSERT ON confirmation_attestations
  FOR EACH ROW EXECUTE FUNCTION trg_dispatch_offer_attested();

-- ---------------------------------------------------------------- a dispatcher booking is decided by a planner
CREATE FUNCTION trg_dispatch_booking_needs_planner() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE oid uuid; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  IF NEW.appointment_id IS NULL THEN RETURN NEW; END IF;
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT o.offer_id INTO oid
    FROM dispatch_offers o JOIN shown_options so ON so.option_id = o.option_id
    JOIN appointments a ON a.appointment_id = NEW.appointment_id
   WHERE o.shipment_id = a.shipment_id AND o.status = 'ACCEPTED' AND o.appointment_id IS NULL
     AND so.dock_id = a.dock_id AND so.during = a.during
   ORDER BY o.responded_at DESC LIMIT 1;
  IF oid IS NOT NULL THEN
    UPDATE dispatch_offers SET appointment_id = NEW.appointment_id WHERE offer_id = oid;
    -- the operations role sends dispatcher offers, so it never decides them (temporary role mapping)
    IF NEW.kind = 'STANDARD' THEN NEW.kind := 'PLANNER_REVIEW'; END IF;
  END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN NEW;
END $$;
CREATE TRIGGER dispatch_booking_needs_planner BEFORE INSERT ON approval_requests
  FOR EACH ROW EXECUTE FUNCTION trg_dispatch_booking_needs_planner();

-- ---------------------------------------------------------------- the verified inbound path (ingest role)
-- What a driver may know about an offer: only their own, and nothing about another driver's (NOT_FOUND either way).
CREATE FUNCTION dispatch_offer_for_driver(p_offer uuid, p_driver text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o dispatch_offers; latest bigint; old_scope text := current_setting('app.facility_scope', true); r jsonb;
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT * INTO o FROM dispatch_offers WHERE offer_id = p_offer;
  IF NOT FOUND OR o.driver_id IS DISTINCT FROM p_driver THEN
    r := jsonb_build_object('status', 'NOT_FOUND');
  ELSE
    SELECT max(selection_id) INTO latest FROM option_selections WHERE thread_id = o.thread_id;
    r := dispatch_offer_json(o) || jsonb_build_object('is_latest_selection', latest = o.selection_id);
  END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN r;
END $$;

CREATE FUNCTION request_offer_alternative(p_offer uuid, p_driver text, p_message bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE o dispatch_offers; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT * INTO o FROM dispatch_offers WHERE offer_id = p_offer FOR UPDATE;
  IF NOT FOUND OR o.driver_id IS DISTINCT FROM p_driver THEN
    RAISE EXCEPTION 'OFFER_NOT_FOUND' USING ERRCODE = 'P0001'; END IF;
  PERFORM thread_lock(o.thread_id);
  IF o.status = 'ALTERNATIVE_REQUESTED' THEN                       -- a repeated tap: the same answer
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
    RETURN dispatch_offer_json(o) || jsonb_build_object('replayed', true);
  END IF;
  IF o.status <> 'OFFERED' OR o.expires_at <= clock_timestamp() THEN
    RAISE EXCEPTION 'OFFER_NOT_OPEN: %', dispatch_offer_json(o) ->> 'status' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM chat_messages m WHERE m.message_id = p_message AND m.thread_id = o.thread_id AND m.sender_type = 'DRIVER'
     AND m.message_id > o.read_back_message_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'MESSAGE_NOT_FROM_DRIVER' USING ERRCODE = 'P0001'; END IF;
  UPDATE dispatch_offers SET status = 'ALTERNATIVE_REQUESTED', responded_at = clock_timestamp(),
         response_message_id = p_message WHERE offer_id = o.offer_id RETURNING * INTO o;
  UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp()) WHERE option_id = o.option_id;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES ('driver:' || p_driver, 'driver', 'DISPATCH_OFFER_ANSWERED', o.facility_id, o.shipment_id,
          jsonb_build_object('offer_id', o.offer_id, 'status', o.status), 'driver asked for another time');
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN dispatch_offer_json(o) || jsonb_build_object('replayed', false);
END $$;

-- ================================================================ driver location reports (explicit, one-off)
ALTER TABLE facilities
  ADD COLUMN latitude  double precision CHECK (latitude BETWEEN -90 AND 90),
  ADD COLUMN longitude double precision CHECK (longitude BETWEEN -180 AND 180),
  ADD CONSTRAINT facilities_position_both CHECK ((latitude IS NULL) = (longitude IS NULL));

CREATE TABLE driver_location_reports (
  report_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  driver_id            text NOT NULL REFERENCES drivers,
  thread_id            text NOT NULL REFERENCES chat_threads,
  shipment_id          text REFERENCES shipments,
  exception_id         text REFERENCES driver_exceptions,
  source               text NOT NULL CHECK (source IN ('TELEGRAM','TEST_SINK')),
  dedupe_key           text NOT NULL UNIQUE,
  shared               boolean NOT NULL,                   -- false: the driver declined to share (still recorded)
  observed_at          timestamptz,
  received_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  accuracy_m           numeric CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
  position_geohash     text CHECK (position_geohash IS NULL OR position_geohash ~ '^[0-9b-hjkmnp-z]{1,7}$'),
  distance_to_facility_km numeric,
  assessment           text NOT NULL CHECK (assessment IN ('VERIFIED','NEEDS_REVIEW','UNAVAILABLE','INVALID')),
  reasons              text[] NOT NULL DEFAULT '{}',
  assessor_version     text NOT NULL,
  CHECK (shared OR (position_geohash IS NULL AND assessment = 'UNAVAILABLE'))
);
CREATE INDEX ix_location_shipment ON driver_location_reports (shipment_id, received_at DESC);
ALTER TABLE driver_location_reports ENABLE ROW LEVEL SECURITY; ALTER TABLE driver_location_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY p_driver_location_reports ON driver_location_reports
  USING (driver_or_staff(driver_id, shipment_id)) WITH CHECK (driver_or_staff(driver_id, shipment_id));

-- What the assessor needs, for the driver's own conversation only: the bound shipment, its facility position (if
-- recorded), the driver's latest declared ETA and the previous shared fix. Nothing is invented when a fact is missing.
CREATE FUNCTION driver_location_context(p_driver text, p_thread text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; s shipments; f facilities; eta timestamptz; prev driver_location_reports;
  old_scope text := current_setting('app.facility_scope', true); r jsonb;
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND OR t.driver_id IS DISTINCT FROM p_driver THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO s FROM shipments WHERE shipment_id = t.shipment_id AND driver_id = p_driver;
  IF NOT FOUND THEN
    r := jsonb_build_object('shipment_id', NULL);
  ELSE
    SELECT * INTO f FROM facilities WHERE facility_id = s.destination_facility_id;
    SELECT derived_eta INTO eta FROM eta_signals WHERE shipment_id = s.shipment_id AND source = 'DRIVER_DECLARED'
     ORDER BY observed_at DESC LIMIT 1;
    SELECT * INTO prev FROM driver_location_reports WHERE shipment_id = s.shipment_id AND shared
       AND position_geohash IS NOT NULL ORDER BY observed_at DESC NULLS LAST LIMIT 1;
    r := jsonb_build_object('shipment_id', s.shipment_id, 'facility_id', f.facility_id,
           'facility_latitude', f.latitude, 'facility_longitude', f.longitude, 'declared_eta', eta,
           'original_eta', s.original_eta_ts, 'shipment_status', s.current_status,
           'previous_geohash', prev.position_geohash, 'previous_observed_at', prev.observed_at);
  END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN r;
END $$;

CREATE FUNCTION record_driver_location(p_driver text, p_thread text, p_dedupe text, p_report jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE t chat_threads; sid text; exc text; rid bigint; old_scope text := current_setting('app.facility_scope', true);
  r driver_location_reports;
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT * INTO t FROM chat_threads WHERE thread_id = p_thread;
  IF NOT FOUND OR t.driver_id IS DISTINCT FROM p_driver THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  -- only the driver's OWN bound shipment, and only while it is theirs
  SELECT shipment_id INTO sid FROM shipments WHERE shipment_id = t.shipment_id AND driver_id = p_driver;
  IF sid IS NOT NULL THEN
    SELECT exception_id INTO exc FROM driver_exceptions WHERE shipment_id = sid AND driver_id = p_driver
       AND thread_id = p_thread AND status = 'OPEN' ORDER BY created_at DESC LIMIT 1;
  END IF;
  INSERT INTO driver_location_reports (driver_id, thread_id, shipment_id, exception_id, source, dedupe_key, shared,
                                       observed_at, accuracy_m, position_geohash, distance_to_facility_km, assessment,
                                       reasons, assessor_version)
  VALUES (p_driver, p_thread, sid, exc, p_report ->> 'source', p_dedupe, (p_report ->> 'shared')::boolean,
          (p_report ->> 'observed_at')::timestamptz, (p_report ->> 'accuracy_m')::numeric, p_report ->> 'position_geohash',
          (p_report ->> 'distance_to_facility_km')::numeric,
          CASE WHEN sid IS NULL AND p_report ->> 'assessment' = 'VERIFIED' THEN 'NEEDS_REVIEW' ELSE p_report ->> 'assessment' END,
          ARRAY(SELECT jsonb_array_elements_text(coalesce(p_report -> 'reasons', '[]'::jsonb)))
            || CASE WHEN sid IS NULL THEN ARRAY['NO_SHIPMENT_IN_CONVERSATION'] ELSE ARRAY[]::text[] END,
          coalesce(p_report ->> 'assessor_version', 'unknown'))
  ON CONFLICT (dedupe_key) DO NOTHING RETURNING report_id INTO rid;
  SELECT * INTO r FROM driver_location_reports WHERE dedupe_key = p_dedupe;
  IF r.driver_id IS DISTINCT FROM p_driver THEN RAISE EXCEPTION 'FORBIDDEN' USING ERRCODE = 'P0001'; END IF;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN jsonb_build_object('report_id', r.report_id, 'duplicate', rid IS NULL, 'shipment_id', r.shipment_id,
                            'exception_id', r.exception_id, 'assessment', r.assessment, 'reasons', to_jsonb(r.reasons));
END $$;

-- ================================================================ Telegram identity (one bot, many drivers)
CREATE TABLE driver_link_codes (
  code_hash      text PRIMARY KEY CHECK (code_hash ~ '^[0-9a-f]{64}$'),   -- sha256 of the code; the code is never stored
  driver_id      text NOT NULL REFERENCES drivers,
  channel        text NOT NULL CHECK (channel IN ('TELEGRAM')),
  issued_by      text NOT NULL,
  issued_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at     timestamptz NOT NULL,
  consumed_at    timestamptz,
  revoked_at     timestamptz
);
CREATE TABLE driver_channel_links (
  link_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  driver_id        text NOT NULL REFERENCES drivers,
  channel          text NOT NULL CHECK (channel IN ('TELEGRAM')),
  external_user_id text NOT NULL CHECK (external_user_id ~ '^[0-9]{1,20}$'),   -- Telegram's numeric user id, never a username
  chat_ref         text NOT NULL CHECK (chat_ref ~ '^-?[0-9]{1,20}$'),
  linked_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  link_code_hash   text REFERENCES driver_link_codes,
  revoked_at       timestamptz,
  revoked_by       text
);
CREATE UNIQUE INDEX ux_link_external ON driver_channel_links (channel, external_user_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX ux_link_driver ON driver_channel_links (driver_id, channel) WHERE revoked_at IS NULL;
-- no application role has any privilege on either table; only the definer functions below use them
ALTER TABLE driver_link_codes ENABLE ROW LEVEL SECURITY; ALTER TABLE driver_link_codes FORCE ROW LEVEL SECURITY;
CREATE POLICY p_driver_link_codes ON driver_link_codes USING (current_user = 'setuhaul_rpc')
  WITH CHECK (current_user = 'setuhaul_rpc');
ALTER TABLE driver_channel_links ENABLE ROW LEVEL SECURITY; ALTER TABLE driver_channel_links FORCE ROW LEVEL SECURITY;
CREATE POLICY p_driver_channel_links ON driver_channel_links USING (current_user = 'setuhaul_rpc')
  WITH CHECK (current_user = 'setuhaul_rpc');

CREATE FUNCTION issue_driver_link_code(p_driver text, p_code_hash text, p_ttl_s integer, p_principal text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; exp timestamptz;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'linking a driver''s Telegram account');
  PERFORM check_principal(ctx, p_principal);
  IF p_ttl_s IS NULL OR p_ttl_s NOT BETWEEN 60 AND 86400 THEN RAISE EXCEPTION 'BAD_TTL' USING ERRCODE = 'P0001'; END IF;
  -- staff may link only a driver who carries a live shipment to one of their facilities (RLS on shipments applies)
  PERFORM 1 FROM shipments s JOIN drivers d USING (driver_id)
   WHERE s.driver_id = p_driver AND d.status = 'ACTIVE' AND s.current_status NOT IN ('COMPLETED','CANCELLED')
     AND s.destination_facility_id = ANY (ctx.facilities);
  IF NOT FOUND THEN RAISE EXCEPTION 'DRIVER_NOT_IN_SCOPE' USING ERRCODE = 'P0001'; END IF;
  UPDATE driver_link_codes SET revoked_at = clock_timestamp()                 -- one usable code per driver at a time
   WHERE driver_id = p_driver AND consumed_at IS NULL AND revoked_at IS NULL;
  exp := clock_timestamp() + make_interval(secs => p_ttl_s);
  INSERT INTO driver_link_codes (code_hash, driver_id, channel, issued_by, expires_at)
  VALUES (p_code_hash, p_driver, 'TELEGRAM', ctx.principal, exp);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'DRIVER_LINK_CODE_ISSUED', NULL, NULL,
          jsonb_build_object('driver_id', p_driver, 'channel', 'TELEGRAM', 'expires_at', exp), 'telegram linking');
  RETURN jsonb_build_object('driver_id', p_driver, 'expires_at', exp);
END $$;

-- The driver sends the code to the bot. Every failure answers the same way (no hint whether a code exists).
CREATE FUNCTION redeem_driver_link_code(p_code_hash text, p_external_user text, p_chat_ref text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE c driver_link_codes; l driver_channel_links;
BEGIN
  SELECT * INTO l FROM driver_channel_links WHERE channel = 'TELEGRAM' AND external_user_id = p_external_user
     AND revoked_at IS NULL;
  SELECT * INTO c FROM driver_link_codes WHERE code_hash = p_code_hash FOR UPDATE;
  IF FOUND AND l.link_id IS NOT NULL AND l.driver_id = c.driver_id AND c.consumed_at IS NOT NULL THEN
    RETURN jsonb_build_object('linked', true, 'driver_id', l.driver_id, 'replayed', true);   -- a repeated /start
  END IF;
  IF NOT FOUND OR c.consumed_at IS NOT NULL OR c.revoked_at IS NOT NULL OR c.expires_at <= clock_timestamp() THEN
    RETURN jsonb_build_object('linked', false, 'reason', 'INVALID_OR_EXPIRED_CODE');
  END IF;
  IF l.link_id IS NOT NULL THEN                       -- this Telegram account already speaks for a driver
    RETURN jsonb_build_object('linked', false, 'reason', 'ACCOUNT_ALREADY_LINKED');
  END IF;
  IF EXISTS (SELECT 1 FROM driver_channel_links WHERE driver_id = c.driver_id AND channel = 'TELEGRAM'
              AND revoked_at IS NULL) THEN            -- re-linking needs staff to revoke the old account first
    RETURN jsonb_build_object('linked', false, 'reason', 'DRIVER_ALREADY_LINKED');
  END IF;
  UPDATE driver_link_codes SET consumed_at = clock_timestamp() WHERE code_hash = p_code_hash;
  INSERT INTO driver_channel_links (driver_id, channel, external_user_id, chat_ref, link_code_hash)
  VALUES (c.driver_id, 'TELEGRAM', p_external_user, p_chat_ref, p_code_hash);
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES ('driver:' || c.driver_id, 'driver', 'DRIVER_CHANNEL_LINKED', NULL, NULL,
          jsonb_build_object('driver_id', c.driver_id, 'channel', 'TELEGRAM', 'code_issued_by', c.issued_by),
          'one-time code redeemed');
  RETURN jsonb_build_object('linked', true, 'driver_id', c.driver_id, 'replayed', false);
END $$;

CREATE FUNCTION driver_for_channel(p_channel text, p_external_user text) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT l.driver_id FROM driver_channel_links l JOIN drivers d USING (driver_id)
   WHERE l.channel = p_channel AND l.external_user_id = p_external_user AND l.revoked_at IS NULL AND d.status <> 'INACTIVE'
$$;

CREATE FUNCTION driver_channel_chat(p_driver text, p_channel text) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
  SELECT chat_ref FROM driver_channel_links WHERE driver_id = p_driver AND channel = p_channel AND revoked_at IS NULL
$$;

CREATE FUNCTION revoke_driver_channel_link(p_driver text, p_reason text, p_principal text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; n integer;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['operations','ops_lead'], 'unlinking a driver''s Telegram account');
  PERFORM check_principal(ctx, p_principal);
  IF p_reason IS NULL OR length(p_reason) NOT BETWEEN 3 AND 300 THEN RAISE EXCEPTION 'REASON_REQUIRED' USING ERRCODE = 'P0001'; END IF;
  PERFORM 1 FROM shipments s WHERE s.driver_id = p_driver AND s.destination_facility_id = ANY (ctx.facilities);
  IF NOT FOUND THEN RAISE EXCEPTION 'DRIVER_NOT_IN_SCOPE' USING ERRCODE = 'P0001'; END IF;
  UPDATE driver_channel_links SET revoked_at = clock_timestamp(), revoked_by = ctx.principal
   WHERE driver_id = p_driver AND revoked_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  INSERT INTO booking_events (principal, principal_role, action, facility_id, shipment_id, after_state, reason)
  VALUES (ctx.principal, role, 'DRIVER_CHANNEL_UNLINKED', NULL, NULL, jsonb_build_object('driver_id', p_driver), p_reason);
  RETURN n;
END $$;

-- ================================================================ ownership and privileges
ALTER FUNCTION dispatch_offer_json(dispatch_offers)                                OWNER TO setuhaul_rpc;
ALTER FUNCTION all_facility_scope()                                                OWNER TO setuhaul_rpc;
ALTER FUNCTION create_dispatch_offer(text, text, text, text, timestamptz, timestamptz, boolean, bigint, integer, text,
                                     text, text)                                   OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_dispatch_offer_attested()                                       OWNER TO setuhaul_rpc;
ALTER FUNCTION trg_dispatch_booking_needs_planner()                                OWNER TO setuhaul_rpc;
ALTER FUNCTION dispatch_offer_for_driver(uuid, text)                               OWNER TO setuhaul_rpc;
ALTER FUNCTION request_offer_alternative(uuid, text, bigint)                       OWNER TO setuhaul_rpc;
ALTER FUNCTION driver_location_context(text, text)                                 OWNER TO setuhaul_rpc;
ALTER FUNCTION record_driver_location(text, text, text, jsonb)                     OWNER TO setuhaul_rpc;
ALTER FUNCTION issue_driver_link_code(text, text, integer, text)                   OWNER TO setuhaul_rpc;
ALTER FUNCTION redeem_driver_link_code(text, text, text)                           OWNER TO setuhaul_rpc;
ALTER FUNCTION driver_for_channel(text, text)                                      OWNER TO setuhaul_rpc;
ALTER FUNCTION driver_channel_chat(text, text)                                     OWNER TO setuhaul_rpc;
ALTER FUNCTION revoke_driver_channel_link(text, text, text)                        OWNER TO setuhaul_rpc;

GRANT SELECT, INSERT, UPDATE ON dispatch_offers TO setuhaul_rpc;
GRANT SELECT ON dispatch_offers TO setuhaul_console;
GRANT SELECT, INSERT ON driver_location_reports TO setuhaul_rpc;
GRANT SELECT ON driver_location_reports TO setuhaul_console;
GRANT SELECT, INSERT, UPDATE ON driver_link_codes, driver_channel_links TO setuhaul_rpc;
GRANT SELECT, INSERT ON shown_options, chat_threads TO setuhaul_rpc;
GRANT UPDATE (shipment_id) ON chat_threads TO setuhaul_rpc;
GRANT SELECT ON shipments, drivers, docks, facilities, appointments, dock_occupancy, eta_signals, driver_exceptions,
             option_selections, chat_messages TO setuhaul_rpc;
GRANT INSERT ON outbox TO setuhaul_rpc;
GRANT UPDATE (driver_id, updated_at) ON shipments TO setuhaul_rpc;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
GRANT EXECUTE ON FUNCTION create_dispatch_offer(text, text, text, text, timestamptz, timestamptz, boolean, bigint, integer,
                                                text, text, text)                  TO setuhaul_console;
GRANT EXECUTE ON FUNCTION issue_driver_link_code(text, text, integer, text)        TO setuhaul_console;
GRANT EXECUTE ON FUNCTION revoke_driver_channel_link(text, text, text)             TO setuhaul_console;
GRANT EXECUTE ON FUNCTION dispatch_offer_for_driver(uuid, text)                    TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION request_offer_alternative(uuid, text, bigint)            TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION driver_location_context(text, text)                      TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION record_driver_location(text, text, text, jsonb)          TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION redeem_driver_link_code(text, text, text)                TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION driver_for_channel(text, text)                           TO setuhaul_ingest;
GRANT EXECUTE ON FUNCTION driver_channel_chat(text, text)                          TO setuhaul_system;
