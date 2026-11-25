# Driver traceability in traces, logs and the Ops view

**Status: built and tested locally on branch `feature/driver-trace-llm-judge`.** Migration 0041 has not been applied to any shared database. Nothing is deployed, and tracing is not enabled anywhere.

## What a turn now says about itself

Before this change, a trace or log line for a driver turn identified it only by `turn_ref` and a hashed `conversation_ref`. Finding "what happened to DRV-JAI-0042's turn at 14:05" meant a database query.

Each turn now carries four internal ids. They are the same everywhere the turn appears:

| Field | Value | LangSmith (root `agent_turn` metadata) | CloudWatch (`agent.turn` line) | Console (Ops view, `agent_turn_metrics`) |
|---|---|---|---|---|
| `driver_id` | internal id, e.g. `DRV-JAI-0042` | yes | yes | yes, only on rows with a facility |
| `shipment_id` | the conversation's shipment, or **missing** when the driver has several and has not chosen one | yes | yes | yes, only on rows with a facility |
| `facility_id` | e.g. `FAC-JAI-01` | yes (now set at the start of the turn as well) | yes (already) | yes (already) |
| `channel` | `telegram`, `whatsapp`, `web`, `sms`, `console` | yes | yes | no |
| `turn_ref` | the turn's UUID = LangSmith trace id | trace id | `turn_ref`, `correlation_id` | `turn_ref` (trace link) |
| `conversation_ref` | 16-hex SHA-256 of the thread id (unchanged) | yes | no | no |

**Searching for one driver:**
- **LangSmith:** filter on metadata `driver_id = "DRV-JAI-0042"`.
- **CloudWatch Logs Insights:** `filter event = "agent.turn" and driver_id = "DRV-JAI-0042"`.
- **Console:** Ops view → Recent turns → **Driver** filter. A row's trace link opens the same trace in LangSmith.

## What is never exported (unchanged, and now tested for these ids too)

The following never appear in any trace, log line or metrics row:
- driver name, phone number, e-mail;
- Telegram chat id, Telegram user id, username, link codes;
- coordinates, and the stored geohash;
- message text (still behind `SETUHAUL_TRACE_CONTENT`, synthetic only, off by default);
- session tokens and keys.

Both the trace redactor and the log cleaner now also drop the keys `chat_id`, `telegram_chat_id`, `telegram_user_id`, `username`, `lat`, `lon`, `latitude`, `longitude` and `geohash`, wherever they appear.

## The format gate (`setuhaul/observability/identifiers.py`)

**Where it applies.** The gate runs at the export boundary: in `redaction.redact`, which handles every trace payload, and in `telemetry.clean`, which handles every CloudWatch line. It applies to the keys `driver_id`, `shipment_id`, `thread_shipment_id` and `facility_id` wherever they occur.

**What passes.** An id passes only if all of these hold:
- it is 3–48 characters of `[A-Za-z0-9_-]`, starting with a letter or digit;
- it carries its kind's token: a driver id contains `DRV-`, a shipment id contains `SHP-`, a facility id starts with `FAC-`;
- it has **no run of 7 or more digits**, so a phone number, a Telegram chat id or an account number can't hide inside an id.

**What happens to the rest.** Anything else is exported as `[INVALID_ID]`. The value is never exported, and no substitute is invented. A missing id stays `null`, so "no driver known" and "a driver id that failed the gate" look different.

**The gate is not proof that an id is synthetic.** It checks format only. Traces stay synthetic because tracing refuses to start unless `SETUHAUL_DATA_CLASS=synthetic`. A test pins this.

**Known side effect.** Synthetic ids generated with random hex suffixes (`DRV-{uuid8}`) occasionally contain 7 digits in a row and then export as `[INVALID_ID]`. That is the fail-closed choice. Synthetic data with readable ids, such as `DRV-JAI-0042` or `DRV-TEST-1`, is unaffected.

**If real-data tracing is ever proposed:** replace `driver_id` with a keyed HMAC pseudonym, with the key in Secrets Manager, and have the console translate it back for authorised staff. That needs explicit approval and is not built.

## Migration 0041 (`migrations/0041_turn_metrics_driver_trace.sql`)

**Changes:**
- **Columns:** adds `agent_turn_metrics.driver_id` and `shipment_id`, both `text NULL`. Existing rows keep `NULL` and stay valid.
- **CHECK constraints:** the same gate as above, with the same regular expressions, plus one more rule: **an id is only allowed on a row that has a `facility_id`**. A row with no facility is visible to every staff scope under the existing RLS policy (`facility_id IS NULL OR scope_allows(facility_id)`), so it must never name a driver.
- **`record_agent_turn`:** replaced. It stores an id only when the id is well formed and the row has a facility. Otherwise it stores `NULL` and does not raise, because turn metrics are best effort.
- **Index:** a partial index on `(driver_id, created_at DESC)`, used by the driver filter.

**Unchanged:**
- the RLS policy;
- the append-only trigger;
- grants: SELECT for `setuhaul_console` and `setuhaul_system`, EXECUTE on `record_agent_turn` for `setuhaul_feasibility` only;
- the function owner, `setuhaul_rpc`.

No new role and no new grant. A test pins all of this.

**Recovery.** The migration is forward-only like every migration. If the columns had to go, a later migration would:
1. restore the 0027 body of `record_agent_turn`;
2. drop the two constraints, the index and the columns.

Nothing else reads these columns, and dropping them loses no other data. The migration has been applied only to local test databases.

## Ops console

**Changes:**
- **Endpoint.** `GET /console/observability` accepts `driver=<id>`. The id is format-checked; anything else gets a 422 that doesn't echo the value.
- **Recent turns table.** Gains **Driver** and **Shipment** columns, plus a driver filter with a "Show all drivers" reset.

**Access.**
- **Who can read it:** unchanged, `view_observability`, which means planner and ops_lead.
- **Who can't:** `operations` and `dispatcher` still get 403.
- **Facility scope:** RLS limits rows to the caller's facilities. A planner at Gurugram never sees a Jaipur driver's turns, even when filtering for that driver.
- **Other Ops sections:** unchanged.

## Tests (`tests/test_driver_traceability.py`, 60 tests, also run through the Data API emulator in CI)

- **The format gate:**
  - valid ids;
  - phones, phones written to look like ids, Telegram chat ids, names, e-mail, usernames, coordinates;
  - wrong-kind ids, too short, too long, wrong type;
  - missing ids.
- **Exported payloads:**
  - redaction and log cleaning on real payloads;
  - a real WhatsApp turn and a Telegram-labelled turn. Each driver message deliberately contains the phone number, the name, a chat id and coordinates. None of them appears in the trace or the log lines.
- **The same id in all three places:** one driver id and one `turn_ref` across the LangSmith root run, the CloudWatch `agent.turn` line and the metrics row. The hashed `conversation_ref` is unchanged.
- **Several drivers or shipments:**
  - two drivers at one facility;
  - one driver with two shipments and no choice made (shipment missing, not guessed);
  - a driver id that fails the gate (logged as `[INVALID_ID]`, stored as `NULL`).
- **Ops console:**
  - the column and filter;
  - filter refusals;
  - 403 for operations and dispatcher;
  - another facility sees nothing.
- **Migration 0041:**
  - the columns are nullable and existing calls still work;
  - the CHECK refuses bad ids even from a direct insert;
  - no grant or owner change.
- **Tracing off:** records nothing, but the metrics row still gets the ids.

**Pinned tests changed on purpose (`tests/test_phase_g.py`).** The `agent.turn` line used to be asserted to contain no `driver_id`. It now has to contain the format-gated id. A malformed `driver_id` such as `DRV1` is now logged as `[INVALID_ID]` instead of being dropped.
