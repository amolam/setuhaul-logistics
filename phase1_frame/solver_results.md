# Solver spike: dispatch rules vs CP-SAT (generated)

Facility FAC-JAI-01, now = 09:56, horizon to 22:00. 16 movable trucks; in-dock truck and dock events are fixed. Objective = Σ w·wait + 2·Σ w·lateness vs promise + 30·changed promises + 2000·w·unscheduled. Weights CRITICAL 8 / HIGH 4 / NORMAL 2 / LOW 1. OR-Tools 9.15.

## Trucks in the instance

| Shipment | Priority | Ready | Occupancy | Compatible docks | Promised | Data flag |
|---|---|---|---:|---|---|---|
| SHP1003 | LOW | 10:06 | 55 | D1, D2, D3, D4 | D1 09:00 | double-assigned |
| SHP1004 | NORMAL | 10:06 | 70 | D1, D2, D3, D4 | D2 09:00 |  |
| SHP1005 | HIGH | 10:06 | 85 | D2, D4 | D3 09:00 |  |
| SHP1007 | NORMAL | 10:06 | 70 | D1, D2, D3, D4 | D1 10:00 | double-assigned |
| SHP1011 | NORMAL | 10:50 | 100 | D6 | D6 11:00 |  |
| SHP1009 | CRITICAL | 10:55 | 85 | D1, D2, D3, D4 | D4 11:00 |  |
| SHP1010 | HIGH | 11:00 | 65 | D5 | D5 11:00 |  |
| SHP1012 | NORMAL | 11:20 | 55 | D1, D2, D3, D4 | — |  |
| SHP1006 | HIGH | 11:30 | 55 | D1, D2, D3, D4 | D4 10:00 | double-assigned |
| SHP1014 | CRITICAL | 11:35 | 85 | D2, D4 | D1 11:00 |  |
| SHP1013 | NORMAL | 11:40 | 70 | D1, D2, D3, D4 | D2 11:00 |  |
| SHP1018 | LOW | 12:00 | 55 | D1, D2, D3, D4 | — | double-assigned |
| SHP1016 | HIGH | 12:10 | 100 | D6 | — | double-assigned |
| SHP1017 | LOW | 13:25 | 60 | D1, D2, D3, D4 | D1 12:00 |  |
| SHP1015 | HIGH | 17:40 | 70 | D5 | D5 17:00 |  |
| SHP1020 | NORMAL | 18:10 | 70 | D1, D2, D3, D4 | D2 18:00 |  |

## Results

| Method | Weighted wait (w·min) | Total wait (min) | Max wait | Missed promises | Changed promises | Unscheduled → escalate | Runtime |
|---|---:|---:|---:|---:|---:|---:|---|
| FCFS by ready time | 1134 | 320 | 61 | 8 | 9 | 1 | 0.1 ms |
| Priority-weighted dispatch | 1177 | 718 | 180 | 9 | 9 | 1 | 0.1 ms |
| Keep promises first (EDD) | 1296 | 418 | 100 | 9 | 9 | 1 | 0.0 ms |
| CP-SAT (OPTIMAL) | 768 | 381 | 139 | 7 | 7 | 1 | 1381 ms |

## Plans

| Shipment | FCFS by ready time | Priority-weighted dispatch | Keep promises first (EDD) | CP-SAT (OPTIMAL) |
|---|---|---|---|---|
| SHP1003 (LOW) | D1 10:06 | D3 13:00 | D4 10:06 | D4 10:06 |
| SHP1004 (NORMAL) | D2 10:06 | D1 10:06 | D1 10:06 | D1 12:25 |
| SHP1005 (HIGH) | D4 10:06 | D2 10:06 | D2 10:06 | D2 10:06 |
| SHP1007 (NORMAL) | D1 11:01 | D4 12:20 | D1 11:16 | D1 10:06 |
| SHP1011 (NORMAL) | D6 10:50 | D6 13:50 | D6 10:50 | D6 10:50 |
| SHP1009 (CRITICAL) | D2 11:16 | D4 10:55 | D2 11:31 | D4 11:01 |
| SHP1010 (HIGH) | D5 11:00 | D5 11:00 | D5 11:00 | D5 11:00 |
| SHP1012 (NORMAL) | D4 11:31 | D1 12:25 | D3 13:00 | D3 13:00 |
| SHP1006 (HIGH) | D1 12:11 | D1 11:30 | D4 11:30 | D1 11:30 |
| SHP1014 (CRITICAL) | D4 12:26 | D2 11:35 | D4 12:25 | D2 11:35 |
| SHP1013 (NORMAL) | D2 12:41 | D2 13:00 | D1 12:26 | D4 12:26 |
| SHP1018 (LOW) | D3 13:00 | D1 13:20 | D1 13:36 | D2 13:00 |
| SHP1016 (HIGH) | D6 12:30 | D6 12:10 | D6 12:30 | D6 12:30 |
| SHP1017 (LOW) | D1 13:25 | D4 13:30 | D2 13:25 | D1 13:35 |
| SHP1015 (HIGH) | **unscheduled** | **unscheduled** | **unscheduled** | **unscheduled** |
| SHP1020 (NORMAL) | D2 18:10 | D2 18:10 | D2 18:10 | D2 18:10 |
