# 11 — Hosted staff console (AWS pilot, synthetic data)

**URL:** the stack output `ConsoleUrl` (`https://<id>.cloudfront.net/`).

**Option A controls stay on:** synthetic data only, booking execution OFF, WhatsApp only to the internal test sink, and production refused.

## How it works

| Piece | What it is |
|---|---|
| Page | `console/` (HTML, JS, CSS; no inline script). It sits in a private S3 bucket that is served only by CloudFront through origin access control, over HTTPS only. A strict CSP is set (`script-src 'self'`, `frame-ancestors 'none'`). |
| Sign-in | Cognito hosted page with authorization code and PKCE (S256), on a public client with no secret. Callback and logout go to the console URL only. MFA (TOTP) is required by the pool, and tokens last 30 minutes. The access token is kept in memory only. |
| Authorization | The API checks every `/console` call itself: Cognito signature, issuer, `token_use=access`, `client_id`, and a staff group (`planner`, `operations` or `ops_lead`). Each action then checks its own permission. |
| Facility scope | The `facility_scope` claim is added by the Cognito pre-token trigger (`V2_0`, Essentials tier, `setuhaul/aws/pretoken.py`). It comes only from admin-assigned groups `facility-FAC-JAI-01` and `facility-FAC-GGN-01`. Nothing the browser sends can change it. Row-level security in the database enforces it. |
| CORS | The API allows exactly the console origin: GET and POST, the headers `authorization` and `content-type`, and no credentials mode. CORS is not authorization; a request with no token gets 401 whatever its origin. |
| Read views | `GET /console/threads` and `/console/threads/{id}` are read only. They return masked phone numbers and messages and never payloads or recipients. A thread outside your facilities is answered with the same 404 as an unknown thread. |
| Publishing | The deploy job builds `config.json` from the stack outputs (API URL, Cognito domain, client id, Region; nothing else). It refuses any secret-looking content (`setuhaul.ops.console_config --check`), then runs `s3 sync` and a CloudFront invalidation. |

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

The first command creates a SYN- driver and shipment. The second continues that driver's conversation. Then open **Conversations** in the console and select the driver (shown masked) to see:
- the messages;
- what the reader recorded (facts and ETA);
- the options shown;
- the notifications, with "internal test sink" only when the stored record carries a test-sink reference.

## Limits

- **No requests to decide:** with booking execution OFF, a driver's choice creates no request, so **Requests** stays empty in Option A.
- **One extraction field:** the conversation view shows the facts the reader carried and the ETA it recorded. There is no per-message extraction log; the extracted text itself is deliberately not stored.
- **Default domains:** CloudFront and Cognito use their default domains (no custom domain or certificate).
