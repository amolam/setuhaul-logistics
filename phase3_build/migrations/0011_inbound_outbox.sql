-- 0011 · B-WF-1 / review findings B2-14 and B2-15: durable inbound delivery (gate for SETUHAUL_BOOKING_ENABLED).
-- Forward-only; 0001–0010 untouched.
--
-- Before: the webhook committed an "enqueued" claim and THEN appended the event to an in-memory queue. A crash between
-- the two lost the driver's message for good (the claim said it was queued; nothing was).
--
-- Now the inbound receipt row is a transactional outbox, with three stages:
--   stored     message + receipt are written in one transaction by the webhook (ingest), as before;
--   published  a relay claims the receipt (enqueued / enqueued_at, as before), sends it to the queue with
--              deduplication id = WhatsApp message id (#generation) and group = driver, THEN sets published_at.
--              A crash before published_at leaves the row for the recovery sweep, which publishes it again;
--   processed  the orchestrator takes a processing lease (processing_owner / processing_until) before the turn and
--              sets processed_at after it. A delivery for a processed message is refused however late it comes, so a
--              duplicate after the queue's 5-minute deduplication window is still rejected. A crashed turn's lease
--              expires and the message is delivered again.
-- generation is bumped when a provider retry recovers a FAILED attestation: that message needs a fresh turn, and the
-- new generation gives it a new deduplication id.
SET search_path = setuhaul, public;

ALTER TABLE inbound_receipts
  ADD COLUMN published_at      timestamptz,
  ADD COLUMN generation        integer NOT NULL DEFAULT 0,
  ADD COLUMN processing_owner  text,
  ADD COLUMN processing_until  timestamptz,
  ADD COLUMN processed_at      timestamptz,
  ADD COLUMN deliveries        integer NOT NULL DEFAULT 0;

-- Receipts written before this migration went through the old in-memory path: treat them as delivered, so the first
-- recovery sweep does not replay history to drivers.
UPDATE inbound_receipts SET published_at = coalesce(enqueued_at, created_at), processed_at = coalesce(enqueued_at, created_at);

CREATE INDEX ix_inbound_receipts_undelivered ON inbound_receipts (created_at) WHERE processed_at IS NULL;

-- The inbound pipeline (relay, worker, recovery) runs as setuhaul_ingest; the agent role gains nothing.
GRANT UPDATE (published_at, generation, processing_owner, processing_until, processed_at, deliveries)
  ON inbound_receipts TO setuhaul_ingest;
