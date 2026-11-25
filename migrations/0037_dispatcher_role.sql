-- 0037 · A dedicated `dispatcher` staff role. Forward-only; 0001–0036 untouched.
--
-- Until now the Cognito `operations` group acted as dispatcher (a temporary mapping, 0035). This adds `dispatcher` as
-- its own application role, carried in the transaction's staff identity exactly like the other three:
--   * begin_staff_context accepts it;
--   * staff_role_for ranks it lowest (audit records the most senior role a person holds);
--   * the five dispatch actions accept it: send an offer, issue / revoke a Telegram link code, reassign a confirmed
--     appointment, request a cancellation.
-- What it does NOT get: approval_role_rules is unchanged (its CHECK does not even admit 'dispatcher'), so a dispatcher
-- decides no booking request, no PLANNER_REVIEW, no move; decide_cancellation stays planner / ops_lead, and the
-- requester-is-not-the-decider rule is unchanged.
-- `operations` keeps every permission it had (compatibility; removing it is a later, separate owner decision).
-- No new database role, no grant change: these are CREATE OR REPLACE of existing functions (owner and grants kept).
-- Each body below is the current definition with only the role list changed.

SET search_path = setuhaul, public;

CREATE OR REPLACE FUNCTION begin_staff_context(p_principal text, p_roles text[], p_facilities text[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
BEGIN
  IF p_roles IS NULL OR cardinality(p_roles) = 0 OR NOT (p_roles <@ ARRAY['dispatcher','operations','planner','ops_lead']) THEN
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

CREATE OR REPLACE FUNCTION staff_role_for(ctx staff_context, p_allowed text[], p_what text) RETURNS text LANGUAGE plpgsql IMMUTABLE
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE role text;
BEGIN
  SELECT x INTO role FROM unnest(ctx.roles) x WHERE x = ANY (p_allowed)
   ORDER BY array_position(ARRAY['dispatcher','operations','planner','ops_lead'], x) DESC LIMIT 1;
  IF role IS NULL THEN RAISE EXCEPTION 'ROLE_NOT_ALLOWED: % needs one of %', p_what, p_allowed USING ERRCODE = 'P0001'; END IF;
  RETURN role;
END $$;

CREATE OR REPLACE FUNCTION create_dispatch_offer(p_shipment text, p_driver text, p_vehicle text, p_dock text, p_start timestamptz,
                                      p_end timestamptz, p_needs_approval boolean, p_schedule_version bigint,
                                      p_ttl_s integer, p_text text, p_idem_key text, p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; s shipments; d drivers; t chat_threads; o dispatch_offers; prev record;
  k text; oid uuid := gen_random_uuid(); offer uuid := gen_random_uuid(); mid bigint; sel bigint; fstate text;
  dock_fac text; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'sending a booking offer');
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

CREATE OR REPLACE FUNCTION issue_driver_link_code(p_driver text, p_code_hash text, p_ttl_s integer, p_principal text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; exp timestamptz;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'linking a driver''s Telegram account');
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

CREATE OR REPLACE FUNCTION revoke_driver_channel_link(p_driver text, p_reason text, p_principal text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; n integer;
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'unlinking a driver''s Telegram account');
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

CREATE OR REPLACE FUNCTION create_reassignment_offer(p_shipment text, p_driver text, p_ttl_s integer, p_text text, p_idem_key text,
                                          p_principal text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ctx staff_context; role text; s shipments; d drivers; a appointments; t chat_threads; o dispatch_offers;
  oid uuid := gen_random_uuid(); offer uuid := gen_random_uuid(); mid bigint; sel bigint; fstate text; why text;
  h text; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  ctx := staff_ctx();
  role := staff_role_for(ctx, ARRAY['dispatcher','operations','ops_lead'], 'reassigning a confirmed appointment');
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
