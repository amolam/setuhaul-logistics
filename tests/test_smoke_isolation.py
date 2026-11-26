"""Smoke check 7 (isolation): a delivery outside the test sink passes only when it is a Telegram message to a driver on
the owner-approved allowlist of the deployed sender. Everything else, and everything when the Telegram-live
configuration is incomplete, still fails."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from smoke_option_d import classify_deliveries, telegram_live_allowlist  # noqa: E402

LIVE = {"SETUHAUL_TELEGRAM_DELIVERY": "live", "SETUHAUL_TELEGRAM_LIVE_AUTHORIZED": "true",
        "SETUHAUL_TELEGRAM_LIVE_APPROVAL": "OWNER-APPROVAL-20261126-SYNTHETIC-PILOT-TELEGRAM-LIVE-01",
        "SETUHAUL_TELEGRAM_AUTHORIZED_DRIVERS": "SYN-DRV-TEST-1, SYN-DRV-TEST-2"}


def test_approved_telegram_to_an_allowlisted_driver_passes():
    assert classify_deliveries([("tg", "driver:SYN-DRV-TEST-1", 7), ("tg", "driver:SYN-DRV-TEST-2", 1)], LIVE) == (8, 0)


def test_telegram_to_a_driver_off_the_allowlist_fails():
    assert classify_deliveries([("tg", "driver:SYN-DRV-OTHER", 1)], LIVE) == (0, 1)


def test_any_other_provider_fails_even_for_an_allowlisted_driver():
    assert classify_deliveries([("wamid", "driver:SYN-DRV-TEST-1", 2), ("", "driver:SYN-DRV-TEST-1", 1)], LIVE) == (0, 3)


def test_an_incomplete_live_configuration_allows_nothing():
    for k, bad in [("SETUHAUL_TELEGRAM_DELIVERY", "test_sink"), ("SETUHAUL_TELEGRAM_LIVE_AUTHORIZED", "false"),
                   ("SETUHAUL_TELEGRAM_LIVE_APPROVAL", ""), ("SETUHAUL_TELEGRAM_LIVE_APPROVAL", "approved"),
                   ("SETUHAUL_TELEGRAM_AUTHORIZED_DRIVERS", "")]:
        env = {**LIVE, k: bad}
        assert classify_deliveries([("tg", "driver:SYN-DRV-TEST-1", 1)], env) == (0, 1), k
        if k != "SETUHAUL_TELEGRAM_AUTHORIZED_DRIVERS":
            assert telegram_live_allowlist(env) == (None, frozenset())
    assert classify_deliveries([("tg", "driver:SYN-DRV-TEST-1", 1)], {}) == (0, 1)


def test_the_allowlist_names_the_approval_and_only_listed_drivers():
    ref, allowed = telegram_live_allowlist(LIVE)
    assert ref == LIVE["SETUHAUL_TELEGRAM_LIVE_APPROVAL"]
    assert allowed == {"driver:SYN-DRV-TEST-1", "driver:SYN-DRV-TEST-2"}
