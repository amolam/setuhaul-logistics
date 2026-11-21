# Schema validation and integrity proof (generated)

Run 2026-11-18 00:11 against a local PostgreSQL 18.6. Source: `validate_schema.py`. Production target is Aurora PostgreSQL 18.3+ (decision T33); the same script must pass there through the Data API (gate G1).

## 1. Schema and snapshot load

- Schema applied cleanly on 18.x: **23 tables**, exclusion constraints, triggers, RLS policies.
- Reference data loaded: 21 shipments, 9 docks, 106 slots (slot non-overlap constraint satisfied).
- Occupancy load: **12 accepted, 4 rejected by constraints**:

| Rejected row | Constraint message |
|---|---|
| CONFIRMED APT1004 SHP1004 D2 09:00-10:10 | EXCLUSION: overlaps an existing occupancy on the same dock |
| CONFIRMED APT1005 SHP1005 D3 09:00-10:25 | DOCK_WEIGHT_EXCEEDED: 20500 kg > 20000 kg on D3 |
| CONFIRMED APT1015A SHP1015 D5 17:00-18:10 | EXCLUSION: overlaps an existing occupancy on the same dock |
| PENDING_CONFIRMATION APT1014A SHP1014 D1 11:00-12:25 | DOCK_WEIGHT_EXCEEDED: 21000 kg > 20000 kg on D1 |

## 2. Same-slot contention

- **10 parallel holds × 30 rounds:** winners per round = [1], losers = 270, other errors = 0 → **PASS: exactly one winner every round**
- **50 parallel holds × 20 rounds:** winners per round = [1], losers = 980, other errors = 0 → **PASS: exactly one winner every round**
- **200 parallel holds × 10 rounds:** winners per round = [1], losers = 1990, other errors = 0 → **PASS: exactly one winner every round**

## 3. Overlapping windows of different lengths

- 150 parallel holds, random start (09:00–13:00) and length (55–100 min) on one dock: 4 granted, **0 overlapping active occupancies** → **PASS**

## 4. Rule and audit guards

| Attempt | Result |
|---|---|
| SHP1014 (21 t) on D1 (20 t max) | REJECTED: DOCK_WEIGHT_EXCEEDED: 21000 kg > 20000 kg on D1 |
| SHP1015 (reefer) on D2 (standard) | REJECTED: DOCK_NOT_REFRIGERATED: D2 |
| SHP1016 (31 t heavy) on D4 | REJECTED: DOCK_WEIGHT_EXCEEDED: 31000 kg > 25000 kg on D4 |
| SHP1021 (Gurugram) on Jaipur D1 | REJECTED: DOCK_WRONG_FACILITY: DOCK-JAI-D1 is not at FAC-GGN-01 |
| SHP1019 (cancelled) on D1 | REJECTED: SHIPMENT_NOT_ELIGIBLE: SHP1019 is CANCELLED |
| Second appointment row for SHP1013 (current, active) | REJECTED: duplicate key value violates unique constraint "ux_current_active_appointment_per_shipment" |
| UPDATE on booking_events (audit trail) | REJECTED: booking_events is append-only |
| Duplicate exception with same dedupe_key | REJECTED: duplicate key value violates unique constraint "driver_exceptions_dedupe_key_key" |
| place_hold while facility automation is paused | REJECTED: AUTOMATION_PAUSED |
| place_hold on an expired option | REJECTED: OPTION_EXPIRED |
| Same truck (VTRUCK1) booked on D2 for another shipment at an overlapping time | REJECTED: conflicting key value violates exclusion constraint "ex_one_dock_per_truck" |

## 5. Row-level security: every facility-owned table, Data API transaction pattern

Reads show rows visible. Write attempts show 1 when RLS blocked the write.

| Caller and attempt | Got | Expected | Result |
|---|---:|---:|---|
| Console, scope GGN: escalations | 1 | 1 | PASS |
| Console, scope JAI+GGN: escalations | 2 | 2 | PASS |
| Console, scope unset (after DISCARD ALL): escalations | 0 | 0 | PASS |
| Agent, scope JAI: active dock occupancy (JAI rows) | 17 | 17 | PASS |
| Agent, scope GGN: dock occupancy (all JAI rows hidden) | 0 | 0 | PASS |
| Console, scope unset: outbox | 0 | 0 | PASS |
| Console, scope unset: notifications | 0 | 0 | PASS |
| Console, scope unset: booking_events | 0 | 0 | PASS |
| Agent, scope unset: appointments | 0 | 0 | PASS |
| Agent, scope unset: shown_options | 0 | 0 | PASS |
| System job, scope = all facilities: outbox | 2 | 2 | PASS |
| Function-owner role scoped to GGN inserts a JAI dock block (WITH CHECK) | 1 | 1 | PASS |
| Non-superuser table owner, scope unset (FORCE) | 0 | 0 | PASS |
| App roles that are SUPERUSER or BYPASSRLS | 0 | 0 | PASS |
| Driver A session: own rows in eta_signals | 1 | 1 | PASS |
| Driver A session: driver B's rows in eta_signals | 0 | 0 | PASS |
| Driver A session + JAI scope also set: driver B's rows in eta_signals (driver context is strict) | 0 | 0 | PASS |
| Staff scope JAI: other facility's rows in eta_signals | 0 | 0 | PASS |
| Staff scope JAI: own facility rows in eta_signals | 1 | 1 | PASS |
| No scope, no driver: eta_signals | 0 | 0 | PASS |
| Driver A session: own rows in tracking_consents | 1 | 1 | PASS |
| Driver A session: driver B's rows in tracking_consents | 0 | 0 | PASS |
| Driver A session + JAI scope also set: driver B's rows in tracking_consents (driver context is strict) | 0 | 0 | PASS |
| Staff scope JAI: other facility's rows in tracking_consents | 0 | 0 | PASS |
| Staff scope JAI: own facility rows in tracking_consents | 1 | 1 | PASS |
| No scope, no driver: tracking_consents | 0 | 0 | PASS |
| Driver A session: own rows in driver_exceptions | 1 | 1 | PASS |
| Driver A session: driver B's rows in driver_exceptions | 0 | 0 | PASS |
| Driver A session + JAI scope also set: driver B's rows in driver_exceptions (driver context is strict) | 0 | 0 | PASS |
| Staff scope JAI: other facility's rows in driver_exceptions | 0 | 0 | PASS |
| Staff scope JAI: own facility rows in driver_exceptions | 1 | 1 | PASS |
| No scope, no driver: driver_exceptions | 0 | 0 | PASS |
| Driver A writes an ETA signal for driver B's shipment (WITH CHECK) | 1 | 1 | PASS |
| Driver A records consent in driver B's name | 1 | 1 | PASS |
| Driver A records own consent against driver B's shipment | 1 | 1 | PASS |
| Tool service scoped to JAI inserts an exception for another facility's shipment | 1 | 1 | PASS |
| Driver A updates driver B's exception (rows affected) | 0 | 0 | PASS |
| Driver A writes an ETA signal for own shipment | 1 | 1 | PASS |
| Tables neither RLS-forced nor on the reasoned allowlist | 0 | 0 | PASS |

Tables without RLS, each with a stated reason (the coverage check fails on any table missing from this list):

- `carriers`: reference data
- `drivers`: reference data (identity resolved by the API, not queried freely)
- `vehicles`: reference data
- `facilities`: reference data
- `facility_config`: reference data
- `docks`: reference data
- `idempotency_keys`: hashed keys, no facility or personal data

### 5b. TMS / WMS mirror tables (closes B-RLS-2) and dependent access

| Caller and attempt | Got | Expected | Result |
|---|---:|---:|---|
| Driver A reads own shipment | 1 | 1 | PASS |
| Driver A reads all shipments: sees only their own | 1 | 1 | PASS |
| Driver A reads driver B's shipment | 0 | 0 | PASS |
| Driver A + JAI facility scope reads driver B's shipment (driver context is strict) | 0 | 0 | PASS |
| GGN staff read JAI shipments | 0 | 0 | PASS |
| JAI staff read JAI shipments | 222 | 222 | PASS |
| No scope, no driver: shipments | 0 | 0 | PASS |
| GGN staff read JAI appointment slots | 0 | 0 | PASS |
| JAI staff read JAI appointment slots | 79 | 79 | PASS |
| GGN staff read a JAI dock event | 0 | 0 | PASS |
| JAI staff read a JAI dock event | 1 | 1 | PASS |
| Driver A still reads own chat thread (policy looks up shipments) | 1 | 1 | PASS |
| JAI staff still read that thread | 1 | 1 | PASS |
| GGN staff cannot read that thread | 0 | 0 | PASS |
| Driver A's session holds a slot for driver B's shipment (compat trigger fails closed) | 1 | 1 | PASS |

## 6. Hold expiry and notification de-duplication

| Check | Result |
|---|---|
| Second driver while the first hold is live | REJECTED: slot held (LOST) |
| Second driver after the first hold expired (3-min TTL passed) | ACCEPTED; expired hold released in the same transaction (first driver active holds = 0) |
| Relay retry creates a second WhatsApp notification for the same event | REJECTED: one notification per event and channel |
| RETRY without next_retry_at | REJECTED: retry must be scheduled |

## 7. Least privilege: grants limit operations, RLS limits rows

1 = the database refused for lack of privilege; 0 = allowed. Denials caused by RLS are not counted here.

| Role and attempt | Got | Expected | Result |
|---|---:|---:|---|
| Agent UPDATEs dock_occupancy directly | 1 | 1 | PASS |
| Agent INSERTs an appointment directly | 1 | 1 | PASS |
| Agent UPDATEs a notification | 1 | 1 | PASS |
| Agent approves its own booking request (approval_requests) | 1 | 1 | PASS |
| Agent reads Step Functions task tokens | 1 | 1 | PASS |
| Agent clears the human-takeover flag | 1 | 1 | PASS |
| Agent pauses or un-pauses a facility | 1 | 1 | PASS |
| Agent DELETEs a chat message | 1 | 1 | PASS |
| Console UPDATEs dock_occupancy directly | 1 | 1 | PASS |
| Console moves an escalation to another facility (column not granted) | 1 | 1 | PASS |
| Console acknowledges an escalation (granted column) | 0 | 0 | PASS |
| System job writes dock_occupancy directly | 1 | 1 | PASS |
| Any app role UPDATEs booking_events | 1 | 1 | PASS |
| Agent calls the sweeper function (not granted) | 1 | 1 | PASS |
| Agent places a hold through place_hold() (intended path) | 0 | 0 | PASS |
| Agent scoped to GGN calls place_hold() on a JAI option (RLS applies inside the definer function) | 1 | 1 | PASS |
| Functions executable by PUBLIC | 0 | 0 | PASS |
| SECURITY DEFINER functions not owned by setuhaul_rpc or without a pinned search_path | 0 | 0 | PASS |
| App roles that can become the function owner | 0 | 0 | PASS |
| DELETE / TRUNCATE grants to any app role | 0 | 0 | PASS |

## 8. Notification recovery resend is enforced once (gate G2, database part)

| Scenario | Got | Expected | Result |
|---|---|---|---|
| Repeated timeouts. PENDING → SENDING | allowed | allowed | PASS |
| Repeated timeouts. Provider times out: SENDING → UNKNOWN | allowed | allowed | PASS |
| Repeated timeouts. Grace passed, recovery resend: UNKNOWN → SENDING | allowed | allowed | PASS |
| Repeated timeouts. Times out again: SENDING → UNKNOWN | allowed | allowed | PASS |
| Repeated timeouts. Second recovery resend: UNKNOWN → SENDING | DENIED | DENIED | PASS |
| Repeated timeouts. Escalate instead: UNKNOWN → FAILED | allowed | allowed | PASS |
| Repeated timeouts. Re-open a FAILED notification: FAILED → SENDING | DENIED | DENIED | PASS |
| Repeated timeouts. Final state: status, recovery resends used | FAILED, 1 | FAILED, 1 | PASS |
| UNKNOWN records when and until when to wait | since set=True, deadline in future=True | since set=True, deadline in future=True | PASS |
| Delayed webhook arrives: UNKNOWN → DELIVERED | allowed | allowed | PASS |
| Late duplicate webhook or worker re-sends: DELIVERED → SENDING | DENIED | DENIED | PASS |
| Worker crashed mid-send (SENDING for 10 min): reaper moves it to UNKNOWN, not PENDING | 1 reaped, now UNKNOWN | 1 reaped, now UNKNOWN | PASS |
| Set unknown_resend_count to 2 | DENIED | DENIED | PASS |
| Reset the resend counter to 0 to allow another resend | DENIED | DENIED | PASS |

_Total runtime 133.2 s._
