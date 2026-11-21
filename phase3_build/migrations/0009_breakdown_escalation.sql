-- 0009 · Owner decision OD-2 (breakdowns, 18 Nov 2026). Forward-only; 0001–0008 untouched.
--
-- OD-2 replaces Phase 1's blanket rule ("breakdown not fixed → safety"):
--   immediate danger, accident, injury, fire or threat   → SAFETY, mode TAKE_OVER (the agent stops until hand-back)
--   unrepaired breakdown, no identified danger            → BREAKDOWN, mode DECIDE (durable escalation; the agent may
--                                                            continue within its normal boundaries)
--   unrepaired breakdown, safety status unknown           → BREAKDOWN escalation + one safety question to the driver
--   repaired breakdown                                    → normal ETA update and feasibility
-- So escalations need their own reason, and the per-conversation memory (0008) must remember that the safety question
-- was asked: a plain "no" to "are you in a safe place?" contains no danger words, yet must become a takeover.
SET search_path = setuhaul, public;

DO $$
DECLARE c text;
BEGIN
  SELECT conname INTO c FROM pg_constraint
   WHERE conrelid = 'setuhaul.escalations'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%reason_code%';
  EXECUTE format('ALTER TABLE setuhaul.escalations DROP CONSTRAINT %I', c);
  SELECT conname INTO c FROM pg_constraint
   WHERE conrelid = 'setuhaul.thread_facts'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%asked%';
  EXECUTE format('ALTER TABLE setuhaul.thread_facts DROP CONSTRAINT %I', c);
END $$;

ALTER TABLE escalations ADD CONSTRAINT escalations_reason_code_check CHECK (reason_code IN (
  'NO_FEASIBLE_SLOT','SAFETY','CONTRADICTORY_DATA','AFTER_HOURS_APPROVAL','CRITICAL_CONFLICT','CLARIFICATION_LIMIT',
  'PENDING_EXPIRED','DRIVER_REQUEST','OUT_OF_SCOPE','AGENT_FAILURE','NOTIFICATION_FAILED','BREAKDOWN'));

ALTER TABLE thread_facts ADD CONSTRAINT thread_facts_asked_check CHECK (asked IN (
  'declared_eta_local','delay_kind','latest_finish_kind','shipment_id','safety_status'));

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA setuhaul FROM PUBLIC;
