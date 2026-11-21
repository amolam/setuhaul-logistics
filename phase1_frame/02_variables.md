# Phase 1 · Frame — Variables Catalogue

Every variable the SetuHaul exception service touches, grouped by **who produces it**. That grouping is what keeps the LLM out of decisions.

**Owner key**

| Owner | Meaning |
|---|---|
| `LLM` | Extracted from free text by the model. **Always** passed through a schema and a deterministic validator before use |
| `DB` | Read from the system of record (deterministic lookup) |
| `CODE` | Computed by deterministic code from DB and validated LLM values |
| `STATE` | Persisted state owned by one service and changed only through its transitions |
| `CFG` | Configuration / policy parameter (versioned, per facility) |
| `HUMAN` | Set only by an ops or warehouse person |
| `PLATFORM` | Infrastructure-level (auth, tracing, idempotency) |

Naming: `snake_case`. Timestamps are `timestamptz` in IST. Durations are integer minutes.

---

## 1. Conversation inputs (from the driver's free text)

The agent fills these through one structured tool call, `record_driver_report`, which has a JSON schema. Code then validates them. If a required value is missing or ambiguous, the agent asks; it never guesses.

| Variable | Type | Owner | Required before | Validation (deterministic) | Seed example |
|---|---|---|---|---|---|
| `intent` | enum `REPORT_DELAY · ASK_OPTIONS · BOOK · CHECK_STATUS · EARLY_ARRIVAL · CANCEL · OTHER` | LLM | any tool that writes | Enum check. `BOOK` needs an explicit yes in the current turn | MSG018 → ASK_OPTIONS |
| `shipment_ref_mentioned` | text, optional | LLM | — | Must match one of the driver's live `order_reference` / `shipment_id` | MSG017 asks for ORD-…-004 or -020 |
| `delay_statement_raw` | text | LLM | — | Stored verbatim for audit | "late by one hour" |
| `delay_kind` | enum `ARRIVAL_TIME · DELAY_DURATION · REPAIR_DURATION · RANGE · UNKNOWN` | LLM | ETA derivation | If `REPAIR_DURATION` or `UNKNOWN`, ask: "Is 90 minutes the repair time or the total delay?" | Brief example "Tyre damaged… around 90 minutes late" |
| `declared_eta_ts` | timestamptz, optional | LLM → CODE | options | Must be ≥ `now` − 15 min and ≤ `now` + 24 h, and later than the departure time | MSG001 → 11:20 |
| `declared_eta_lo_ts` / `declared_eta_hi_ts` | timestamptz | LLM → CODE | options when a range is given | lo ≤ hi, window ≤ 3 h | ETA012 12:30–13:00 |
| `declared_delay_min` | int, optional | LLM | — | 0–1440. Converted to an ETA by code: `base_eta + delay` | MSG016 → 45 |
| `earliest_start_ts` | timestamptz, optional | LLM | — | ≥ ETA | MSG001 "after 12" |
| `latest_finish_ts` | timestamptz, optional | LLM | — | > `earliest_start_ts`. Hard constraint if given | MSG003 "leave before 1:30" |
| `latest_finish_kind` | enum `GATE_OUT · UNLOAD_END · UNLOAD_START · UNKNOWN` | LLM | booking when `latest_finish_ts` is set | If UNKNOWN, ask (MSG004) | MSG004 |
| `exception_type` | enum `DELAY · BREAKDOWN · TRAFFIC · WEATHER · EARLY_ARRIVAL · DOCK_UNAVAILABLE · UNKNOWN` | LLM | — | Enum check | EXC002 BREAKDOWN |
| `vehicle_fixed` | bool, optional | LLM | — | ~~Breakdown with "not fixed" → escalate (safety)~~ Superseded by owner decision OD-2 (decision T40, 18 Nov 2026): not fixed → BREAKDOWN escalation without takeover; danger → safety takeover; safety unknown → ask | "Tyre repaired" |
| `safety_flag` | bool | LLM **and** CODE | — | Keyword list plus model flag. Either one true → human takeover | accident, injury, fire |
| `selected_option_id` | text | LLM | hold / book | Must be an `option_id` shown in this conversation and not expired | "the second one works" → option 2 |
| `explicit_confirmation` | bool | LLM | book | True only for an unambiguous yes **to a specific option** | "Take it" |
| `language` | ISO code | LLM | — | — | hi / en |

## 2. Context looked up from the backend (never asked of the driver)

| Variable | Type | Owner | Source |
|---|---|---|---|
| `driver_id` | text | DB | `drivers.phone` = verified sender number |
| `driver_status` | enum | DB | `drivers.driver_status` (must be ACTIVE) |
| `live_shipments[]` | list | DB | `shipments WHERE driver_id = ? AND current_status NOT IN (CANCELLED, COMPLETED)` |
| `shipment_id` | text | CODE | The single live shipment, or the one the driver picked from the list |
| `shipment_status` | enum | DB | `shipments.current_status` |
| `destination_facility_id` | text | DB | `shipments` |
| `priority_code` | enum `LOW · NORMAL · HIGH · CRITICAL` | DB | `shipments` |
| `load_weight_kg` | int | DB | `shipments` |
| `required_dock_type` | enum | DB | `shipments` |
| `temperature_control_required` | bool | DB | `shipments` |
| `expected_unload_min` | int | DB | `shipments` |
| `vehicle_type_code`, `vehicle_capacity_kg`, `refrigeration_capable` | — | DB | `vehicles`, `vehicle_types` |
| `original_eta_ts` | timestamptz | DB | `shipments` |
| `eta_history[]` | list | DB | `eta_updates` ordered by `created_at` |
| `current_appointment` | record or null | DB | `appointments WHERE is_current AND status IN (PENDING_CONFIRMATION, CONFIRMED, IN_PROGRESS)` |
| `current_slot` | record | DB | `appointment_slots` joined to `docks` |
| `checkin` | record or null | DB | `facility_checkins` (`gate_in_ts`, `queue_state`, `actual_dock_id`) |
| `facility` | record | DB | `facilities` (`open_time`, `close_time`, `grace`) |
| `facility_rules{}` | map | DB / CFG | `facility_rules` (active, effective at `now`) |
| `dock_events[]` | list | DB | `dock_status_events` overlapping the planning horizon |
| `open_exceptions[]` | list | DB | `driver_exceptions` for this shipment |
| `pending_confirmations[]` | list | DB | appointments in PENDING_CONFIRMATION plus `operational_messages` delivery status |

## 3. Derived variables (deterministic code)

| Variable | Formula / rule | Notes |
|---|---|---|
| `now` | Injected clock | Makes scenarios replayable (A13) |
| `effective_eta_ts` | Precedence: `OPERATIONS_OVERRIDE` > `WAREHOUSE_ESTIMATE` > latest `DRIVER_DECLARED` > `ORIGINAL_PLAN`. Within a source, the latest `created_at` wins | `v_latest_eta` uses latest-overall. We add source precedence (Design decision 3) |
| `eta_confidence` | `HIGH` if a point time was given; `MEDIUM` if derived from a duration; `LOW` if vague or a range; dropped one level if the declaration is older than 60 min | |
| `eta_window` | `[eta_lo, eta_hi]` = the declared range, else `eta ± CFG.eta_band_min[confidence]` | LOW → ±30, MEDIUM → ±15, HIGH → ±0 |
| `arrived` | `checkin.gate_in_ts IS NOT NULL` | Gate-in beats any ETA |
| `ready_ts` (release time) | `arrived ? max(now, gate_in_ts) + gate_to_dock_min[f] : max(now, eta_hi) + gate_to_dock_min[f]` | Plan against the pessimistic edge of the window |
| `delay_vs_appointment_min` | `ready_ts − current_slot.start` (signed) | Negative means early (A26) |
| `appointment_at_risk` | `ready_ts + occupancy_min > current_slot.end + grace_min` | Triggers a re-schedule offer (A06) |
| `no_show_deadline_ts` | `slot_start + NO_SHOW_GRACE_MIN` | Sweeper job |
| `early_checkin_allowed` | `gate_in_ts ≥ slot_start − CHECKIN_EARLY_LIMIT_MIN` | RULE001 |
| `required_capability` | `REEFER` if `temperature_control_required`; else `HEAVY` if `load_weight_kg > HEAVY_DOCK_REQUIRED_KG` or `required_dock_type = HEAVY`; else `STANDARD` | Fixes the hard-coded "D5" (A17) |
| `dock_compatible(dock)` | type matches `required_capability` (STANDARD loads may **not** take REEFER or HEAVY unless `CFG.allow_downgrade`) **and** `load_weight_kg ≤ max_vehicle_weight_kg` **and** (`!temp_required` or `supports_refrigerated`) **and** `dock.facility_id = destination_facility_id` | Catches A03 |
| `occupancy_min` | `expected_unload_min + CFG.changeover_min` | |
| `slots_needed` | `ceil(occupancy_min / slot_len_min)` | Catches A04 |
| `dock_blocked(dock, t0, t1)` | Any `dock_status_events` of type BREAKDOWN, MAINTENANCE or MANUAL_BLOCK overlaps `[t0, t1)`, or any slot in the range is not OPEN | Catches A08, A09 |
| `slot_free(slot, now)` | No active appointment, **and** no hold with `expires_at > now` | Answers "what does available mean" (decision 5) |
| `needs_approval(start)` | `start_time ≥ LAST_NEW_START_TIME`, or `end > facility.close_time` | RULE005 (A15) |
| `assignment_conflict` | The driver or vehicle has another live shipment whose busy window overlaps this one | A01, A02 → no auto-booking |
| **`is_feasible(shipment, start_slot)`** | `dock_compatible ∧ ¬dock_blocked(start, start+occupancy) ∧ all slots_needed contiguous slots slot_free ∧ start ≥ ready_ts ∧ start ≥ earliest_start_ts? ∧ start+occupancy ≤ latest_finish_ts? ∧ start ≥ now ∧ ¬assignment_conflict ∧ shipment_status ∉ {CANCELLED, COMPLETED}` | Single source of truth. Called when listing options **and again at commit** |
| `option_rank_score` | Lexicographic order: (1) needs_approval false first, (2) earliest finish, (3) least change to other trucks, (4) dock code | Transparent and explainable. Priority is applied by the allocation policy, not per-driver ranking |
| `option_set_version` | Hash of `(facility_schedule_version, shipment_id, option ids)` | Detects stale options at confirmation (decision 9) |

## 4. Booking state (STATE)

### 4.1 Option, hold and appointment lifecycle

```
OPTION_SHOWN ──driver yes──► HOLD ──commit ok──► PENDING_CONFIRMATION ──warehouse ok / auto──► CONFIRMED ──gate-in──► IN_PROGRESS ──► COMPLETED
     │                         │                       │                                       │
     └ expires (no capacity    ├ TTL expires ► RELEASED├ TTL / reject ► REJECTED ► re-offer      ├ ETA infeasible ► AT_RISK ► re-schedule (old one CANCELLED, replaced_appointment_id set)
       change)                 └ re-check fails ► LOST └                                       └ grace passes ► NO_SHOW
```

| Variable | Type | Owner | Meaning |
|---|---|---|---|
| `option_id` | uuid | CODE | One displayed option. Changes no capacity |
| `option_expires_at` | ts | CODE | `shown_at + CFG.option_ttl_min` (display validity only) |
| `hold_id` | uuid | STATE | A short exclusive claim on 1..n contiguous slots |
| `hold_expires_at` | ts | STATE | `created + CFG.hold_ttl_min` |
| `hold_owner` | (thread_id, shipment_id) | STATE | Only the owner can convert the hold |
| `appointment_id` | text | STATE | System-of-record booking |
| `appointment_status` | enum `PENDING_CONFIRMATION · CONFIRMED · IN_PROGRESS · COMPLETED · CANCELLED · NO_SHOW · REJECTED` (+ `AT_RISK` as a flag) | STATE | Written only by the booking service |
| `pending_expires_at` | ts | STATE | `booked_at + CFG.warehouse_confirm_ttl_min` (A23) |
| `replaced_appointment_id` | text | STATE | History chain |
| `row_version` | int | STATE | Optimistic concurrency token per slot / appointment |
| `facility_schedule_version` | bigint | STATE | Bumped on every capacity change in a facility. Used for stale detection and to trigger the scheduler |

### 4.2 Conversation and exception state

| Variable | Type | Owner |
|---|---|---|
| `thread_id` | text | STATE |
| `thread_status` | `OPEN · WAITING_FOR_DRIVER · WAITING_FOR_WAREHOUSE · RESOLVED · ESCALATED · CLOSED` | STATE |
| `missing_fields[]` | list | CODE (what the agent must still ask) |
| `clarification_turns` | int | CODE (metric) |
| `exception_id`, `exception_status` | — | STATE |
| `dedupe_key` | `driver_id + shipment_id + hash(normalised_text) + 10-min bucket` | CODE. **Unique** |
| `shown_options[]` | list of `option_id` | STATE (AgentCore Memory, short-term) |
| `escalation_id`, `escalation_reason_code`, `handoff_record` | — | STATE. See §7 |

## 5. Facility scheduling tool variables (optional extension)

The tool reads **structured state only**. It never reads chat text.

| Business concept | Symbol | Variable | Source |
|---|---|---|---|
| Truck / shipment | job `j ∈ J` | `shipment_id` | inbound shipments for facility F in horizon H |
| Dock door | machine `m ∈ M` | `dock_id` | `docks` (ACTIVE, not blocked) |
| Latest ETA | release `r_j` | `ready_ts` | §3 |
| Gate-in | `r_j = gate_in + gate_to_dock_min[f]` | `checkin.gate_in_ts` | |
| Unload duration | processing `p_j` | `occupancy_min` | |
| Appointment | due `d_j` | `current_slot.start` (or none) | |
| Truck already unloading | fixed task | `(m, start, predicted_end = max(now, unload_start + p_j))` | `facility_checkins` IN_DOCK (A11) |
| Priority | weight `w_j` | `CFG.priority_weight[priority_code]` | |
| Compatibility | `C_jm ∈ {0,1}` | `dock_compatible` | |
| Dock unavailability | windows `U_m` | dock events and BLOCKED slots | |
| Driver limit | deadline `D_j` | `latest_finish_ts` (hard) | |
| Already promised to driver | `prev_start_j`, `prev_m_j` | current CONFIRMED appointment | |

**Decision variables:** `x_jm ∈ {0,1}` (job j on dock m), `S_j` (start time). Optionally `y_j` (job deferred or escalated).

**Objective (plain words):** less waiting, fewer missed appointments, fewer changes to promises already made.
`minimise Σ_j w_j · wait_j  +  α · Σ_j w_j · late_j  +  β · Σ_j changed_j  +  γ · Σ_j y_j`
where `wait_j = S_j − r_j`, `late_j = max(0, S_j − d_j)`, `changed_j = 1` if the dock or start moved more than `CFG.change_tolerance_min` from the promise.

**Hard constraints:** one job per dock at a time (no overlap, including changeover); `S_j ≥ r_j`; `S_j + p_j ≤ D_j`; `C_jm = 1`; no overlap with `U_m`; fixed tasks are not moved; `S_j ≤ LAST_NEW_START` unless approved.

**Baselines to compare against:** FCFS by `r_j`, earliest-due-date, weighted-priority dispatch. CP-SAT is used only if it beats these on the snapshot by a margin we can explain.

**Output:** a ranked list of proposed `(j, m, S_j)` with a reason per change. It is a **proposal**. Commits still go through the booking service.

## 6. Configuration parameters (CFG, versioned per facility)

| Parameter | Default | Source / reason |
|---|---|---|
| `slot_len_min` | 60 (HEAVY dock 90) | Seed slots |
| `changeover_min[facility]` | 10 | Assumption S3 / U3 |
| `gate_to_dock_min[facility]` | **Learned per facility** (see §6a). Seed: JAI 10, GGN 15 | Replaces a single global `yard_to_dock_min` |
| `checkin_early_limit_min` | 60 | RULE001 |
| `no_show_grace_min` | JAI 30, GGN 20 | RULE002, RULE006 (single source; `facilities.checkin_grace_min` deprecated, A16) |
| `heavy_dock_required_kg` | 25 000 | RULE004 |
| `last_new_start_time` | 21:00 (JAI); GGN unknown → close − 60 min | RULE005, A16 |
| `eta_band_min` | LOW 30 / MEDIUM 15 / HIGH 0 | Assumption S8 |
| `option_ttl_min` | 5 | E4 |
| `hold_ttl_min` | 3 | E4. Short so it cannot starve others during a spike |
| `warehouse_confirm_ttl_min[facility][shift]` | **Per facility and shift** (see §6a). Seed: JAI day 15, JAI night 10, GGN day 15, GGN night 0 (no coordinator, A22) | Replaces one global value |
| `confirm_lead_min[facility]` | 30 | The warehouse must answer at least this long before the slot starts |
| `auto_confirm_driver_chat_bookings[facility][shift]` | false (GGN night: true *only if* the client approves; otherwise escalate) | Assumption S9 |
| `priority_weight` | CRITICAL 8 · HIGH 4 · NORMAL 2 · LOW 1 | Placeholder. Decided in Design (decision 7) |
| `priority_protect_window_min` | 0 (off) | If > 0, a CRITICAL request can claim a hold slot up to N minutes before others commit. Decided in Design |
| `change_tolerance_min` | 15 | Scheduler stability |
| `max_options_shown` | 3 | Conversation brevity |
| `max_clarification_turns` | 3 | Then escalate |
| `planning_horizon_h` | 12 | Scheduler |
| `allow_dock_downgrade` | false | A STANDARD load on a REEFER or HEAVY dock needs approval |

## 6a. Per-facility timing variables (not one global value)

Every warehouse has a different yard layout, gate process, staffing and shift pattern. Timing parameters are therefore **keyed by facility** (and by shift where staffing matters), versioned, and **learned from check-in history** once enough data exists. Until then they use a conservative seed.

| Variable | Definition | How it is learned (deterministic, nightly job) | Seed today | Evidence in snapshot |
|---|---|---|---|---|
| `gate_processing_min[f]` | `yard_queue_enter_ts − gate_in_ts` | p75 over the last 14 days, n ≥ 20, else seed | JAI 2, GGN 2 | JAI: 1 min (CHK1001–1005) |
| `gate_to_dock_min[f]` | Time from being called to dock-in. **Needs a new column `called_to_dock_ts`**, because the snapshot only has `yard_queue_enter_ts` and `dock_in_ts`, which include waiting | p75 of `dock_in_ts − called_to_dock_ts`; fallback p50 of `dock_in_ts − max(yard_queue_enter_ts, slot_start)` | JAI 10, GGN 15 (no data, so conservative) | JAI: CHK1002 06 min (on time). CHK1001 19 min includes an early wait, so it is excluded. GGN: **no check-ins**, flagged |
| `changeover_min[f]` | Gap between `gate_out` of truck n and `dock_in` of truck n+1 on the same dock | p75 per dock type | 10 | Not derivable from the snapshot (U3) |
| `unload_overrun_factor[f][dock_type]` | `actual unload ÷ expected_unload_min` | p75 ratio; multiplies `occupancy_min` for planning | 1.0 | SHP1001 0.84 (42/50). SHP1002 is still in the dock past 70 min (overrun, A11) |
| `warehouse_confirm_ttl_min[f][shift]` | How long a pending request waits for the warehouse | Set by the client per facility. Monitored as p90 actual confirmation time | JAI day 15 / night 10, GGN day 15 / night **0** | OM002 still QUEUED. GGN night coordinator vacant (A22) |
| `pending_expires_at` | `min(booked_at + warehouse_confirm_ttl_min[f][shift], slot_start − confirm_lead_min[f])` | Formula | — | APT1014A booked 09:52 for 11:00 slot → expires 10:07 (JAI day) |
| `shift_of(f, t)` | Shift calendar per facility | Facility config | JAI 06–14 / 14–22, GGN 07–15 / 15–21 | `facilities.open_time` / `close_time` |
| `no_show_grace_min[f]` | From `facility_rules` only (`facilities.checkin_grace_min` deprecated, A16) | Rule table | JAI 30, GGN 20 | RULE002, RULE006 |
| `last_new_start_time[f]` | From `facility_rules` | Rule table | JAI 21:00, GGN **missing**, so close − 60 = 20:00 until set | RULE005, A16 |

**Rule.** When a learned value is below its minimum sample size, the seed is used and the option is labelled `timing_basis = SEED` in the trace. That label is visible in evals and to ops.

## 6b. Independent ETA signal variables

These variables come from `04_eta_signals_and_tracking.md`. Each signal is a row: `eta_signal_id`, `shipment_id`, `signal_source` (`GATE_IN · OPERATIONS_OVERRIDE · TELEMATICS · LOCATION_PIN · SIM_PING · FASTAG · WAREHOUSE_ESTIMATE · DRIVER_DECLARED · ORIGINAL_PLAN`), `observed_at`, `position_geohash` (never sent to the LLM), `derived_eta_ts`, `uncertainty_min`, `signal_flags[]` (`SIGNAL_INCONSISTENT · OPTIMISTIC_DECLARATION · CORROBORATED · STALE · CONTRADICTORY_DATA`), `raw_ref`.

Per shipment: `tracking_tier` (T0–T3), `tracking_consent_id`, `consent_scope`, `consent_expires_at`, `last_fix_age_min`, `fastag_last_plaza`, `fastag_last_crossing_ts`, `eway_bill_no`, `eway_vehicle_no`.

`effective_eta_ts` and `eta_confidence` in §3 are now produced by `fuse_eta()` over these signals. The source-precedence list in §3 is the degenerate case where only chat and plan exist.

## 6c. Extraction tool contract (`record_driver_report`)

This is the only tool through which free text becomes structured data. Strict JSON schema (`strict: true`, `additionalProperties: false`). Every field is nullable so the model can say "unknown" instead of guessing. Validation in §1 runs afterwards in code.

```json
{
  "name": "record_driver_report",
  "description": "Record what the driver said in this turn. Use null for anything the driver did not state. Never infer times the driver did not give.",
  "strict": true,
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["intent", "shipment_ref_mentioned", "delay_kind", "declared_eta_local", "declared_eta_range_local",
                 "declared_delay_min", "earliest_start_local", "latest_finish_local", "latest_finish_kind",
                 "exception_type", "vehicle_fixed", "safety_concern", "selected_option_id", "explicit_confirmation", "language"],
    "properties": {
      "intent": {"type": "string", "enum": ["REPORT_DELAY", "ASK_OPTIONS", "BOOK", "CHECK_STATUS", "EARLY_ARRIVAL", "CANCEL", "OTHER"]},
      "shipment_ref_mentioned": {"type": ["string", "null"], "description": "Order or shipment reference exactly as the driver wrote it"},
      "delay_kind": {"type": "string", "enum": ["ARRIVAL_TIME", "DELAY_DURATION", "REPAIR_DURATION", "RANGE", "UNKNOWN"]},
      "declared_eta_local": {"type": ["string", "null"], "description": "HH:MM 24h IST, only if the driver stated a time"},
      "declared_eta_range_local": {"type": ["array", "null"], "items": {"type": "string"}, "description": "[HH:MM, HH:MM] if a range was given"},
      "declared_delay_min": {"type": ["integer", "null"]},
      "earliest_start_local": {"type": ["string", "null"]},
      "latest_finish_local": {"type": ["string", "null"]},
      "latest_finish_kind": {"type": ["string", "null"], "enum": ["GATE_OUT", "UNLOAD_END", "UNLOAD_START", "UNKNOWN", null]},
      "exception_type": {"type": "string", "enum": ["DELAY", "BREAKDOWN", "TRAFFIC", "WEATHER", "EARLY_ARRIVAL", "DOCK_UNAVAILABLE", "UNKNOWN"]},
      "vehicle_fixed": {"type": ["boolean", "null"]},
      "safety_concern": {"type": "boolean", "description": "true for any accident, injury, fire, threat or medical issue"},
      "selected_option_id": {"type": ["string", "null"], "description": "Only an option_id previously shown in this conversation"},
      "explicit_confirmation": {"type": "boolean", "description": "true only for an unambiguous yes to one specific shown option"},
      "language": {"type": "string"}
    }
  }
}
```

The model returns local wall-clock strings only. Code converts them to `timestamptz` using `now` and the facility's timezone, and rejects impossible values. Example: MSG001 "Reaching around 11:20. Any slot after 12?" becomes `intent=ASK_OPTIONS`, `delay_kind=ARRIVAL_TIME`, `declared_eta_local="11:20"`, `earliest_start_local="12:00"`, every other field null or false.

## 6d. Worked examples

`feasibility_prototype.py` computes every derived variable from the snapshot and writes `worked_examples.md`. It contains five worked feasibility checks (SHP1006 with and without the "after 12" preference, SHP1014, SHP1015, SHP1012) and a table of derived values for all 21 shipments. Headline results:

| Shipment | Result | Why |
|---|---|---|
| SHP1006 (HIGH, ETA 11:20, wants after 12:00) | Options D1/D2/D3 13:00, **but escalate as CONTRADICTORY_DATA** | DRV006 is also assigned to SHP1021 departing Noida 10:00 (A01). No auto-booking until ops resolves it |
| SHP1014 (CRITICAL, 21 t, 75 min) | D2 13:00–14:25 or D4 13:00–14:25, each **reserving two contiguous slots** | D1 and D3 rejected on weight. The pending D1 11:00 request is infeasible (A03, A05) |
| SHP1015 (reefer, ETA 18:30) | **ESCALATE: NO_FEASIBLE_SLOT** | D5 is blocked 18:00–22:00 by maintenance. No other reefer dock at JAI |
| SHP1012 (must leave by 13:30) | **ESCALATE: NO_FEASIBLE_SLOT** | Every 12:00 slot is occupied or blocked, and 13:00 + 55 min passes 13:30. The infeasible pending APT1014A occupies D1 11:00, which shows why pending TTL and re-validation matter |
| SHP1002 (in dock) | **FIXED TASK** | Never re-slotted. The scheduler treats it as immovable |

## 7. Escalation hand-off record

| Field | Example (SHP1015) |
|---|---|
| `escalation_id` | ESC-… |
| `reason_code` | `NO_FEASIBLE_SLOT` · `SAFETY` · `CONTRADICTORY_DATA` · `AFTER_HOURS_APPROVAL` · `CRITICAL_CONFLICT` · `CLARIFICATION_LIMIT` · `PENDING_EXPIRED` · `DRIVER_REQUEST` |
| `shipment_id`, `driver_id`, `facility_id`, `priority_code` | SHP1015, DRV015, FAC-JAI-01, HIGH |
| `current_appointment` | APT1015A D5 17:00–18:00 (infeasible) |
| `effective_eta_ts`, `eta_confidence`, `eta_source` | 18:30, HIGH, DRIVER_DECLARED |
| `constraints` | REEFER, 60 min, latest finish unknown |
| `options_considered[]` with `reject_reason` | D5 18:00–22:00 maintenance (DEVT002); no other reefer dock at JAI; GGN-D3 is another facility |
| `suggested_human_actions[]` | Approve overnight cold hold · move maintenance · re-route to GGN · next-day slot |
| `driver_last_message`, `what_driver_was_told` | "…Can the reefer unload tonight?" / "Operations is reviewing, you'll hear by 16:45" |
| `sla_due_at` | `created + CFG.escalation_ack_min` |
| `notification_attempts[]` | channel, recipient, status (FAILED is visible, A21) |
| `trace_id` | Links to the full agent and tool trace |

## 8. Platform and non-functional variables

| Variable | Owner | Purpose |
|---|---|---|
| `external_message_id` | PLATFORM | Channel's message id. Ingest idempotency (A18, A19) |
| `idempotency_key` | PLATFORM | On every write tool call: `hash(thread_id, tool, args, turn_id)`. Repeated calls return the stored result |
| `correlation_id` / `trace_id` | PLATFORM | OpenTelemetry trace across channel → agent → tools → DB |
| `session_id` | PLATFORM | AgentCore Runtime session = `thread_id` |
| `principal` | PLATFORM | Authenticated caller: driver (phone-verified), ops user (Cognito), service (IAM role) |
| `tenant_scope` / `facility_scope` | PLATFORM | Row-level security for ops users |
| `model_id`, `model_route` | PLATFORM | Which model handled the turn (extraction vs reply) |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_estimate` | PLATFORM | Cost observability per conversation |
| `guardrail_action` | PLATFORM | PII masked or prompt attack blocked |
| `latency_ms` (per turn, per tool) | PLATFORM | SLOs |
| `prompt_version`, `policy_version`, `cfg_version` | PLATFORM | Reproducibility in evals and replays |

## 9. Measures (evaluation variables)

| Metric variable | Definition |
|---|---|
| `ttfo_sec` | First driver message → first usable outcome (options shown, booked or escalated) |
| `auto_resolved` | Case closed without human takeover |
| `double_booking_count` | Overlapping active occupancies on any dock. **Must be 0** |
| `stale_option_rate` | Options that fail `is_feasible` at confirmation ÷ confirmations attempted |
| `escalation_complete` | Hand-off record passes the schema with every required field present |
| `driver_wait_min` | `dock_in_ts − gate_in_ts` after rescheduling |
| `slot_utilisation` | Occupied slot-minutes ÷ open slot-minutes in window |
| `priority_violation_count` | A decision where a lower-weight job displaced a higher one against policy |
| `clarification_turns_per_case` | Agent questions ÷ resolved cases |
| `hallucinated_fact_count` | Agent reply states a time, dock or status not present in tool output (LLM-judge plus string check) |
