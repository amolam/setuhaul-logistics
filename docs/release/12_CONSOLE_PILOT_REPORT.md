# 12 — Hosted staff console: pilot report (22 Nov 2026)

**This pilot uses synthetic data only.** Booking execution is OFF. WhatsApp goes only to the internal test sink. Production is refused.

## Where it stands

| Item | Value |
|---|---|
| Console URL | `https://w8ptr7w960.execute-api.ap-southeast-2.amazonaws.com/pilot/app/` |
| Code | `eaad598`, the same on `phase-e/aws-implementation-fixes` and `release/fde-sydney-pilot` on GitHub. `main` is untouched. |
| CI | Run 45039036387: 1070 passed, 1 skipped; the Data API-mode suite passed (398 tests). |
| Deploy | Run 45039719968 succeeded. `scripts/verify_aws_deploy.py` passes 29 of 29 checks (`evidence/deploy_check.json`). |
| AWS changes | Stack `SetuHaulPilot` updated. Cognito domain, console client and pre-token trigger added. The console is served by the existing API Gateway. Nothing uses CloudFront or S3, and the extra permissions given to the deploy role were removed. |

## What was verified

**Sign-in.** The `pilot-planner` user signed in with Cognito hosted login. It uses the authorization code flow with PKCE (S256) and a public client with no secret. TOTP MFA is required, and the user set their own password and MFA device.

**The ledger fix (`3eb1d57`).** The usage ledger now runs outside the turn fence.
- A regression test, `test_the_ledger_works_inside_a_fenced_turn`, failed before the fix and passes after it.
- On AWS, a reservation that runs out of budget is now recorded as `BUDGET_EXHAUSTED`, where it used to fail as `meter_error`. Scenario A5 passes.

**The thread views.**
- `GET /console/threads` and `/console/threads/{id}` are read only and need staff sign-in. Results are limited to the user's facilities by row-level security, are paginated, and show masked phone numbers.
- Outside your facilities you get the same 404 as an unknown id.

**Range fix (`eaad598`).** The Data API cannot return `tstzrange` values, so option lists failed on AWS. They now return the range bounds. The emulator also refuses those types, so the whole test suite guards against this.

**End to end in the browser.** A synthetic driver (shipment `SYN-SHP-B4F81FA6`) went through the full conversation:
1. The driver reported a delay.
2. The agent read the arrival time back.
3. The driver confirmed with YES.
4. The agent offered 3 options (Dock D1, D2 and D3 at 2:00 AM on 23 Nov).
5. Both agent replies are recorded as SENT through the internal test sink, each with a `testsink.` reference.

All of this is visible in the console's Conversations view. Scenarios S1, A1–A6 and D1 passed (`evidence/option_a.json`).

## Security results

The live checks are in `evidence/security_live.json`.

| Check | Result |
|---|---|
| No token, on threads, thread detail, requests and escalations | 401, `Cache-Control: no-store` |
| Forged token: `alg=none`, a fake RS256 signature, HS256, or garbage | 401 |
| CORS from `https://evil.example` | No `Access-Control-Allow-Origin` header; the preflight gets 405 |
| Path traversal (`../`, `%2e%2e`), `/app/.env`, `/app/index.html`, `/demo/*` | 404, and no source is exposed |
| Page headers | Strict CSP (`script-src 'self'`, no `unsafe-eval`, `frame-ancestors 'none'`, `base-uri 'none'`), `nosniff`, `no-referrer`, `X-Frame-Options: DENY`, HSTS, COOP |
| Caching | The page is `no-cache`, assets are content-hashed and immutable, and staff data is `no-store` |
| Secrets in the browser | The live `app.js` and `app.css` contain no secret, key, HMAC, `localStorage` or `/demo` path. `config.json` holds only `cognitoDomain`, `clientId` and `apiBase`. No inline scripts. |
| Token with no staff group | Refused, by `test_no_scope_sees_nothing_and_no_staff_group_is_refused` |
| Another facility's thread | 404, by `test_another_facility_cannot_list_or_open_the_conversation`. Scope comes only from admin-assigned groups through the pre-token trigger, so nothing the browser sends can change it (`test_pretoken.py`). |

The last two rows are checked by tests against the same code that is deployed, not live. A live check would need a second Cognito user with its own password and MFA, which only the account owner should create.

## Limits

- **Requests stays empty:** with booking execution OFF, a driver's choice creates no request.
- **Rules fallback:** extraction currently uses the rules fallback. Today's model budget is marked spent, and the real model needs an OpenRouter key and approval for paid calls.
- **CloudFront:** blocked until AWS verifies the account.
- **Pause evidence:** the Aurora ACU metric in CloudWatch stopped publishing. RDS cluster events are the evidence for pause and resume: paused 12:24, 12:54, 13:19, 13:51 and 15:08 UTC, each resume taking about 10–15 s. `pilot_mode.pause_check` should read those events instead (a follow-up).
- **No extraction log:** the conversation view shows the stored facts and ETA, but there is no per-message extraction log.
- **Pilot still awake:** at the time of writing the pilot is AWAKE, because the AWS sign-in expired before `pilot_mode sleep` could run.
