## Scenario: early, late and in-progress trucks at one facility

Facility FAC-DEMO, now 09:00, window to 21:00, input fingerprint `832a7825aa9558f7…`

### Input state

| Shipment | Priority (w) | Ready | Processing min | Compatible docks | Promised | Arrived | Flag |
|---|---|---|---|---|---|---|---|
| SHP-EARLY | NORMAL (2) | 09:00 | 50 | D1, D2, D3 | D2 10:30 | 08:45 |  |
| SHP-C | LOW (1) | 09:05 | 40 | D1, D2, D3 | — | — |  |
| SHP-A | NORMAL (2) | 09:10 | 60 | D1, D2, D3 | D2 09:15 | — |  |
| SHP-D | NORMAL (2) | 09:20 | 60 | D1, D2, D3 | D3 09:30 | — |  |
| SHP-LATE | HIGH (4) | 10:35 | 60 | D1, D2, D3 | D1 09:45 | — |  |
| SHP-B | CRITICAL (8) | 10:40 | 70 | D1, D2, D3 | D1 10:50 | — |  |

Fixed (never moved):

| Dock | From | To | Kind | Ref |
|---|---|---|---|---|
| D1 | 08:20 | 09:40 | IN_DOCK | APT-UNLOADING |

### Metrics

| Rule | weighted_wait | total_wait | max_wait | missed_promises | changed_promises | kept_promises | unscheduled | moves | moves_in_a_cycle | score |
|---|---|---|---|---|---|---|---|---|---|---|
| FCFS | 95 | 65 | 35 | 1 | 5 | 0 | 0 | 5 | 3 | 725 |
| EARLIEST_ETA | 112 | 56 | 30 | 1 | 4 | 1 | 0 | 4 | 3 | 796 |
| FACILITY_SCORE | 185 | 110 | 75 | 1 | 1 | 4 | 0 | 1 | 0 | 615 |

### Orderings

**FCFS**

| # | Shipment | Dock | Start | End | Action | Apply | Wait | w·wait | late term | change | Score | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | SHP-EARLY | D2 | 09:00 | 09:50 | MOVE | swap cycle: a person | 0 | 0 | 0 | 30 | 30 | FCFS position 1: early: ready 09:00, promised D2 10:30; D2 09:00 is free and lowers its score 180 → 30 (saves 180 w·min, costs one change) |
| 2 | SHP-A | D3 | 09:10 | 10:10 | MOVE | swap cycle: a person | 0 | 0 | 0 | 30 | 30 | FCFS position 2: promised D2 09:15 is not usable (occupied by SHP-EARLY); next suitable dock time D3 09:10 |
| 3 | SHP-C | D1 | 09:40 | 10:20 | PLAN | — | 35 | 35 | 0 | 0 | 35 | FCFS position 3: no booking yet; ready 09:05; earliest suitable dock time D1 09:40 |
| 4 | SHP-D | D2 | 09:50 | 10:50 | MOVE | swap cycle: a person | 30 | 60 | 80 | 30 | 170 | FCFS position 4: promised D3 09:30 is not usable (occupied by SHP-A); next suitable dock time D2 09:50 |
| 5 | SHP-LATE | D1 | 10:35 | 11:35 | MOVE | 2 (after SHP-B) | 0 | 0 | 400 | 30 | 430 | FCFS position 5: late: ready 10:35, after promised D1 09:45 + 30 min grace; next suitable dock time D1 10:35 |
| 6 | SHP-B | D3 | 10:40 | 11:50 | MOVE | 1 | 0 | 0 | 0 | 30 | 30 | FCFS position 6: promised D1 10:50 is not usable (occupied by SHP-LATE); next suitable dock time D3 10:40 |

**EARLIEST_ETA**

| # | Shipment | Dock | Start | End | Action | Apply | Wait | w·wait | late term | change | Score | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | SHP-EARLY | D2 | 09:00 | 09:50 | MOVE | swap cycle: a person | 0 | 0 | 0 | 30 | 30 | EARLIEST_ETA position 1: early: ready 09:00, promised D2 10:30; D2 09:00 is free and lowers its score 180 → 30 (saves 180 w·min, costs one change) |
| 2 | SHP-C | D3 | 09:05 | 09:45 | PLAN | — | 0 | 0 | 0 | 0 | 0 | EARLIEST_ETA position 2: no booking yet; ready 09:05; earliest suitable dock time D3 09:05 |
| 3 | SHP-A | D1 | 09:40 | 10:40 | MOVE | swap cycle: a person | 30 | 60 | 100 | 30 | 190 | EARLIEST_ETA position 3: promised D2 09:15 is not usable (occupied by SHP-EARLY); next suitable dock time D1 09:40 |
| 4 | SHP-D | D3 | 09:46 | 10:46 | MOVE | 1 | 26 | 52 | 64 | 30 | 146 | EARLIEST_ETA position 4: promised D3 09:30 is not usable (occupied by SHP-C); next suitable dock time D3 09:46 |
| 5 | SHP-LATE | D2 | 10:35 | 11:35 | MOVE | swap cycle: a person | 0 | 0 | 400 | 30 | 430 | EARLIEST_ETA position 5: late: ready 10:35, after promised D1 09:45 + 30 min grace; next suitable dock time D2 10:35 |
| 6 | SHP-B | D1 | 10:40 | 11:50 | KEEP | — | 0 | 0 | 0 | 0 | 0 | EARLIEST_ETA position 6: keeps its promise D1 10:50 (ready 10:40, docks at 10:40) |

**FACILITY_SCORE**

| # | Shipment | Dock | Start | End | Action | Apply | Wait | w·wait | late term | change | Score | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | SHP-A | D2 | 09:10 | 10:10 | KEEP | — | 0 | 0 | 0 | 0 | 0 | pass 3: keeps its promise D2 09:15 (ready 09:10, docks at 09:10) |
| 2 | SHP-D | D3 | 09:20 | 10:20 | KEEP | — | 0 | 0 | 0 | 0 | 0 | pass 3: keeps its promise D3 09:30 (ready 09:20, docks at 09:20) |
| 3 | SHP-C | D1 | 09:40 | 10:20 | PLAN | — | 35 | 35 | 0 | 0 | 35 | pass 2: no booking yet; ready 09:05; earliest suitable dock time D1 09:40 |
| 4 | SHP-EARLY | D2 | 10:15 | 11:05 | KEEP | — | 75 | 150 | 0 | 0 | 150 | pass 3: keeps its promise D2 10:30 (ready 09:00, docks at 10:15) |
| 5 | SHP-LATE | D3 | 10:35 | 11:35 | MOVE | 1 | 0 | 0 | 400 | 30 | 430 | pass 2: late: ready 10:35, after promised D1 09:45 + 30 min grace; next suitable dock time D3 10:35 |
| 6 | SHP-B | D1 | 10:40 | 11:50 | KEEP | — | 0 | 0 | 0 | 0 | 0 | pass 3: keeps its promise D1 10:50 (ready 10:40, docks at 10:40) |

## Challenge snapshot: FAC-JAI-01 at 09:56

Facility FAC-JAI-01, now 09:56, window to 21:56, input fingerprint `cf405877789a9632…`

### Input state

| Shipment | Priority (w) | Ready | Processing min | Compatible docks | Promised | Arrived | Flag |
|---|---|---|---|---|---|---|---|
| SHP1003 | LOW (1) | 10:06 | 55 | D1, D2, D3, D4 | D1 09:00 | 08:20 | CONTRADICTORY_DATA |
| SHP1004 | NORMAL (2) | 10:06 | 70 | D1, D2, D3, D4 | D2 09:00 | 09:25 |  |
| SHP1005 | HIGH (4) | 10:06 | 85 | D2, D4 | D3 09:00 | 09:05 |  |
| SHP1007 | NORMAL (2) | 10:15 | 70 | D1, D2, D3, D4 | D1 10:00 | — | CONTRADICTORY_DATA |
| SHP1011 | NORMAL (2) | 11:05 | 100 | D6 | D6 11:00 | — |  |
| SHP1009 | CRITICAL (8) | 11:10 | 85 | D1, D2, D3, D4 | D4 11:00 | — |  |
| SHP1010 | HIGH (4) | 11:15 | 65 | D5 | D5 11:00 | — |  |
| SHP1012 | NORMAL (2) | 11:20 | 55 | D1, D2, D3, D4 | — | — |  |
| SHP1006 | HIGH (4) | 11:30 | 55 | D1, D2, D3, D4 | D4 10:00 | — | CONTRADICTORY_DATA |
| SHP1016 | HIGH (4) | 12:10 | 100 | D6 | — | — | CONTRADICTORY_DATA |
| SHP1018 | LOW (1) | 12:15 | 55 | D1, D2, D3, D4 | — | — | CONTRADICTORY_DATA |
| SHP1017 | LOW (1) | 13:25 | 60 | D1, D2, D3, D4 | D1 12:00 | — |  |
| SHP1015 | HIGH (4) | 17:55 | 70 | D5 | D5 17:00 | — |  |
| SHP1020 | NORMAL (2) | 18:25 | 70 | D1, D2, D3, D4 | D2 18:00 | — |  |

Fixed (never moved):

| Dock | From | To | Kind | Ref |
|---|---|---|---|---|
| D1 | 11:00 | 12:25 | REQUEST_PENDING | APT1014A |
| D2 | 08:08 | 10:06 | IN_DOCK | APT1002 |
| D2 | 11:00 | 12:10 | REQUEST_PENDING | APT1013A |
| D3 | 09:15 | 13:00 | BLOCK | DEVT001 |
| D5 | 18:00 | 21:56 | BLOCK | DEVT002 |

### Metrics

| Rule | weighted_wait | total_wait | max_wait | missed_promises | changed_promises | kept_promises | unscheduled | moves | moves_in_a_cycle | score |
|---|---|---|---|---|---|---|---|---|---|---|
| FCFS | 870 | 210 | 85 | 8 | 5 | 2 | 6 | 5 | 0 | 35922 |
| EARLIEST_ETA | 780 | 210 | 85 | 8 | 5 | 2 | 6 | 5 | 0 | 35592 |
| FACILITY_SCORE | 924 | 338 | 139 | 7 | 4 | 3 | 6 | 4 | 0 | 35954 |

### Orderings

**FCFS**

| # | Shipment | Dock | Start | End | Action | Apply | Wait | w·wait | late term | change | Score | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | SHP1005 | D4 | 10:06 | 11:31 | MOVE | 3 (after SHP1009) | 0 | 0 | 528 | 30 | 558 | FCFS position 1: late: ready 10:06, after promised D3 09:00 + 30 min grace; next suitable dock time D4 10:06 |
| 2 | SHP1011 | D6 | 11:05 | 12:45 | KEEP | — | 0 | 0 | 20 | 0 | 20 | FCFS position 4: keeps its promise D6 11:00 (ready 11:05, docks at 11:05) |
| 3 | SHP1010 | D5 | 11:15 | 12:20 | KEEP | — | 0 | 0 | 120 | 0 | 120 | FCFS position 6: keeps its promise D5 11:00 (ready 11:15, docks at 11:15) |
| 4 | SHP1004 | D4 | 11:31 | 12:41 | MOVE | 4 (after SHP1009) | 85 | 170 | 604 | 30 | 804 | FCFS position 2: late: ready 10:06, after promised D2 09:00 + 30 min grace; next suitable dock time D4 11:31 |
| 5 | SHP1012 | D2 | 12:10 | 13:05 | PLAN | — | 50 | 100 | 0 | 0 | 100 | FCFS position 3: no booking yet; ready 11:20; earliest suitable dock time D2 12:10 |
| 6 | SHP1009 | D1 | 12:25 | 13:50 | MOVE | 2 (after SHP1017) | 75 | 600 | 1360 | 30 | 1990 | FCFS position 5: promised D4 11:00 is not usable (occupied by SHP1005); next suitable dock time D1 12:25 |
| 7 | SHP1017 | D2 | 13:25 | 14:25 | MOVE | 1 | 0 | 0 | 170 | 30 | 200 | FCFS position 7: late: ready 13:25, after promised D1 12:00 + 30 min grace; next suitable dock time D2 13:25 |
| 8 | SHP1020 | D2 | 18:25 | 19:35 | MOVE | 5 | 0 | 0 | 100 | 30 | 130 | FCFS position 9: running late within the 30 min grace: ready 18:25; same dock at D2 18:25 |

Unscheduled (for a person): SHP1003 CONTRADICTORY_DATA; SHP1006 CONTRADICTORY_DATA; SHP1007 CONTRADICTORY_DATA; SHP1015 NO_CAPACITY_IN_HORIZON; SHP1016 CONTRADICTORY_DATA; SHP1018 CONTRADICTORY_DATA

**EARLIEST_ETA**

| # | Shipment | Dock | Start | End | Action | Apply | Wait | w·wait | late term | change | Score | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | SHP1005 | D4 | 10:06 | 11:31 | MOVE | 2 (after SHP1009) | 0 | 0 | 528 | 30 | 558 | EARLIEST_ETA position 1: late: ready 10:06, after promised D3 09:00 + 30 min grace; next suitable dock time D4 10:06 |
| 2 | SHP1011 | D6 | 11:05 | 12:45 | KEEP | — | 0 | 0 | 20 | 0 | 20 | EARLIEST_ETA position 3: keeps its promise D6 11:00 (ready 11:05, docks at 11:05) |
| 3 | SHP1010 | D5 | 11:15 | 12:20 | KEEP | — | 0 | 0 | 120 | 0 | 120 | EARLIEST_ETA position 5: keeps its promise D5 11:00 (ready 11:15, docks at 11:15) |
| 4 | SHP1004 | D4 | 11:31 | 12:41 | MOVE | 3 (after SHP1009) | 85 | 170 | 604 | 30 | 804 | EARLIEST_ETA position 2: late: ready 10:06, after promised D2 09:00 + 30 min grace; next suitable dock time D4 11:31 |
| 5 | SHP1009 | D2 | 12:10 | 13:35 | MOVE | 1 | 60 | 480 | 1120 | 30 | 1630 | EARLIEST_ETA position 4: promised D4 11:00 is not usable (occupied by SHP1005); next suitable dock time D2 12:10 |
| 6 | SHP1012 | D1 | 12:25 | 13:20 | PLAN | — | 65 | 130 | 0 | 0 | 130 | EARLIEST_ETA position 6: no booking yet; ready 11:20; earliest suitable dock time D1 12:25 |
| 7 | SHP1017 | D1 | 13:25 | 14:25 | MOVE | 4 | 0 | 0 | 170 | 30 | 200 | EARLIEST_ETA position 7: late: ready 13:25, after promised D1 12:00 + 30 min grace; next suitable dock time D1 13:25 |
| 8 | SHP1020 | D2 | 18:25 | 19:35 | MOVE | 5 | 0 | 0 | 100 | 30 | 130 | EARLIEST_ETA position 9: running late within the 30 min grace: ready 18:25; same dock at D2 18:25 |

Unscheduled (for a person): SHP1003 CONTRADICTORY_DATA; SHP1006 CONTRADICTORY_DATA; SHP1007 CONTRADICTORY_DATA; SHP1015 NO_CAPACITY_IN_HORIZON; SHP1016 CONTRADICTORY_DATA; SHP1018 CONTRADICTORY_DATA

**FACILITY_SCORE**

| # | Shipment | Dock | Start | End | Action | Apply | Wait | w·wait | late term | change | Score | Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | SHP1011 | D6 | 11:05 | 12:45 | KEEP | — | 0 | 0 | 20 | 0 | 20 | pass 1: keeps its promise D6 11:00 (ready 11:05, docks at 11:05) |
| 2 | SHP1009 | D4 | 11:10 | 12:35 | KEEP | — | 0 | 0 | 160 | 0 | 160 | pass 1: keeps its promise D4 11:00 (ready 11:10, docks at 11:10) |
| 3 | SHP1010 | D5 | 11:15 | 12:20 | KEEP | — | 0 | 0 | 120 | 0 | 120 | pass 1: keeps its promise D5 11:00 (ready 11:15, docks at 11:15) |
| 4 | SHP1005 | D2 | 12:10 | 13:35 | MOVE | 1 | 124 | 496 | 1520 | 30 | 2046 | pass 2: late: ready 10:06, after promised D3 09:00 + 30 min grace; next suitable dock time D2 12:10 |
| 5 | SHP1004 | D1 | 12:25 | 13:35 | MOVE | 3 (after SHP1017) | 139 | 278 | 820 | 30 | 1128 | pass 2: late: ready 10:06, after promised D2 09:00 + 30 min grace; next suitable dock time D1 12:25 |
| 6 | SHP1012 | D4 | 12:35 | 13:30 | PLAN | — | 75 | 150 | 0 | 0 | 150 | pass 2: no booking yet; ready 11:20; earliest suitable dock time D4 12:35 |
| 7 | SHP1017 | D3 | 13:25 | 14:25 | MOVE | 2 | 0 | 0 | 170 | 30 | 200 | pass 2: late: ready 13:25, after promised D1 12:00 + 30 min grace; next suitable dock time D3 13:25 |
| 8 | SHP1020 | D2 | 18:25 | 19:35 | MOVE | 4 | 0 | 0 | 100 | 30 | 130 | pass 1: running late within the 30 min grace: ready 18:25; same dock at D2 18:25 |

Unscheduled (for a person): SHP1003 CONTRADICTORY_DATA; SHP1006 CONTRADICTORY_DATA; SHP1007 CONTRADICTORY_DATA; SHP1015 NO_CAPACITY_IN_HORIZON; SHP1016 CONTRADICTORY_DATA; SHP1018 CONTRADICTORY_DATA

