-- 0040 · A dispatcher offer that is no longer open can never be booked through the conversation. Forward-only.
--
-- Found in the real Telegram test-bot run: the driver typed "no" to a dispatcher offer. The offer became DECLINED,
-- but the conversation still had the offer's option SELECTED and the option was still inside its window. The
-- driver's next message got "Please reply YES to confirm", and a "yes" then held that DECLINED slot and filed a
-- booking request (as STANDARD, not PLANNER_REVIEW, since no ACCEPTED offer stood behind it).
--
-- Rule: when a dispatcher offer leaves OFFERED for anything other than ACCEPTED (DECLINED, ALTERNATIVE_REQUESTED,
-- EXPIRED, SUPERSEDED, UNAVAILABLE), its option expires at once and, if the conversation still has that option
-- selected, the selection is cleared (state NO_OPTIONS). A later "yes" then has nothing to confirm, and hold_option
-- refuses the expired option (existing checks). ACCEPTED is untouched: that path holds and submits as before.
-- A newer offer that supersedes this one selects its own option afterwards in the same transaction (0035 order).

SET search_path = setuhaul, public;

CREATE FUNCTION trg_dispatch_offer_closed() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE old_scope text := current_setting('app.facility_scope', true);
        old_driver text := current_setting('app.driver_id', true);
BEGIN
  IF OLD.status = 'OFFERED' AND NEW.status NOT IN ('OFFERED', 'ACCEPTED') THEN
    PERFORM set_config('app.facility_scope', all_facility_scope(), true);
    PERFORM set_config('app.driver_id', '', true);
    UPDATE shown_options SET expires_at = least(expires_at, clock_timestamp()) WHERE option_id = NEW.option_id;
    UPDATE thread_flow SET state = 'NO_OPTIONS', selected_option = NULL, yes_message_id = NULL, hold_option = NULL,
                           attestation_id = NULL, version = version + 1, updated_at = now()
     WHERE thread_id = NEW.thread_id AND selected_option = NEW.option_id
       AND state IN ('OPTIONS_SHOWN', 'SELECTED', 'CONFIRMED_BY_DRIVER');
    PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
    PERFORM set_config('app.driver_id', coalesce(old_driver, ''), true);
  END IF;
  RETURN NULL;
END $$;
ALTER FUNCTION trg_dispatch_offer_closed() OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION trg_dispatch_offer_closed() FROM PUBLIC;

CREATE TRIGGER dispatch_offer_closed AFTER UPDATE OF status ON dispatch_offers
  FOR EACH ROW EXECUTE FUNCTION trg_dispatch_offer_closed();
