# Phase G.2: LangSmith evaluation smoke (10 synthetic cases)

Status: implemented and tested locally. **The live LangSmith run has not been done**, because no LangSmith key was
available in this workspace. The offline run of the same 10 examples passes 10/10 at $0
(`evals/results/g2_smoke_20261125_011656.json`). This phase proves the integration works. It is not a rollout.

## 1. The workflow

```
agent execution ──► LangSmith trace ──► dataset ──► deterministic evaluators ──► experiment
(Phase G executors,   (existing G.1 spans,  setuhaul-g2-smoke-10   g2_evaluators.py      one run per example,
 local DB, scripted    redacted, project    (create or reuse)      5 evaluators          linked to its example,
 model, $0)            setuhaul-synthetic-                                               feedback per evaluator,
                       pilot)                                                            agent trace ids in outputs)
```

Nothing new runs on the agent path. The script installs an exporting tracer only in its own process, built from the
G.1 `Tracer`, `LangSmithExporter` and `LangSmithClient`. The orchestrator's existing spans become the trace:
`agent_turn`, `intent_extraction`, `model_call`, `parameter_check`, `decide`, the tool spans, `optimizer` and
`final_response`. The Lambda's own switch (`SETUHAUL_LANGSMITH_TRACING`) is unchanged and stays off.

## 2. The 10 scenarios (existing rows of `evals/phase_g/dataset.jsonl`, original IDs)

| ID | Category | Why it was picked |
|---|---|---|
| C01 | Normal booking | A delay with a time: the optimizer ranks feasible options and nothing is held yet |
| C03 | Normal booking | The driver's earliest start reaches the tool, and every option honours it |
| C04 | Normal booking | The full flow (options, pick, explicit yes) ends in a PENDING request for a human |
| C06 | Policy and authorization | The model claims a yes the driver never gave: no hold and no request |
| T04 | Policy and authorization | A driver session that names another driver's shipment is refused (403) |
| A10 | Human approval boundary | The operations role cannot apply a schedule move; only a planner can approve it |
| T06 | Human approval boundary | Deciding a booking request is not an agent tool (404): only a human approves |
| T05 | Duplicate/idempotency | The same report on the same turn gets the same answer and stores one ETA signal |
| C08 | Failure/refusal handling | The model is down: the rules reader answers, options are still offered, no booking |
| C12 | Driver communication | A status question gets "no active booking" and an offer, never a booking |

Every category had a suitable existing example, so there are no gaps.

Each example in the dataset carries:

- `inputs`: the original scenario input.
- `outputs.expected_outcome`: the original expectation, without the `authorization`, `label_note` and
  `response_traits` fields.
- `outputs.expected_policy_decision`: `ALLOWED` or `DENIED`.
- `metadata`: `scenario_id`, `category`, title, the reason it was picked, `synthetic: true`, the evaluators that
  apply, and a `content_sha` used to detect drift.

## 3. Evaluators (deterministic, `g2-eval-1`)

| Evaluator | Judges (from the executor's checks on stored state and HTTP answers) | Applied to |
|---|---|---|
| outcome_correctness | tools called, decision path, reply content, optimizer use, parameters, fallback; HTTP/status for direct calls | C01 C03 C04 T05 C08 C12 |
| policy_compliance | forbidden tools not called, no phone number in a reply, a DENIED call refused | all except T05 |
| approval_boundary | the approver-only action was refused with the expected status (reuses G.1 `approval_required`) | A10 T06 |
| booking_state | appointments and holds match the expected state (NONE, or PENDING for a human) | C01 C03 C04 C06 T04 (n/a) C08 C12 |
| idempotency | the repeated call answered the same and had one effect (reuses G.1 `idempotency`) | T05 |

The evaluators are pure functions and import nothing from `setuhaul`. A test pins this. They cannot authorise,
refuse or retry a booking. There is no LLM judge.

## 4. Setup (owner, about 5 minutes)

1. In LangSmith, create a personal or service API key in **Settings → API Keys**. Use a workspace that is meant for
   synthetic data.
2. In a PowerShell window at the repository root, type the key yourself. Do not paste it into a file or a chat.

   ```powershell
   $env:LANGSMITH_API_KEY = Read-Host "LangSmith key"
   ```

   ```powershell
   $env:LANGSMITH_TRACING = "true"
   ```

   ```powershell
   $env:LANGSMITH_PROJECT = "setuhaul-synthetic-pilot"
   ```

   Set `LANGSMITH_ENDPOINT` only for a non-US workspace (EU or APAC/Sydney):

   ```powershell
   $env:LANGSMITH_ENDPOINT = "https://apac.api.smith.langchain.com"
   ```

   If the key is organisation-scoped (a service key, or an organisation with several workspaces), name the workspace.
   Copy the workspace ID from **Settings → Workspaces**:

   ```powershell
   $env:LANGSMITH_WORKSPACE_ID = "<workspace-id>"
   ```

   If LangSmith answers HTTP 401 or 403, run the read-only probe. It prints LangSmith's own error text and never
   prints the key:

   ```powershell
   python -m evals.langsmith.g2_probe
   ```

3. Make sure the local test PostgreSQL is running on port 55432. The runner refuses any other host and the RDS Data
   API.
4. Verify the configuration. This makes no network call. Exit code 0 means ready; the key is shown only by its shape.

   ```powershell
   python -m evals.langsmith.g2_smoke --check-config
   ```

5. Run the smoke. It takes about 30 seconds and makes about 20 LangSmith traces.

   ```powershell
   python -m evals.langsmith.g2_smoke
   ```

   It prints `dataset_url`, `experiment_url`, `trace_url` and a summary for each example, and writes
   `evals/results/g2_smoke_<stamp>.json`.

   Exit codes:
   - `0`: all 10 pass.
   - `1`: an example failed.
   - `2`: not configured.
   - `3`: LangSmith was unavailable. The examples were still evaluated locally.

6. Close the window, or run `Remove-Item Env:LANGSMITH_API_KEY`.

Running it again reuses the dataset: no new dataset and no new examples. Each run adds one new experiment, which is
how LangSmith compares runs.

An offline run makes no network call and runs on the local database only:

```powershell
python -m evals.langsmith.g2_smoke --offline
```

## 5. Guarantees

- **Off by default.** No key is committed or hardcoded, and nothing reads the configuration at application start-up.
  A test checks that no `setuhaul/` module references the smoke.
- **Synthetic data only.** The dataset is built from existing synthetic rows and refused if anything matches a
  redaction pattern.
- **Agent traces are redacted.** They pass the G.1 redaction and export guard, and driver text is off: phones, names,
  message bodies, tokens and keys are dropped or scrubbed. The scripted driver text is uploaded only as the dataset's
  own synthetic inputs.
- **Fail-open.** A LangSmith outage changes no agent answer. Export errors are counted, the examples are evaluated
  locally, and no trace is reported unless LangSmith accepted it. A test pins this.
- **No paid model call.** The model is the scripted `FakeModel`, so the estimated maximum model cost is $0.00. The
  script cannot select a paid provider.
- **Safe environments only.** The script refuses `SETUHAUL_ENV=pilot` or `staging`, the RDS Data API, and endpoints
  other than the LangSmith Cloud US, EU and APAC endpoints.

## 6. Notes and gaps

- **Project name.** As the brief asks, agent traces go to `setuhaul-synthetic-pilot`, tagged `environment=eval` and
  `agent_version=g2-smoke`. The G.1 application guard (`project_from_env`) still stops a dev/eval *application*
  from writing to the pilot project. Only this standalone script writes there. To keep evaluation traces apart,
  set `LANGSMITH_PROJECT=setuhaul-dev`.
- **Trace linkage.** Experiment runs list their agent trace IDs in `outputs.agent_trace_ids`; the agent traces are not
  nested under the experiment run. Nesting them would mean changing `Tracer.turn`, which this phase leaves alone.
- **Direct calls have no agent trace.** T04, T05, T06 and A10 are direct tool or console calls that do not go through
  the orchestrator, so the experiment run is their only LangSmith record.
- **Live run pending.** The dataset URL, experiment URL and trace URL exist only after the owner runs step 5. In tests,
  the whole path is exercised against an in-memory LangSmith (`tests/test_phase_g2.py`).

## 7. Cost

- **Model:** $0. The model is scripted.
- **LangSmith:** about 20 traces per run: 10 experiment runs and about 10 agent turns, each with 5 to 10 child runs.
  At one run a day, that is about 600 traces a month, well inside the free Developer allowance (5k base traces a
  month at the time of writing). On a paid plan, base traces cost about $0.50 per 1k beyond the allowance, so less
  than $1 a month. Check current LangSmith pricing before relying on these figures.
- **Local compute:** about 30 seconds a run.
