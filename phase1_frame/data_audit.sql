-- SetuHaul Phase 1 data audit
-- Each check starts with a header line:  -- @check <ID> | <severity> | <title>
-- A check that returns rows has found evidence of the issue. run_audit.py runs every check.
-- Snapshot: 2026-08-04 Asia/Kolkata. All timestamps are ISO-8601 text with +05:30, so string compare is safe.

-- @check A01 | HIGH | Same driver assigned to overlapping in-flight shipments (physically impossible)
-- busy window of a shipment = departure .. gate-out (if known) else latest ETA + unload
WITH busy AS (
  SELECT s.shipment_id, s.driver_id, s.vehicle_id, s.current_status, s.origin_city,
         -- normalised to IST wall-clock text 'YYYY-MM-DD HH:MM:SS' so both sides compare and read the same
         datetime(COALESCE(s.actual_departure_ts, s.planned_departure_ts), '+330 minutes') AS busy_from,
         COALESCE(datetime(fc.gate_out_ts, '+330 minutes'),
                  datetime(COALESCE(s.latest_eta_ts, s.original_eta_ts), '+' || s.expected_unload_min || ' minutes', '+330 minutes')) AS busy_to
  FROM shipments s LEFT JOIN facility_checkins fc ON fc.shipment_id = s.shipment_id
  WHERE s.current_status <> 'CANCELLED')
SELECT a.driver_id, a.shipment_id AS shipment_a, a.current_status AS status_a,
       a.busy_from AS a_from, a.busy_to AS a_to,
       b.shipment_id AS shipment_b, b.current_status AS status_b, b.origin_city AS origin_b,
       b.busy_from AS b_from
FROM busy a JOIN busy b ON a.driver_id = b.driver_id AND a.shipment_id < b.shipment_id
WHERE b.busy_from < a.busy_to AND a.busy_from < b.busy_to;

-- @check A02 | HIGH | Same vehicle used by overlapping shipments (incl. a different driver)
SELECT a.vehicle_id, a.shipment_id AS shipment_a, a.driver_id AS driver_a, a.current_status AS status_a,
       substr(a.latest_eta_ts,12,5) AS eta_a, b.shipment_id AS shipment_b, b.driver_id AS driver_b,
       b.current_status AS status_b, substr(COALESCE(b.actual_departure_ts,b.planned_departure_ts),12,5) AS dep_b
FROM shipments a JOIN shipments b
  ON a.vehicle_id = b.vehicle_id AND a.shipment_id < b.shipment_id
WHERE a.current_status <> 'CANCELLED' AND b.current_status <> 'CANCELLED'
  AND COALESCE(b.actual_departure_ts,b.planned_departure_ts) < COALESCE(a.latest_eta_ts,a.original_eta_ts)
  AND COALESCE(a.actual_departure_ts,a.planned_departure_ts) < COALESCE(b.latest_eta_ts,b.original_eta_ts);

-- @check A03 | HIGH | Active appointment on a dock whose weight limit is below the load
SELECT a.appointment_id, a.shipment_id, a.appointment_status, d.dock_code,
       s.load_weight_kg, d.max_vehicle_weight_kg
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN shipments s ON s.shipment_id = a.shipment_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.load_weight_kg > d.max_vehicle_weight_kg;

-- @check A04 | HIGH | Expected unload time does not fit in the booked slot (spills into next slot, which the model does not reserve)
SELECT a.appointment_id, a.shipment_id, a.appointment_status, d.dock_code,
       substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end,
       s.expected_unload_min,
       (strftime('%s',sl.slot_end_ts) - strftime('%s',sl.slot_start_ts))/60 AS slot_min
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN shipments s ON s.shipment_id = a.shipment_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.expected_unload_min > (strftime('%s',sl.slot_end_ts) - strftime('%s',sl.slot_start_ts))/60;

-- @check A05 | HIGH | Pending booking that cannot start until after the driver's latest ETA, and whose unload overruns the slot end
SELECT a.appointment_id, a.shipment_id, d.dock_code,
       substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end,
       substr(le.effective_eta_ts,12,5) AS eta, le.eta_confidence, s.expected_unload_min,
       time(strftime('%s',le.effective_eta_ts) + s.expected_unload_min*60 + 19800, 'unixepoch') AS earliest_finish
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN shipments s ON s.shipment_id = a.shipment_id
JOIN v_latest_eta le ON le.shipment_id = a.shipment_id
WHERE a.appointment_status = 'PENDING_CONFIRMATION'
  AND datetime(le.effective_eta_ts, '+' || s.expected_unload_min || ' minutes') > datetime(sl.slot_end_ts);

-- @check A06 | HIGH | Confirmed appointment already made infeasible by the driver's latest ETA (stale promise)
SELECT a.appointment_id, a.shipment_id, d.dock_code,
       substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end,
       substr(le.effective_eta_ts,12,5) AS latest_eta, le.eta_source
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN v_latest_eta le ON le.shipment_id = a.shipment_id
LEFT JOIN facility_checkins fc ON fc.shipment_id = a.shipment_id
WHERE a.appointment_status = 'CONFIRMED' AND a.is_current = 1
  AND fc.gate_in_ts IS NULL
  AND le.effective_eta_ts >= sl.slot_end_ts;

-- @check A07 | MEDIUM | Original plan booked a slot that starts before the planned ETA (planner promise was infeasible from day one)
SELECT a.appointment_id, a.shipment_id, d.dock_code,
       substr(sl.slot_start_ts,12,5) AS slot_start, substr(s.original_eta_ts,12,5) AS original_eta,
       a.booking_source
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN shipments s ON s.shipment_id = a.shipment_id
WHERE a.booking_source = 'PLANNER'
  AND s.original_eta_ts > sl.slot_start_ts;

-- @check A08 | HIGH | Confirmed appointment on a dock with an overlapping breakdown/maintenance event, slot not blocked
SELECT a.appointment_id, a.shipment_id, d.dock_code, sl.slot_id, sl.slot_status,
       substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end,
       e.dock_event_id, e.event_type,
       substr(e.event_start_ts,12,5) AS ev_start, substr(e.event_end_ts,12,5) AS ev_end
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN docks d ON d.dock_id = sl.dock_id
JOIN dock_status_events e ON e.dock_id = sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')
  AND e.event_type IN ('BREAKDOWN','MAINTENANCE','MANUAL_BLOCK')
  AND e.event_start_ts < sl.slot_end_ts
  AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts
  AND sl.slot_status = 'OPEN';

-- @check A09 | MEDIUM | OPEN slots that overlap a dock event (view v_slot_availability would show them AVAILABLE or OCCUPIED, not BLOCKED)
SELECT sl.slot_id, d.dock_code, substr(sl.slot_start_ts,12,5) AS slot_start, substr(sl.slot_end_ts,12,5) AS slot_end,
       e.dock_event_id, e.event_type, v.availability_status
FROM appointment_slots sl
JOIN docks d ON d.dock_id = sl.dock_id
JOIN dock_status_events e ON e.dock_id = sl.dock_id
JOIN v_slot_availability v ON v.slot_id = sl.slot_id
WHERE sl.slot_status = 'OPEN'
  AND e.event_type <> 'REOPENED'
  AND e.event_start_ts < sl.slot_end_ts
  AND COALESCE(e.event_end_ts,'9999') > sl.slot_start_ts;

-- @check A10 | MEDIUM | docks.dock_status says ACTIVE while a breakdown event is open at the snapshot
SELECT d.dock_id, d.dock_code, d.dock_status, e.dock_event_id, e.event_type, e.event_start_ts, e.event_end_ts
FROM docks d JOIN dock_status_events e ON e.dock_id = d.dock_id
WHERE e.event_type = 'BREAKDOWN' AND d.dock_status = 'ACTIVE';

-- @check A11 | MEDIUM | In-dock truck has overrun its expected unload; next confirmed booking on that dock is affected
SELECT fc.shipment_id, d.dock_code, substr(fc.unload_start_ts,12,5) AS unload_start, s.expected_unload_min,
       time(strftime('%s',fc.unload_start_ts) + s.expected_unload_min*60 + 19800, 'unixepoch') AS expected_end,
       nxt.appointment_id AS next_appt, nxt.shipment_id AS next_shipment, substr(nsl.slot_start_ts,12,5) AS next_slot_start
FROM facility_checkins fc
JOIN shipments s ON s.shipment_id = fc.shipment_id
JOIN docks d ON d.dock_id = fc.actual_dock_id
JOIN appointment_slots nsl ON nsl.dock_id = fc.actual_dock_id
JOIN appointments nxt ON nxt.slot_id = nsl.slot_id AND nxt.appointment_status IN ('CONFIRMED','PENDING_CONFIRMATION')
WHERE fc.queue_state = 'IN_DOCK' AND fc.unload_end_ts IS NULL
  AND nsl.slot_start_ts > fc.unload_start_ts
  AND datetime(nsl.slot_start_ts) < datetime(fc.unload_start_ts, '+' || s.expected_unload_min || ' minutes');

-- @check A12 | HIGH | Historical double booking: two appointments were active on the same slot at the same time (index would reject this today)
SELECT a.slot_id, a.appointment_id AS appt_a, a.booked_at AS booked_a, a.appointment_status AS status_a,
       b.appointment_id AS appt_b, b.booked_at AS booked_b, b.cancelled_at AS b_cancelled_at
FROM appointments a JOIN appointments b
  ON a.slot_id = b.slot_id AND a.appointment_id <> b.appointment_id
WHERE b.booked_at > a.booked_at
  AND (a.cancelled_at IS NULL OR a.cancelled_at > b.booked_at)
  AND (b.cancelled_at IS NULL OR b.cancelled_at > a.booked_at)
  AND a.appointment_status NOT IN ('REJECTED') AND b.appointment_status NOT IN ('REJECTED');

-- @check A13 | MEDIUM | Snapshot has no single as-of time: records were last updated across a 9-hour span
SELECT 'shipments' AS tbl, min(updated_at) AS min_ts, max(updated_at) AS max_ts FROM shipments
UNION ALL SELECT 'appointments', min(updated_at), max(updated_at) FROM appointments
UNION ALL SELECT 'facility_checkins', min(updated_at), max(updated_at) FROM facility_checkins
UNION ALL SELECT 'chat_messages', min(message_ts), max(message_ts) FROM chat_messages
UNION ALL SELECT 'eta_updates', min(created_at), max(created_at) FROM eta_updates;

-- @check A14 | MEDIUM | Confirmed slot that already started past the no-show grace with no check-in and no NO_SHOW mark (relative to latest snapshot time)
SELECT a.appointment_id, a.shipment_id, substr(sl.slot_start_ts,12,5) AS slot_start,
       r.rule_value AS grace_min, a.appointment_status
FROM appointments a
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN shipments s ON s.shipment_id = a.shipment_id
JOIN facility_rules r ON r.facility_id = sl.facility_id AND r.rule_type = 'NO_SHOW_GRACE_MIN' AND r.active_flag = 1
LEFT JOIN facility_checkins fc ON fc.shipment_id = a.shipment_id
WHERE a.appointment_status = 'CONFIRMED' AND fc.gate_in_ts IS NULL
  AND datetime(sl.slot_start_ts, '+' || r.rule_value || ' minutes') < datetime((SELECT max(updated_at) FROM shipments));

-- @check A15 | MEDIUM | OPEN slots that start after the facility LAST_NEW_START_TIME rule (bookable without the required approval)
SELECT sl.slot_id, d.dock_code, substr(sl.slot_start_ts,12,5) AS slot_start, r.rule_value AS last_new_start
FROM appointment_slots sl
JOIN docks d ON d.dock_id = sl.dock_id
JOIN facility_rules r ON r.facility_id = sl.facility_id AND r.rule_type = 'LAST_NEW_START_TIME'
WHERE sl.slot_status = 'OPEN' AND substr(sl.slot_start_ts,12,5) >= r.rule_value;

-- @check A16 | LOW | Facility rules are incomplete or duplicated across facilities (GGN lacks early-checkin, last-start, reefer, heavy rules; grace defined twice)
SELECT f.facility_id, f.checkin_grace_min AS facilities_grace,
       (SELECT rule_value FROM facility_rules r WHERE r.facility_id=f.facility_id AND rule_type='NO_SHOW_GRACE_MIN') AS rule_grace,
       (SELECT group_concat(rule_type) FROM facility_rules r WHERE r.facility_id=f.facility_id) AS rule_types,
       f.open_time, f.close_time,
       (SELECT min(substr(slot_start_ts,12,5)) FROM appointment_slots s WHERE s.facility_id=f.facility_id) AS first_slot,
       (SELECT max(substr(slot_end_ts,12,5)) FROM appointment_slots s WHERE s.facility_id=f.facility_id) AS last_slot_end,
       f.default_unload_min
FROM facilities f;

-- @check A17 | LOW | Reefer rule is hard-coded to a dock code (RULE003 says "D5") instead of a capability
SELECT rule_id, facility_id, rule_type, rule_value, description FROM facility_rules
WHERE rule_type = 'REEFER_DOCK_REQUIRED' AND description LIKE '%D5%';

-- @check A18 | MEDIUM | Duplicate exception rows share one dedupe_key (no UNIQUE constraint), and the retry opened a second thread
SELECT e.dedupe_key, group_concat(e.exception_id) AS exceptions, group_concat(e.thread_id) AS threads,
       group_concat(e.exception_status) AS statuses
FROM driver_exceptions e GROUP BY e.dedupe_key HAVING count(*) > 1;

-- @check A19 | MEDIUM | Duplicate driver messages: same driver + same text, different external ids
SELECT m1.chat_message_id, m1.thread_id, m1.external_message_id, m2.chat_message_id AS dup_id,
       m2.thread_id AS dup_thread, m2.external_message_id AS dup_ext_id, m2.is_duplicate,
       (strftime('%s',m2.message_ts) - strftime('%s',m1.message_ts)) AS seconds_apart
FROM chat_messages m1 JOIN chat_messages m2
  ON m1.sender_reference = m2.sender_reference AND m1.message_text = m2.message_text
 AND m1.chat_message_id < m2.chat_message_id
WHERE m1.sender_type = 'DRIVER';

-- @check A20 | MEDIUM | Conversation not linked to a shipment while the driver has more than one active assignment
SELECT t.thread_id, t.driver_id, t.thread_intent,
       (SELECT group_concat(shipment_id) FROM shipments s
         WHERE s.driver_id = t.driver_id AND s.current_status NOT IN ('CANCELLED','COMPLETED')) AS candidate_shipments
FROM chat_threads t WHERE t.shipment_id IS NULL;

-- @check A21 | HIGH | Escalation notification failed or went to an address that is not a known facility contact
SELECT om.operational_message_id, om.shipment_id, om.recipient_address, om.delivery_status,
       CASE WHEN fc.contact_id IS NULL THEN 'UNKNOWN_RECIPIENT' ELSE fc.contact_id END AS contact_match
FROM operational_messages om
LEFT JOIN facility_contacts fc ON fc.email = om.recipient_address
WHERE om.channel = 'EMAIL' AND (om.delivery_status = 'FAILED' OR fc.contact_id IS NULL);

-- @check A22 | LOW | Facility contact missing email (cannot receive email escalations)
SELECT contact_id, facility_id, contact_role, contact_name, email, phone FROM facility_contacts
WHERE email IS NULL OR trim(email) = '' OR contact_name LIKE 'Vacant%';

-- @check A23 | MEDIUM | Pending confirmations with no expiry: request sent/queued but no confirmation, no TTL field exists
SELECT a.appointment_id, a.shipment_id, a.booked_at, om.operational_message_id, om.delivery_status,
       (strftime('%s',(SELECT max(updated_at) FROM shipments)) - strftime('%s',a.booked_at))/60 AS minutes_pending_at_snapshot
FROM appointments a
LEFT JOIN operational_messages om ON om.appointment_id = a.appointment_id
WHERE a.appointment_status = 'PENDING_CONFIRMATION';

-- @check A24 | MEDIUM | Escalated thread whose appointment is still CONFIRMED (system of record disagrees with conversation state)
SELECT t.thread_id, t.thread_status, t.shipment_id, a.appointment_id, a.appointment_status
FROM chat_threads t
JOIN appointments a ON a.shipment_id = t.shipment_id AND a.is_current = 1
WHERE t.thread_status = 'ESCALATED' AND a.appointment_status = 'CONFIRMED';

-- @check A25 | MEDIUM | Live shipment with no current active appointment (needs a slot, nobody owns it)
SELECT s.shipment_id, s.current_status, substr(le.effective_eta_ts,12,5) AS eta,
       (SELECT group_concat(appointment_id || ':' || appointment_status) FROM appointments a WHERE a.shipment_id = s.shipment_id) AS appointment_history,
       (SELECT group_concat(exception_id || ':' || exception_status) FROM driver_exceptions e WHERE e.shipment_id = s.shipment_id) AS exceptions
FROM shipments s JOIN v_latest_eta le USING (shipment_id)
WHERE s.current_status NOT IN ('CANCELLED','COMPLETED')
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = s.shipment_id AND a.is_current = 1
                  AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'));

-- @check A26 | LOW | Exception delay does not reconcile with declared ETA minus original ETA (early arrival cannot be expressed)
SELECT e.exception_id, e.shipment_id, e.reported_delay_min,
       (strftime('%s',e.declared_eta_ts) - strftime('%s',s.original_eta_ts))/60 AS computed_delay_min
FROM driver_exceptions e JOIN shipments s ON s.shipment_id = e.shipment_id
WHERE e.declared_eta_ts IS NOT NULL AND e.exception_status <> 'DUPLICATE'
  AND e.reported_delay_min <> (strftime('%s',e.declared_eta_ts) - strftime('%s',s.original_eta_ts))/60;

-- @check A27 | LOW | Exception with a stated time preference but no latest-finish captured (open question to the driver)
SELECT e.exception_id, e.thread_id, e.earliest_acceptable_ts, e.latest_acceptable_ts, m.message_text
FROM driver_exceptions e
JOIN chat_messages m ON m.thread_id = e.thread_id AND m.sender_type = 'DRIVER' AND m.is_duplicate = 0
WHERE e.latest_acceptable_ts IS NULL
  AND (m.message_text LIKE '%leave%' OR m.message_text LIKE '%before%' OR m.message_text LIKE '%after%' OR m.message_text LIKE '%pickup%');

-- @check A28 | INFO | Low-confidence / range ETAs the scheduler must not treat as a point estimate
SELECT eta_update_id, shipment_id, declared_eta_ts, confidence_code, note
FROM eta_updates WHERE confidence_code = 'LOW';

-- @check A29 | INFO | No compatible feasible slot for the reefer shipment after its declared ETA (expected escalation case)
SELECT s.shipment_id, substr(le.effective_eta_ts,12,5) AS eta, s.expected_unload_min,
       (SELECT count(*) FROM v_slot_availability v
         WHERE v.facility_id = s.destination_facility_id AND v.supports_refrigerated = 1
           AND v.availability_status = 'AVAILABLE' AND v.slot_start_ts >= le.effective_eta_ts) AS feasible_reefer_slots
FROM shipments s JOIN v_latest_eta le USING (shipment_id)
WHERE s.temperature_control_required = 1 AND s.current_status NOT IN ('COMPLETED','CANCELLED');

-- @check A30 | INFO | Integrity gaps the two partial unique indexes do NOT cover (structural, proven by schema inspection)
SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name LIKE 'ux_%';
