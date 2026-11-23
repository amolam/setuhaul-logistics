# Phase G.1 security and privacy review

Scope: every change on `feature/phase-g1-langsmith-model-reliability` against `main` (`951a320`).

## 1. Secrets

| Item | Finding |
|---|---|
| Values read | **None.** Secrets Manager was queried for metadata only (`DescribeSecret`, `ListSecretVersionIds`). Database evidence went through the RDS Data API with the secret **ARN** (the value never reached the workstation). |
| Secrets changed or rotated | None. The CDK `OpenRouterKey` resource is byte-for-byte unchanged and pinned by a test (changing `GenerateSecretString` would replace the value). |
| New secret | `setuhaul/<env>/langsmith`, **only** with `-c langsmith_tracing=on`, created with an explicit non-secret placeholder `{"api_key": ""}` and KMS-encrypted with the stack key, readable by the agent role only (tested). |
| Keys in errors/logs | Secret errors no longer contain the secret ARN (C5: it carried the account id). Keys only ever go into a header. A key's shape (`openrouter_format`/`unrecognized_format`/`langsmith_format`/`empty`) is logged, never its value, length or more than the public prefix. Tests: `test_the_key_never_reaches_logs_or_errors`, `test_secret_problems_…never_reveal…`, `test_the_langsmith_client_sends_the_key_only_as_a_header…`. |
| Environment keys | `OPENROUTER_API_KEY` and `LANGSMITH_API_KEY` are refused in staging/pilot. |
| Repository scan | Changed and new files scanned for `sk-or-v1-…`, `lsv2_…`, `AKIA…`, the owner's e-mail and 12-digit ids: only the fake test values in `tests/test_phase_g1.py` (synthetic `+91…` numbers and `123456789012`). |

## 2. PII and content

* Traces: central redaction + export guard (LANGSMITH_OBSERVABILITY.md §4). Driver text is off by default, and when on it
  is scrubbed. A full turn through the real orchestrator is checked for the driver's phone and words
  (`test_a_turn_is_traced_with_hierarchy_correlation_and_no_personal_data`).
* CloudWatch: `agent.tool` / `agent.turn` events go through `telemetry.clean` (drops PII keys); ids only.
* Console: new fields are counts, categories, ids, times and links; facility scope by RLS; no phone or key (tested).
* Ledger function 0028: aggregates only; the ledger never held prompts, text or keys.

## 3. IAM and network

* Default deploy: **no IAM change** (template identical to `main` apart from the code asset, proven by synthesizing both).
* Opt-in: `secretsmanager:GetSecretValue` (+ KMS decrypt via the key policy grant) on the LangSmith secret, for the
  agent role only. No wildcard, no new service principal.
* Network: none changed. The agent Lambda is outside the VPC (as for OpenRouter); the export endpoint is allowlisted to
  the two LangSmith API hosts. The UI links are allowlisted to the two LangSmith UI hosts, server- and browser-side.

## 4. Authorisation and isolation

* No new route and no new permission. G6 lives inside `view_observability` (planner, ops_lead); `operations` → 403 (tested).
* Recent turns: `agent_turn_metrics` RLS (`facility_id IS NULL OR scope_allows`); a JAI planner sees JAI and CENTRAL only (tested).
* The 0028 function: `SECURITY DEFINER`, `STABLE`, owner `setuhaul_rpc` (NOBYPASSRLS), pinned `search_path`, EXECUTE
  revoked from PUBLIC, granted to console and system only (tested). No table, column, booking, approval, capacity or
  kill-switch change.
* Booking boundaries untouched: no change to the booking flow gate, the database booking functions, `check_booking_preconditions`,
  the kill switch, the approval workflow or Option D activation.

## 5. Failure isolation

LangSmith down, slow or buggy: the turn's reply, tool calls and model calls are unchanged (tested). Tracing never
raises into the agent. OpenRouter down or misconfigured: the rules reader answers, the category is recorded, no other
paid model is tried, and the booking flow is unchanged (tested for every category class).

## 6. Evaluation

The runner stays local-only (fresh local PostgreSQL; refuses `SETUHAUL_DSN=rds-data` and `SETUHAUL_ENV=pilot/staging`).
LangSmith sync runs from dev/eval/ci only, into separate projects. The LLM judge is off and can never decide
authorisation or constraints.

## 7. Residual risks / owner decisions

* LangSmith Cloud is a third-party processor. Enable it only for synthetic data and with an owner decision on the plan.
* `SETUHAUL_TRACE_CONTENT=on` exports scrubbed message text. Keep it off unless needed for a specific synthetic investigation.
* The `/health` endpoint is public (pre-existing) and reports booking and kill-switch state; the console relies on it.
  It exposes no secret or data. Unchanged in G.1.
