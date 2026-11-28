# 11 — Hosted staff console (AWS pilot, synthetic data)

**URL:** stack output `ConsoleUrl`, which is `https://w8ptr7w960.execute-api.ap-southeast-2.amazonaws.com/pilot/app/`.

**Option A controls stay on:** synthetic data only, booking execution OFF, WhatsApp only to the internal test sink, and production refused.

> **State note (28 Nov 2026).** The line above is the Option A state of 22–23 Nov 2026. Since then: booking on the pilot is turned on only under a fresh, release-bound owner approval; Telegram is live for two owner-authorised synthetic test drivers; and a fourth staff role, `dispatcher`, exists (migration 0037). The current state is in the root [`README.md`](../../README.md#live-synthetic-pilot). There is no API Gateway authoriser: the authorization row below is the only token check.
>
> **Staff roles (Cognito groups), as implemented:** `planner`, `operations`, `ops_lead` and `dispatcher`. A `dispatcher` can view dispatch, send a booking offer, link a driver's Telegram account and request a cancellation; a `dispatcher` cannot decide any approval. `operations`, `planner` and `ops_lead` can decide a STANDARD request; the other request kinds need `planner` or `ops_lead` (`approval_role_rules`, migrations 0015, 0016 and 0037; `PERMISSIONS` in `setuhaul/api/service.py`). TOTP MFA is required for all four.

**Why API Gateway and not CloudFront:** the first console deploy (run 45029899923) was refused by CloudFront: *"Your account must be verified before you can add new CloudFront resources."* So the page is served by the existing API, on the same HTTPS origin as its data. That avoids any CORS, bucket or CDN. CloudFront and S3 can be revisited once AWS has verified the account.

## How it works

| Piece | What it is |
|---|---|
| Page | Lives in `setuhaul/api/console_static/` (HTML, JS, CSS; no inline script) inside the verified Lambda zip. `setuhaul/api/console_site.py` serves a fixed allow-list: `/app/`, `/app/config.json`, `/app/assets/app.js`, `/app/assets/app.css`. Nothing else on disk is reachable. Asset URLs are content-hashed and cached as immutable; the HTML and config are `no-cache`; `/console/*` data is `no-store`. |
| Headers | CSP `default-src 'none'; script-src 'self'; connect-src 'self' <cognito>; frame-ancestors 'none'; base-uri 'none'`, plus `nosniff`, `no-referrer`, `X-Frame-Options: DENY` and HSTS. HTTPS comes from API Gateway. Styles allow inline attributes; scripts never do, and there is no eval. |
| Sign-in | Cognito hosted login (prefix domain `setuhaul-pilot-console`) with authorization code and PKCE (S256), on a public client with no secret. Callback and logout are exactly the console URL. MFA (TOTP) is required by the pool, and tokens last 30 minutes. The access token is kept in memory only; reloading the page means signing in again, which is quick while the Cognito session lasts. |
| Authorization | Unchanged and per route in the API: Cognito JWKS signature, issuer, `token_use=access`, `client_id` allow-list, expiry, and a staff group (`planner`, `operations`, `ops_lead` or `dispatcher`), then the action's own permission. |
| Facility scope | The `facility_scope` claim comes from the Cognito pre-token trigger (`V2_0`, Essentials tier, `setuhaul/aws/pretoken.py`), only from admin-assigned groups `facility-FAC-JAI-01` and `facility-FAC-GGN-01`. Row-level security in the database enforces it. The browser cannot set it. |
| Read views | `GET /console/threads` and `/console/threads/{id}` are read only. They return masked phone numbers and messages and never payloads or recipients, at most 50 a page and 200 messages. Outside your facilities you get the same 404 as an unknown id. |
| Checks | CI runs `python -m setuhaul.ops.console_config --check setuhaul/api/console_static`, which refuses secret-looking content and unexpected file types. `scripts/verify_aws_deploy.py` checks the live client, domain, trigger and page headers. |

## Staff user (you run these; no password or MFA code passes through anyone else)

```powershell
$pool = aws cloudformation describe-stacks --region ap-southeast-2 --stack-name SetuHaulPilot --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text
aws cognito-idp admin-create-user --region ap-southeast-2 --user-pool-id $pool --username pilot-planner --user-attributes Name=email,Value=YOUR_EMAIL Name=email_verified,Value=true --desired-delivery-mediums EMAIL
aws cognito-idp admin-add-user-to-group --region ap-southeast-2 --user-pool-id $pool --username pilot-planner --group-name planner
aws cognito-idp admin-add-user-to-group --region ap-southeast-2 --user-pool-id $pool --username pilot-planner --group-name facility-FAC-JAI-01
aws cognito-idp admin-add-user-to-group --region ap-southeast-2 --user-pool-id $pool --username pilot-planner --group-name facility-FAC-GGN-01
```

Cognito emails a temporary password. At the first sign-in on the console you:
1. set your own password;
2. scan the QR code with an authenticator app (TOTP);
3. enter the 6-digit code.

## A synthetic driver message (trusted local machine; the browser never signs anything)

```powershell
python scripts/send_driver_message.py "stuck in traffic, approximately 2 hours late"
python scripts/send_driver_message.py --phone +999XXXXXXXXXX "YES"
```

The first command creates a SYN- driver and shipment. The second continues that driver's conversation. Then open **Conversations** and select the driver (shown masked) to see:
- the messages;
- what the reader recorded (facts and ETA);
- the options shown;
- the notifications, with "internal test sink" only when the stored record carries a test-sink reference.

## Limits

- **No requests to decide:** with booking execution OFF, a driver's choice creates no request, so **Requests** stays empty in Option A.
- **One extraction field:** the conversation view shows the facts the reader carried and the ETA it recorded. There is no per-message extraction log; the extracted text itself is deliberately not stored.
- **Default domains:** the URL is the API Gateway default domain, and sign-in uses the Cognito prefix domain (no custom domain).
