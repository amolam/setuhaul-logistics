-- 0042 · The offline LLM judge records its paid calls in the SAME usage ledger as the agent. Forward-only, additive.
--
-- model_usage.purpose admits 'judge' besides 'extract' and 'phrase'. Nothing else changes:
--   * reserve_model_call / settle_model_call (0023) are untouched: a judge call is reserved before it is sent, under the
--     same row lock on model_budget_day (one row per UTC day for the whole database), against the same daily cap, and
--     settled at the provider-reported cost (or, when the cost is unknown, at the full reservation; 0 only when the
--     request provably never left the process). The agent's and the judge's spend on one database therefore share
--     one daily total: no second budget mechanism exists.
--   * no new grant, no new role, no new function. Only setuhaul_agent can reserve and settle (0023 grants).
--
-- The judge runs offline (evals/langsmith/llm_judge.py, dev/eval only, synthetic data), never in the pilot path.
-- Recovery: a later migration could restore the two-value CHECK once no 'judge' row remains (rows are never deleted,
-- so in practice the wider CHECK stays).

SET search_path = setuhaul, public;

ALTER TABLE model_usage DROP CONSTRAINT model_usage_purpose_check;
ALTER TABLE model_usage ADD CONSTRAINT model_usage_purpose_check CHECK (purpose IN ('extract', 'phrase', 'judge'));
