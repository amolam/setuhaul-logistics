"""Independent runtime feature controls for the synthetic pilot (Full Activation release).

Six controls, each read from its own variable. No single flag turns more than one of them on, and every one of them is
resolved per environment:

  BOOKING_ENABLED            SETUHAUL_BOOKING_ENABLED (CDK: only with the owner's booking approval, api + agent only).
                             The switch is necessary, not sufficient: the start-up gates (fresh, release-specific
                             evidence), the database authorization and the kill switch still decide (/health reports
                             the database's answer). Unchanged by this module.
  TELEGRAM_DELIVERY_MODE     SETUHAUL_TELEGRAM_DELIVERY  test_sink (default) | off | test_bot (local/dev) | live
  TELEGRAM_LIVE_AUTHORIZED   SETUHAUL_TELEGRAM_LIVE_AUTHORIZED  true only with the owner's Telegram approval (CDK)
  TELEGRAM_OUTBOUND_ENABLED  SETUHAUL_TELEGRAM_OUTBOUND_ENABLED (default true): false = the Telegram outbound kill
                             switch. Nothing is sent; each due message FAILS visibly with an escalation (never silent).
  TELEGRAM_WEBHOOK_ENABLED   SETUHAUL_TELEGRAM_WEBHOOK_ENABLED (default true) AND a webhook secret configured: false =
                             the inbound route does not exist (404), independently of outbound.
  LLM_JUDGE_ENABLED          SETUHAUL_LLM_JUDGE_ENABLED=true AND an approved positive daily judge budget
                             (SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD); pilot / dev / eval and synthetic data only.
  LANGSMITH_TRACING_ENABLED  SETUHAUL_LANGSMITH_TRACING=on (the tracer still turns itself off on any key problem).

Production (SETUHAUL_ENV production|prod, or any non-synthetic data class) resolves to OFF for booking, live Telegram,
the test bot and the judge, whatever the variables say; the enforcement points (telegram_from_env,
online_judge.config_from_env) refuse those configurations themselves, this module only reports. No secret, token,
chat id or key is ever read or returned here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

PRODUCTION_ENVS = ("production", "prod")
TELEGRAM_LIVE_ENVS = ("pilot",)                 # live Telegram: the synthetic pilot only (this release)
JUDGE_ONLINE_ENVS = ("pilot", "dev", "eval")
TELEGRAM_MODES = ("test_sink", "off", "test_bot", "live")


def _true(v) -> bool:
    return str(v or "").strip().lower() in ("true", "on", "1", "yes")


def _false(v) -> bool:
    return str(v or "").strip().lower() in ("false", "off", "0", "no")


def is_production(env: dict) -> bool:
    return ((env.get("SETUHAUL_ENV") or "").strip().lower() in PRODUCTION_ENVS
            or (env.get("SETUHAUL_DATA_CLASS") or "synthetic").strip().lower() != "synthetic")


def _decimal(v) -> Decimal:
    try:
        d = Decimal(str(v or "0").strip())
        return d if d.is_finite() else Decimal(0)
    except (InvalidOperation, ValueError):
        return Decimal(0)


@dataclass(frozen=True)
class FeatureState:
    environment: str
    production: bool
    booking_enabled: bool
    telegram_delivery_mode: str
    telegram_live_authorized: bool
    telegram_outbound_enabled: bool
    telegram_webhook_enabled: bool
    llm_judge_enabled: bool
    langsmith_tracing_enabled: bool
    notes: tuple = field(default_factory=tuple)   # why a requested control resolved off (names only, never values)

    def public(self) -> dict:
        """Safe for /health: booleans, modes and reasons; never a secret or an identifier."""
        d = asdict(self)
        d["notes"] = list(self.notes)
        return d


def resolve(env: dict) -> FeatureState:
    environment = (env.get("SETUHAUL_ENV") or "dev").strip().lower()
    prod = is_production(env)
    notes: list[str] = []

    booking = _true(env.get("SETUHAUL_BOOKING_ENABLED"))
    if booking and prod:
        notes.append("booking: refused in production by this release")
        booking = False

    mode = (env.get("SETUHAUL_TELEGRAM_DELIVERY") or "test_sink").strip()
    if mode not in TELEGRAM_MODES:
        notes.append("telegram: unknown delivery mode")
        mode = "invalid"
    live_auth = _true(env.get("SETUHAUL_TELEGRAM_LIVE_AUTHORIZED"))
    if mode == "live" and (prod or environment not in TELEGRAM_LIVE_ENVS):
        notes.append(f"telegram: live delivery is allowed only in {TELEGRAM_LIVE_ENVS} (synthetic)")
        mode, live_auth = "refused", False
    if mode == "live" and not live_auth:
        notes.append("telegram: live needs SETUHAUL_TELEGRAM_LIVE_AUTHORIZED=true")
    outbound = not _false(env.get("SETUHAUL_TELEGRAM_OUTBOUND_ENABLED"))
    secret = bool((env.get("SETUHAUL_TELEGRAM_WEBHOOK_SECRET") or env.get("SETUHAUL_TELEGRAM_WEBHOOK_SECRET_ID") or "").strip())
    webhook = secret and not _false(env.get("SETUHAUL_TELEGRAM_WEBHOOK_ENABLED"))

    judge = _true(env.get("SETUHAUL_LLM_JUDGE_ENABLED"))
    if judge:
        budget = _decimal(env.get("SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD"))
        if prod or environment not in JUDGE_ONLINE_ENVS:
            notes.append(f"llm_judge: allowed only in {JUDGE_ONLINE_ENVS} (synthetic)")
            judge = False
        elif budget <= 0:
            notes.append("llm_judge: no approved daily budget (SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD is 0)")
            judge = False

    langsmith = _true(env.get("SETUHAUL_LANGSMITH_TRACING"))
    return FeatureState(environment, prod, booking, mode, live_auth, outbound, webhook, judge, langsmith, tuple(notes))
