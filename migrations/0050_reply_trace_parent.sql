-- 0050 · one LangSmith tree per Telegram message (owner, 27 Nov 2026, OWNER-APPROVAL-20261127-DEMO-04).
-- Forward-only and additive: one nullable column, no default, no backfill, no trigger, no grant change (the agent's
-- table-level INSERT and the system role's SELECT on chat_messages already cover it). An AGENT reply records the
-- trace root of the turn that wrote it ({trace_id, id, dotted_order}); the outbound sender reads it so the Telegram
-- send nests under that agent_turn, which itself nests under the telegram_inbound trace. Observability metadata only:
-- no booking, approval or authorization reads it.
SET search_path = setuhaul, public;

ALTER TABLE chat_messages ADD COLUMN trace_parent jsonb;
