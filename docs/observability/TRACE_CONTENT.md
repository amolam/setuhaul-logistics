# LangSmith trace content: synthetic pilot (27 Nov 2026, fix plan issue 8)

Decision register **T57**; owner decision **Q3** (27 Nov 2026). Code: `setuhaul/observability/redaction.py`,
`setuhaul/observability/langsmith.py` (`content_mode_from_env`), `setuhaul/agent/orchestrator.py` (spans),
`infra/setuhaul_stack.py` (`trace_content`). Proof: `tests/test_trace_content_full.py`.

## The setting

| `SETUHAUL_TRACE_CONTENT` | Where it is honoured | What leaves the process |
|---|---|---|
| `off` (default everywhere except the pilot) | everywhere | ids, states, counts, timings; every free-text value is `[REDACTED]` |
| `on` | any synthetic environment with tracing on | as `off`, plus scrubbed message text (300 characters per value) |
| `full` (pilot default from this release) | **only** `SETUHAUL_ENV=pilot` **and** `SETUHAUL_DATA_CLASS=synthetic` | as `on`, plus the model's per-turn input and raw output, full tool results, long text (4,000 characters per value) |

`full` anywhere else falls back to `off` at run time (one log line), and the CDK stack refuses `trace_content=full`
outside the pilot at synth time. The pilot can be lowered without a code change: deploy with `-c trace_content=on`
or `-c trace_content=off`. The stack output `TraceContent` shows what is deployed.

## What `full` exports (after the scrub)

- the driver's message and conversation context (the masked bodies the agent reads);
- the model's per-turn input (`model_input`: the user block) and its raw output (`raw_output`);
- the prompt and schema versions (`prompt_version`, `schema_version`). The fixed system instructions are versioned
  code and are **never** exported, by version only;
- every tool call's arguments and its full result;
- the parameter check (missing fields, problems, the requested date), the decision path, the reply;
- correlation: the turn's `correlation_id` / `turn_ref`, the inbound request's id, driver, shipment and facility ids.

## What is always removed, in every mode

- **Credentials and secrets**: any key named like a secret, password, token, key or credential (`api_key`,
  `authorization`, `session_token`, `webhook_secret`, ...), the system prompt, and anything that looks like a JWT, a
  bearer token, an OpenRouter / LangSmith / OpenAI-style key, an AWS access key or secret, a Meta token, or a 12-digit
  AWS account id (also inside an ARN).
- **Driver identifiers**: keys for phone numbers, names, e-mails, addresses, chat ids, Telegram user ids, usernames,
  first and last names, coordinates, Cognito subjects, IP addresses; phone numbers and e-mail addresses inside any text;
  and the turn's own driver name, phone number and Telegram ids, registered by the inbound path and the agent and
  masked as `[PII]` wherever they appear in free text.
- The export guard drops a whole turn that still matches any of these patterns or registered identifiers after
  redaction (counted as `blocked_leaks`); nothing is exported half-redacted.

Operational data that is not driver PII stays readable: facility and dock codes, times and dates, order, shipment,
appointment and offer ids, statuses, rule ids and outcomes, reason codes and staff notes.

## Access controls

- The LangSmith API key lives in Secrets Manager (`setuhaul/pilot/langsmith`, KMS-encrypted). Only the three tracing
  functions (agent, api, outbound) can read it (`tests/test_infra.py`); the owner sets its value. No application
  role, console user or driver can read it.
- Traces go only to the pilot's own project, `setuhaul-synthetic-pilot`; a pilot cannot write to a dev project
  (`project_from_env`). Endpoints are restricted to LangSmith Cloud US / EU / APAC.
- Who can read traces: the members of the owner's LangSmith workspace. **Owner action:** keep that workspace limited to
  the people who operate the pilot, and do not create public share links for pilot traces.
- The staff console shows links only (`SETUHAUL_LANGSMITH_UI_URL`); it never proxies trace content.

## Retention

- LangSmith keeps traces for the retention period of the project's plan and setting (LangSmith Cloud offers a
  short "base" and a long "extended" retention). **Owner action:** set the `setuhaul-synthetic-pilot` project to the
  shortest retention that covers the pilot's debugging needs, and delete the project when the pilot ends. The data is
  synthetic, so no personal data is held there by design; the redaction above is a second line of defence.
- Nothing is written to disk by the exporter; a run that cannot be posted is dropped, never retried more than once.

## Switching it off

`cdk deploy -c trace_content=off` (or `on`), through the usual release process. In an emergency the owner can remove
the LangSmith key's value; tracing then reads as `EMPTY_SECRET` and turns itself off, with no effect on the agent.
