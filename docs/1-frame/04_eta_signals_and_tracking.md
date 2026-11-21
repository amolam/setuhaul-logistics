# Phase 1 · Frame — Independent ETA Signals (beyond the driver's word)

**Why this exists.** A driver's message is evidence, not truth. Drivers are often optimistic, may under-report a delay to avoid penalties, and cannot be verified when they report an accident or breakdown. The snapshot has no live GPS (brief: *"0 live GPS feeds"*). This section adds **cost-optimal, independent signals** that corroborate or contradict what the driver says, plus a **deterministic fusion rule** that turns them into one ETA with a stated confidence.

> **Verification status.** Signal capabilities were checked against public sources (cited at the end). Indian vendor prices are **vendor-published or vendor-claimed figures, not quotes**. Treat every ₹ value as indicative until procurement gets a quote.

---

## 1. Signal inventory

| # | Signal | What it proves | Freshness | Coverage | Driver effort | Indicative cost | Trust |
|---|---|---|---|---|---|---|---|
| S0 | **Planned ETA** (TMS, `original_eta_ts`) | The plan, not the truck | Static | 100 % | none | Free (already have) | Low once the trip starts |
| S1 | **Driver declaration** (chat) | What the driver *says* | On demand | 100 % | types a message | Free | Low–medium. Cannot be verified alone |
| S2 | **WhatsApp location pin on request** (`location_request_message`) → route ETA | A one-time position fix, shared by the driver | On demand | Drivers on WhatsApp | one tap | Messaging cost per current Meta pricing, plus one route calculation | Medium–high. The position is real; it is a single point in time |
| S3 | **FASTag toll-plaza crossings** (via ULIP or a licensed aggregator) | The vehicle passed toll plaza X at time T | Event-based, only at toll plazas; data kept ~72 h | Tolled national and state highways | none (passive) | Credit or per-lookup pricing; vendor quote needed | High for "was here at T". Gaps between plazas |
| S4 | **SIM-based tracking** (telecom location, consent by SMS) | Approximate position from cell towers | Pings every 15–60 min | Any mobile phone, no app | one-time consent per trip | Vendor claim: ~₹1–2 per truck per day on credit plans | Medium: accuracy from hundreds of metres to kilometres |
| S5 | **Telematics GPS** (devices already fitted by SetuHaul or the carrier, via API) | Continuous precise position, speed, ignition | 30 s – 5 min | Company-owned trucks and large carriers | none | Hardware ₹3,000–8,000 + ₹150–300 per month (vendor figures). **₹0 marginal** where a device already exists | High |
| S6 | **E-way bill** (NIC, via ULIP) | Vehicle number on the consignment, declared distance and validity | On update | All GST consignments above the threshold | none | Via the ULIP partner | Identity and route check, not position |
| S7 | **Gate events**: check-in (have), ANPR camera (optional) | The truck actually arrived | Real time | Our facilities | none | Existing gate process; ANPR is optional capex | Ground truth for arrival |
| S8 | **Traffic- and road-aware route time** (routing service from the last known point) | How long the rest of the trip should take | On demand | Anywhere a fix exists | none | Per-request routing price (AWS Location Service or similar) | Model, not observation |
| S9 | **Historical ETA slip per lane, carrier and time of day** | How wrong declarations usually are | Batch | Grows with history | none | Free (our own data) | Calibrates confidence. ML candidate in a later phase |

**Not usable:** WhatsApp **live** location. The WhatsApp Business / Cloud API delivers only one-time location messages, not live-location streams, so S2 must be a pin requested each time.

## 2. Cost-optimal strategy: pay only when a signal can change a decision

The expensive question is not "where is every truck", but "is this truck's promise still true, and does the answer change what we do?" So signals are **tiered and event-triggered**, never continuous by default.

| Tier | When it runs | Signals | Why it is cheap |
|---|---|---|---|
| **T0 Always** | Every shipment | S0 plan, S7 gate events, S1 when the driver writes, S5 where a device already exists (push or poll at low frequency) | No new spend |
| **T1 Decision horizon** | The truck is within `signal_horizon_h` (default 4 h) of its slot, **or** an exception is open | S3 FASTag polling every `fastag_poll_min` (default 30 min) | Passive and per-lookup. Only ~30–40 % of daily loads are inside a horizon at once |
| **T2 Doubt** | The declared ETA disagrees with T1 evidence by more than `eta_conflict_min` (default 30), **or** confidence is LOW on a HIGH/CRITICAL load | S2 location-pin request (one tap) plus S8 route ETA | One message and one route call, only when needed |
| **T3 Watch** | HIGH or CRITICAL priority, reefer, or a route with no toll plazas, **and** T2 failed (no pin, or still contradictory) | S4 SIM tracking, trip-scoped consent, stopped at gate-in | Paid per truck-day, applied to a small minority |
| **T4 Human** | Safety keyword, accident claim, or contradictory evidence that persists | Ops call. Signals are shown as evidence only | People are the scarcest resource, so they come last |

**Rough volume at SetuHaul scale** (indicative; prices need quotes):

| Item | Assumption | Per day |
|---|---|---|
| Loads | 280–360 | — |
| T1 FASTag lookups | ~35 % of loads × ~4 lookups | ~400–500 lookups |
| T2 location pins and route calls | ~15–25 exceptions on a normal day, ~50 % doubtful | ~10–15 pins and ~30 route calls |
| T3 SIM truck-days | ≤ 10 % of loads | ≤ ~36 truck-days → ~₹40–70 per day at the vendor-claimed rate |
| T0 telematics | Existing devices only | ₹0 marginal |

The conclusion is that a full GPS rollout across contracted trucks (capex plus a monthly fee on every vehicle) is **not** cost-optimal for this problem. FASTag plus on-demand pins plus targeted SIM tracking covers the decision-relevant trucks at a small fraction of the cost. SetuHaul's own fleet uses its existing telematics.

## 3. Deterministic ETA fusion

Each signal becomes an `eta_signal` row: `(shipment_id, source, observed_at, position?, derived_eta, uncertainty_min, raw_ref)`. One deterministic function chooses the effective ETA. **No LLM is involved.**

**Precedence.** The freshest valid signal from the highest-trust source wins:

1. `GATE_IN` (S7): the truck has arrived. ETA = gate-in.
2. `OPERATIONS_OVERRIDE`: a human decision.
3. `TELEMATICS` (S5), when `observed_at` is within 15 min, plus route time (S8).
4. `LOCATION_PIN` (S2), when within 30 min, plus route time.
5. `SIM_PING` (S4), when within 45 min, plus route time, with ± accuracy added.
6. `FASTAG` (S3) last crossing, plus route time from that plaza.
7. `WAREHOUSE_ESTIMATE`.
8. `DRIVER_DECLARED` (S1).
9. `ORIGINAL_PLAN` (S0).

**Plausibility checks.** These run on every new signal and produce flags:

| Check | Rule | Effect |
|---|---|---|
| Physically impossible | Implied speed between two fixes > `max_truck_speed_kmh` (80) | Drop the newer signal and flag `SIGNAL_INCONSISTENT` |
| Optimistic declaration | Driver ETA is earlier than `last_fix + route_time − tolerance` | Use the evidence ETA. Confidence becomes LOW. Agent says: "Our latest location shows about X more minutes. Shall I plan for HH:MM?" |
| Pessimistic declaration | Driver ETA is much later than the evidence ETA | Keep the driver ETA (they may know of a stop or repair), but ask once |
| Breakdown corroboration | Driver reports a breakdown **and** no movement for > 30 min on GPS or SIM, or no FASTag progress | Mark `CORROBORATED`. It still goes to a human if safety is involved |
| Stale evidence | Newest non-driver signal is older than `signal_stale_min` | Confidence drops one level |
| Wrong vehicle | E-way bill vehicle number ≠ assigned vehicle (S6) | `CONTRADICTORY_DATA`, then escalate (connects to audit A01 / A02) |

**Confidence.** It is derived from the source and the evidence. The LLM never sets it.

| Situation | `eta_confidence` |
|---|---|
| Gate-in, or fresh telematics or pin | HIGH |
| SIM or FASTag with route time, consistent with the driver | MEDIUM–HIGH |
| Driver only, given as a precise time | MEDIUM |
| Driver only, vague or a range ("around 2 hours") | LOW |
| Signals disagree | LOW, and flagged |

**Accidents and safety.** Signals can **corroborate** that a truck stopped, but they never decide anything about safety. Any safety keyword or accident claim goes to a human at once (Boundaries: *"Driver safety decisions"*).

## 4. Privacy and consent (India, DPDP Act 2023)

Location is personal data of the driver. The design follows the commonly cited DPDP principles. **Legal review is required. This section is not legal advice.**

- **Trip-scoped purpose.** Tracking only between dispatch and gate-in for a specific shipment. Never off-duty.
- **Recorded consent** for SIM tracking (S4) and location pins (S2), with the consent text, timestamp and withdrawal path. FASTag and telematics are covered contractually with the carrier as vehicle owner. Drivers are informed.
- **Minimisation.** The LLM never receives coordinates or phone numbers, only the derived ETA and confidence. Raw positions stay in the signals store.
- **Retention.** Raw position data is purged `signal_retention_days` (default 30) after delivery. Derived ETAs are kept for audit.
- **Residency.** Raw signals are stored in ap-south-1. Model calls go through Bedrock's Global endpoint (§ HLD), so only masked, coordinate-free text leaves the region.

## 5. How it plugs into the architecture

| Component | AWS |
|---|---|
| FASTag and e-way-bill pollers (T1), SIM provider polling (T3) | EventBridge Scheduler → Lambda (per-shipment schedules created on entering the horizon, deleted at gate-in) |
| Telematics push (T0) | API Gateway (IAM / SigV4 for partners that can sign; otherwise a separate HMAC-verified endpoint) → SQS |
| Location pins (T2) | WhatsApp inbound `location` message → the same ingest queue as chat |
| Route time (S8) | Amazon Location Service routes (**availability in ap-south-1 not yet verified**; fallback is a commercial India routing API) |
| Signal store and fusion | `eta_signals` table in Aurora, plus a `fuse_eta()` function in the booking service. It emits an `EtaChanged` event that triggers re-feasibility and the scheduler |
| Partner credentials | AWS Secrets Manager |

## 6. New variables introduced (added to `02_variables.md`)

`eta_signal_id`, `signal_source`, `observed_at`, `position_geohash`, `derived_eta_ts`, `uncertainty_min`, `signal_flags[]`, `last_fix_age_min`, `tracking_tier`, `tracking_consent_id`, `consent_scope`, `consent_expires_at`, `fastag_last_plaza`, `fastag_last_crossing_ts`, `eway_bill_no`, `eway_vehicle_no`.

Configuration: `signal_horizon_h`, `fastag_poll_min`, `eta_conflict_min`, `signal_stale_min`, `max_truck_speed_kmh`, `signal_retention_days`, `t3_priority_threshold`.

## Sources

- ULIP and FASTag tracking: [fr8.in on ULIP](https://www.fr8.in/blog/unified-logistics-interface-platform-ulip-india/), [Masters India FASTag API (72-hour data window)](https://docs.mastersindia.co/masters-india-apis/proof-of-delivery-pod-api/fastag-api), [LorryCare: FASTag vs GPS](https://lorrycare.com/blog/fastag-data-vs-gps-tracking), [SuperProcure: GPS / SIM / FASTag](https://www.superprocure.com/blog/gps-sim-fastag-realtime-tracking-impact-on-indian-logistics-industry/)
- SIM tracking and cost claims: [Telenity: SIM vs GPS for fleets in India](https://www.telenity.com/sim-tracking-vs-gps-fleets-india/), [Fleetx SIM tracking](https://www.fleetx.io/solutions/sim-tracking), [LorryCare: tracking 20 trucks under ₹2/day](https://lorrycare.com/blog/track-20-trucks-under-2-rupees-per-day), [Roado: GPS device costs](https://www.roado.tech/blog/why-small-trucking-companies-are-abandoning-gps-devices/)
- WhatsApp location: [Meta: location request messages](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/location-request-messages/), [Twilio: WhatsApp message features (live location unsupported)](https://jp.twilio.com:443/docs/whatsapp/message-features), [Bird: receiving WhatsApp locations](https://bird.com/docs/guides/whatsapp/receiving-whatsapp/location)
- Routing: [Amazon Location Service routes pricing](https://docs.aws.amazon.com/location/latest/developerguide/routes-pricing.html)
- DPDP and logistics (secondary, law-firm commentary): [Mondaq: DPDP compliance for logistics](https://www.mondaq.com/india/privacy-protection/1744892/dpdp-act-compliance-for-logistics-and-supply-chain-companies-in-india-gps-tracking-telematics-and-workforce-data-risks), [KSK: DPDP for logistics GPS / telematics](https://ksandk.com/data-protection-and-data-privacy/dpdp-compliance-for-logistics-gps-telematics-and-risk/)
