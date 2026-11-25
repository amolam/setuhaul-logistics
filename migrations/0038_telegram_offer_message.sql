-- 0038 · Clear an offer's Telegram buttons after a TYPED yes / no. Forward-only; 0001–0037 untouched.
--
-- A tapped button carries its own message id, so the webhook clears it directly (0036). A typed "haan" / "no" does
-- not: the inbound path needs the Telegram message id of the offer it answered. The relay records it when Telegram
-- accepts the send (notifications.provider_message_id, 'tg.<chat-hash>.<message_id>'). This read-only definer returns
-- it to the verified inbound path for the driver's OWN offer only. It returns nothing for another driver's offer, for
-- a message that has not been accepted by Telegram yet, or for a non-Telegram delivery.
-- Clearing buttons is cosmetic: whatever happens to the buttons, a later tap is refused by the offer's status (0035).

SET search_path = setuhaul, public;

CREATE FUNCTION offer_telegram_message(p_offer uuid, p_driver text) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = setuhaul, pg_catalog, pg_temp AS $$
DECLARE ref text; old_scope text := current_setting('app.facility_scope', true);
BEGIN
  PERFORM set_config('app.facility_scope', all_facility_scope(), true);
  SELECT n.provider_message_id INTO ref
    FROM dispatch_offers o JOIN notifications n ON n.chat_message_id = o.read_back_message_id
   WHERE o.offer_id = p_offer AND o.driver_id = p_driver
     AND n.recipient_ref = 'driver:' || p_driver AND n.provider_message_id LIKE 'tg.%'
   ORDER BY n.updated_at DESC LIMIT 1;
  PERFORM set_config('app.facility_scope', coalesce(old_scope, ''), true);
  RETURN ref;
END $$;

ALTER FUNCTION offer_telegram_message(uuid, text) OWNER TO setuhaul_rpc;
REVOKE EXECUTE ON FUNCTION offer_telegram_message(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION offer_telegram_message(uuid, text) TO setuhaul_ingest;
