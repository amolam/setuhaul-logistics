# Phase D: security scan (21 Nov 2026)

Evidence labels: **verified** = run here and the output read; **developer-reported** = stated in earlier work, not
re-run; **assumed**; **not tested**; **blocked** = needs access this workstation does not have.

## 1. Secrets and personal data

| Check | Method | Result | Label |
|---|---|---|---|
| Tracked files (182) | Regex scan for AWS access keys, AWS secret assignments, private-key blocks, OpenRouter / Anthropic / OpenAI-style / GitHub / Slack tokens, JWTs, password assignments, DSNs with passwords, AWS account ids near `arn:`/`account`, e-mail addresses. Text inside `.pptx`/`.zip` parts included | No secrets. 3 findings, all synthetic: account id `000000000000` in `tests/test_inbound_delivery.py` (placeholder ARN); `*.example` addresses in `data/setuhaul_schema_and_seed.sql` and `docs/1-frame/audit_results.md` (from the challenge data package) | verified |
| Git history | The same scan over every blob reachable from every ref (`git rev-list --all --objects`: 304 blobs, all commits) | Same 3 synthetic findings; nothing that was committed and later deleted | verified |
| `.env`, `.env.*`, keys | `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), `*.key`, `*.pem`, `secrets/`. No such file is tracked. `.env.example` holds placeholders only | Clean | verified |
| OpenRouter key | Lives in `%USERPROFILE%\.setuhaul\openrouter.key`, outside the repository. Staging/pilot read it from Secrets Manager only; an environment-variable key is refused there | Not in the repository or its history | verified |
| Key in logs and errors | `tests/test_providers.py::test_the_key_never_reaches_logs_or_errors`, `test_a_secret_that_cannot_be_read_fails_without_revealing_anything` | Pass in the suite | verified |
| `/health` | Returns status, database reachability, booking switch, environment name and version only; tests assert no DSN fragment appears | Pass | verified |
| Deck | `presentation/SetuHaul_readout.pptx` XML parts scanned; author field "SetuHaul FDE team" | Clean | verified |
| Commit author | All commits carry the owner's personal e-mail as author | Visible to anyone with access to the private repository. **Owner decision (22 Nov 2026): keep it**; no history rewrite | verified |
| Synthetic data only | Seed, golden sets and eval caches are from the challenge data package or generated; phone numbers are `+9198…` test ranges | No real driver or customer data found | verified (by inspection of the data sources), assumed for free-text eval inputs |

The scanner is a regex scan, not a commercial tool (gitleaks/trufflehog are not installed here). Recommended
before the push: run GitHub secret scanning and push protection on the private repository (free for private repos
on GitHub Team/Enterprise; on a personal plan use `gitleaks detect` locally).

## 2. Dependencies

`pip-audit 2.10.1` against PyPI/OSV, all three requirement files.

| | Before | After |
|---|---|---|
| Vulnerable packages | `starlette 0.48.0`, `PyJWT 2.10.1`, `cryptography 45.0.7` (47 advisories), `pytest 8.4.2` (dev) | none: "No known vulnerabilities found" for `requirements.txt`, `requirements-aws.txt`, `requirements-dev.txt` |
| Pins changed | — | `fastapi 0.118.0 → 0.165.0` (needed for Starlette 1.x), `starlette 0.48.0 → 1.10.0`, `PyJWT 2.10.1 → 2.15.1`, `cryptography 45.0.7 → 50.0.2`, `pytest 8.4.2 → 9.2.0`; added `httpx2==2.16.2` (dev only: Starlette 1.x's test client uses it; the application keeps `httpx`) |

The full suite passes on the upgraded set (see `01_TEST_REPORT.md`). Label: verified.

## 3. IAM and credentials

| Check | Result | Label |
|---|---|---|
| Database credentials hard-coded | None. The DSN comes from `SETUHAUL_DSN`; tests use the local cluster DSN | verified |
| Application roles | Five database roles (agent, ingest, feasibility, console, system), RLS and an allowlist of functions per role; the agent is not a database operator (`tests/test_agent_not_operator.py`, `tests/test_db_requests.py`) | verified locally |
| AWS IAM least privilege | There is no IAM policy or infrastructure code in the repository yet, so nothing can be checked. The required policies are listed in `04_SYDNEY_PILOT_DEPLOYMENT_PLAN.md` §4 and are proven by the WORKLOAD_IDENTITY gate | not tested |
| Shared credential | Locally `Db.tx()` uses one DSN and `SET LOCAL ROLE`; production must map each workload to one role (DEPLOYMENT_GATES.md, workload identity acceptance) | open: part of Phase E build |
