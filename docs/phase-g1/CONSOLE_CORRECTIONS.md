# Console corrections (G5) and LangSmith links (G6)

Status: implemented, tested locally, and checked in a browser against a local API with synthetic data; **not deployed**.
Screenshots (local harness: fresh local PostgreSQL, synthetic drivers, a locally signed test token, no Cognito, no
AWS) are in `screenshots/`.

## G5.1 Status pills: live state, not hardcoded

Before: `index.html` hardcoded `booking execution OFF`, while the pilot runs with booking **ON** (public `/health`:
`booking_enabled: true`, kill switch `disengaged`).

Now three pills are filled from the public, read-only `GET /health` (no token, no new endpoint), refreshed at load,
every 60 s and on *Refresh*:

| Pill | Source | Values |
|---|---|---|
| environment | `environment` | e.g. `pilot-ap-southeast-2 · synthetic data only` |
| booking execution | `booking_enabled` | `ON · synthetic data only` / `OFF` / `status unavailable` |
| kill switch (separate) | `booking_kill_switch` | `ENGAGED (bookings refused)` / `disengaged` / `not applicable (booking off)` / `status unavailable` |

If `/health` fails or reports the database unreachable, the pills say **status unavailable**: never a guess. No pill
says "production". Screenshots 01 and 07.

## G5.2 Optimizer: the real shipment format, validated, read-only

* The placeholder `SHP-SYN-0001` (not a real format) is now `Shipment id, format SYN-SHP-XXXXXXXX`, with help text
  saying where to copy one from. No shipment id is hardcoded.
* Validation uses one pattern, `^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$`, defined in `setuhaul/api/app.py` (`SHIPMENT_ID_PATTERN`,
  enforced by the route → 422) and mirrored in `app.js` (a test keeps the two equal). An invalid id is refused in the
  browser with "Nothing was sent to the server" (screenshot 05).
* Still read-only: the recommendation note and server checks are unchanged (screenshot 06).

## G5.3 Empty states say why

| Panel | Empty because… | Text |
|---|---|---|
| Optimizer | nothing asked yet | "No recommendation yet. Enter a shipment id (format …) and press Recommend. Read only: nothing is held, booked or changed." |
| Agent metrics | no turns in the window (`status_code=NO_RECENT_TURNS`) | "No agent turns in this window … The service answered, so this is not a failure." |
| Tool calls | no turns vs turns without tools | two distinct messages |
| Any tab | request failed | "Service failure: the server did not answer this view (…). Numbers are not shown rather than guessed." + "Metrics unavailable because the request failed, not because nothing happened." (screenshot 08) |
| Any tab | 403 | "Not permitted for your role" + "This view is for planners and ops leads; the server refused it for your role." (screenshot 07) |
| Model use | status line | `NO_ACTIVITY`, `NOT_STARTED`, `PROVIDER_FAILING`, `BUDGET_REFUSED`, `DEGRADED (n of m failed…)`, `OK` |

Model use (screenshots 01–03) now separates: turns by reader (model / rules after a provider failure / after a budget
refusal / model never started / no model configured), attempts (OK / provider errors / refused by the budget, not sent
/ unsettled), failure categories (16-category taxonomy; v0.3.0's legacy names such as `auth` are mapped), tokens
(provider-reported only), and cost **provider-reported vs ESTIMATED (reservation kept)**. Each split shows
"parts add up to the total" or a red mismatch warning. The budget day boundary is printed: `00:00 UTC (05:30
Asia/Kolkata)`. No metric is fabricated. Without migration 0028 the provider/estimate split says it is unavailable.

## G6 LangSmith links (planner, ops_lead)

Shown inside Observability, which the server already restricts to `planner` and `ops_lead` (`view_observability`). No
permission was added, and `operations` still gets 403 (tested; screenshot 07). Shown (screenshot 04):

* tracing on/off; the project link (from `SETUHAUL_LANGSMITH_UI_URL`, which must be a `https://[eu.]smith.langchain.com/`
  page, or it is reported unavailable);
* latest evaluation status (from `evaluation_runs`), dataset version, evaluator version, the experiment link (if the
  run recorded one);
* failure categories;
* recent turns (facility-scoped by row-level security): time, facility, reader, failure category, ms, and a per-turn
  **trace link** (`<project>/r/<turn_ref>`) or, with tracing off, the turn_ref prefix to search CloudWatch.

The browser holds no LangSmith credential and makes no call to LangSmith. The page only renders links the server
returns, and re-checks their prefix before rendering. Tests assert that `app.js` has no `x-api-key`, `lsv2_`,
`LANGSMITH_API_KEY` or API host.

## Not changed

Sign-in (Cognito PKCE), tabs, the Requests/Escalations/Docks behaviour, and the server's role and facility checks.
`/health` still reports `version 0.2.0` (the FastAPI app's version string was never bumped for v0.3.0). Noted as a
follow-up rather than changed here, because the deployment manifest and gate evidence key on the build, not on this
string.
