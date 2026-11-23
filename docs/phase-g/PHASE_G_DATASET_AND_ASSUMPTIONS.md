# Phase G4: dataset and assumptions

## Foundation
* **Trainer dataset:** `docs/brief/21182924-SetuHaul_Data_Package.zip` (SQLite snapshot, data dictionary, database guide,
  ER diagram). The repository copy `data/setuhaul_freight_operations.db` is the same file. 18 tables, 4 Aug 2026,
  facilities FAC-JAI-01 (6 docks) and FAC-GGN-01 (3 docks), 21 shipments, 15 drivers, 20 appointments, 3 dock events.
* **Pilot seed:** `setuhaul/aws/synthetic_seed.py` (two facilities, four standard docks, hourly slots; the `SYN-` / `+999`
  identity rule that fails closed). Unchanged.

## Builder
`setuhaul/adapters/phase_g_dataset.py` (`python -m setuhaul.adapters.phase_g_dataset "<local dsn>"`) copies the trainer
snapshot and loads it through the existing `pg_seed.seed` loader, i.e. through every database constraint. Changes, all
mechanical:

| # | Change | Why |
|---|---|---|
| 1 | `SYN-` prefix on carrier, driver, vehicle, shipment, order and thread ids (all references) | synthetic marking (same rule as the pilot) |
| 2 | Driver phones → `+999…` (ITU-reserved, never routable); names, licences, registrations → synthetic labels | no real person or number |
| 3 | Every timestamp moved by **whole days** to the target day (default: today, IST) | runs at any date; whole days keep every time-of-day rule (06:00–22:00, 21:00 last new start, shifts) |
| 4 | `facility_config.shifts[*].crews` from `phase_g_assumptions.json` | labour (HC07) is not in the trainer data |
| 5 | Extra hourly OPEN slots `SYN-SLOT-…` for the next 3 days on every trainer dock (never replacing a trainer slot) | load test and evaluation at any time of day |

## Coverage (measured by the builder; `coverage_pct`)

| Trainer records | In the trainer data | Carried | Note |
|---|---|---|---|
| carriers / drivers / vehicles | 4 / 15 / 15 | 4 / 15 / 15 | |
| facilities / docks | 2 / 9 | 2 / 9 | |
| shipments | 21 | 21 | |
| active appointments (PENDING / CONFIRMED / IN_PROGRESS) | 14 | 10 | 4 refused by the database: APT1004 and APT1015A overlap another booking (exclusion constraint), APT1005 and APT1014A exceed the dock weight limit (the trainer's known-bad rows) |
| dock events | 3 | 3 | |
| chat threads | 12 | 12 | |
| **Total applicable** | **95** | **91 (95.8 %)** | historic appointments (6), chat message bodies, operational e-mails and facility contacts are not loaded (not operational state; personal data) |

(The builder prints the exact figures of each run; the run used for this report is in
`PHASE_G_FINAL_VALIDATION_REPORT.md`.)

## Assumptions (configurable: `setuhaul/adapters/phase_g_assumptions.json`)

| Assumption | Value | Used by | Basis |
|---|---|---|---|
| Labour crews, FAC-JAI-01 | DAY 06–14: 4, NIGHT 14–22: 3 | HC07 | assumption; the trainer data has no roster. 6 docks, so labour can bind at peaks |
| Labour crews, FAC-GGN-01 | DAY 07–15: 2, NIGHT 15–21: 2 | HC07 | assumption (3 docks) |
| Crews when a facility has no roster | `null` = one per dock (never binding) | HC07 | conservative default for the pilot (no roster there) |
| Facility capacity (concurrent unloads) | JAI 5, GGN 3 | HC10 | assumption (yard/forklift limit) |
| Snapshot max age | 120 s | STALE_DATA | the service reads fresh data per request; the limit guards cached callers |
| ETA stale after | 240 min | warning | matches the Frame ETA-signal decay assumptions |
| Optimizer time limit | 2 000 ms | timeout | far above the measured p95 |
| Perishable | `temperature_control_required` = perishable | wait factor ×2 | the trainer schema has no separate perishability flag |
| Time-critical | `priority_code` CRITICAL/HIGH | weight 8/4 | trainer priority codes |

Shift boundaries for the pilot environment stay as seeded (two 12-hour shifts, no crews), so the pilot's behaviour
would not change even with the optimizer switched on.
