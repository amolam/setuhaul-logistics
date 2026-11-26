"""Online LLM judge for the SYNTHETIC PILOT: advisory quality scores for agent replies (Full Activation release).

It reuses the accepted offline judge unchanged (judge_core: prompt judge-p2, forced tool record_judgement, the strict
validation, the three dimensions clarity / tone / language_match; LLM_JUDGE.md sections 10-11). Scores are ADVISORY and
human calibration is OUTSTANDING: every stored row says so (0043 CHECK advisory, calibration_status).

Where it runs: in the agent worker (aws/lambdas.agent_handler) AFTER a turn's outcome is final and its reply is
committed, so the driver's reply, the booking and every other workflow have already happened and nothing waits on it.
No new AWS resource. It can never decide anything:
  * it has no tool, no booking service, no session and no write path except record_turn_judgement (0043, advisory
    rows only) and the usage ledger; nothing in the application reads judgements to approve, book, authorize or
    release anything;
  * every failure (disabled, sampled out, budget, timeout, provider error, malformed answer, database error) is
    caught here, logged with a category and, where a paid call was considered, recorded as SKIPPED / ERROR /
    UNPARSEABLE. It never raises into the worker.

Eligible turns: an agent turn with a turn_ref, a non-empty reply of at most 2000 characters and the driver's message,
in the synthetic data class, selected by the sampling rate. Sampling is deterministic on turn_ref, so an SQS
redelivery makes the same decision; the ledger call id is derived from turn_ref, so a redelivered turn can never be
paid for twice (reserve_model_call answers DUPLICATE), and the table keeps one row per turn.

Cost controls (all must hold before anything is sent):
  * SETUHAUL_LLM_JUDGE_ENABLED=true and SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD > 0 (default 0: nothing is sent until the
    owner approves a budget), at most ONLINE_DAILY_MAX_USD;
  * per judgement (per run): SETUHAUL_LLM_JUDGE_RUN_CAP_USD (default 0.01, at most 0.05), against the reservation;
  * the judge's own daily sub-cap: judge_spent_today() under a transaction advisory lock, in the same transaction as
    reserve_model_call (0023), which enforces the shared daily cap minus SETUHAUL_LLM_JUDGE_AGENT_RESERVE_USD kept for
    the agent: the judge cannot starve the agent and nothing exceeds the shared cap;
  * one attempt, no malformed retry, a read timeout of SETUHAUL_LLM_JUDGE_TIMEOUT_S (default 20, at most 25);
  * no loop: a judgement never enqueues, never triggers a turn and is never itself judged.
Settled at the provider-reported cost (0023); an unknown cost keeps the full reservation (conservative).
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from . import judge_core as JC

log = logging.getLogger("setuhaul.online_judge")

EVAL_VERSION = "online-judge-v1"
ONLINE_DAILY_MAX_USD = Decimal("1.00")
RUN_CAP_MAX_USD = Decimal("0.05")
TIMEOUT_MAX_S = 25.0
MAX_REPLY_CHARS = 2000
NAMESPACE = uuid.UUID("6f1d2c1e-3b0a-4d9e-9a51-6a5e0b7c4d21")
SITUATION = "A live synthetic-pilot conversation between the booking assistant and a truck driver about dock time."
LANGS = {"en": "en", "hi": "hi", "hi-Latn": "hi-Latn", "hinglish": "hi-Latn", "hi_latn": "hi-Latn"}
CALIBRATION = "HUMAN_CALIBRATION_OUTSTANDING"


def _dec(v, default: str) -> Decimal:
    try:
        d = Decimal(str(v if v not in (None, "") else default).strip())
        return d if d.is_finite() else Decimal(default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


@dataclass(frozen=True)
class JudgeConfig:
    enabled: bool
    reason: str | None                 # why it is off (names only)
    environment: str
    daily_budget_usd: Decimal
    run_cap_usd: Decimal
    agent_reserve_usd: Decimal
    shared_cap_usd: Decimal
    sample_rate: float
    timeout_s: float


def config_from_env(env: dict) -> JudgeConfig:
    """Resolved with setuhaul.ops.features (pilot / dev / eval, synthetic, never production). Never raises."""
    from ..ops.features import resolve
    f = resolve(env)
    environment = f.environment
    daily = _dec(env.get("SETUHAUL_LLM_JUDGE_DAILY_BUDGET_USD"), "0")
    run_cap = _dec(env.get("SETUHAUL_LLM_JUDGE_RUN_CAP_USD"), "0.01")
    shared = _dec(env.get("SETUHAUL_LLM_DAILY_BUDGET_USD"), "0")
    reserve = _dec(env.get("SETUHAUL_LLM_JUDGE_AGENT_RESERVE_USD"), str(shared / 2))
    try:
        rate = min(1.0, max(0.0, float(env.get("SETUHAUL_LLM_JUDGE_SAMPLE_RATE") or 0.2)))
    except ValueError:
        rate = 0.0
    try:
        timeout = min(TIMEOUT_MAX_S, max(1.0, float(env.get("SETUHAUL_LLM_JUDGE_TIMEOUT_S") or 20)))
    except ValueError:
        timeout = 20.0
    reason = None
    if not f.llm_judge_enabled:
        reason = next((n for n in f.notes if n.startswith("llm_judge")), "llm_judge: SETUHAUL_LLM_JUDGE_ENABLED is not true")
    elif daily > ONLINE_DAILY_MAX_USD:
        reason = f"llm_judge: daily budget above the {ONLINE_DAILY_MAX_USD} maximum"
    elif not (Decimal(0) < run_cap <= RUN_CAP_MAX_USD):
        reason = f"llm_judge: per-judgement cap must be > 0 and <= {RUN_CAP_MAX_USD}"
    elif shared <= 0 or reserve < 0 or shared - reserve <= 0:
        reason = "llm_judge: no room in the shared daily budget after the agent's reserve"
    elif rate <= 0:
        reason = "llm_judge: sample rate is 0"
    return JudgeConfig(reason is None, reason, environment, daily, run_cap, reserve, shared, rate, timeout)


def sampled(turn_ref: str, rate: float) -> bool:
    """Deterministic on turn_ref: a redelivered turn gets the same decision."""
    h = int(hashlib.sha256(f"setuhaul-judge-sample:{turn_ref}".encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


def ineligible(res, driver_text: str | None) -> str | None:
    """Why this turn is not eligible (None: eligible)."""
    if res is None or not getattr(res, "turn_ref", None):
        return "no_turn"
    reply = getattr(res, "reply", None)
    if not isinstance(reply, str) or not reply.strip():
        return "no_reply"
    if len(reply) > MAX_REPLY_CHARS:
        return "reply_too_long"
    if not (driver_text or "").strip():
        return "no_driver_message"
    return None


class JudgeLedger:
    """The judge's meter (ModelBudget's reserve/settle contract) for ONE judgement: per-judgement cap, the judge's
    daily sub-cap and the shared cap (minus the agent's reserve), all in one ledger transaction. The call id is derived
    from the turn, so the same turn can never be reserved (paid) twice."""

    def __init__(self, db, cfg: JudgeConfig, turn_ref: str):
        from .metering import ModelBudget
        self._inner = ModelBudget(db, cfg.environment, cfg.shared_cap_usd - cfg.agent_reserve_usd)
        self.db, self.cfg = db, cfg
        self.call_id = uuid.uuid5(NAMESPACE, f"judge:{turn_ref}")
        self.refusal: str | None = None

    def reserve(self, call_id, attempt, purpose, model, provider, reserve_usd):
        from .metering import Reservation, _ledger_tx
        amount = Decimal(str(reserve_usd))
        if purpose != "judge":
            raise ValueError("JudgeLedger meters judge calls only")
        if amount > self.cfg.run_cap_usd:
            self.refusal = "run_cap"
            return Reservation(uuid.uuid4(), self.call_id, attempt, "BUDGET_EXHAUSTED", Decimal(0))
        rid = uuid.uuid4()
        with _ledger_tx(self.db) as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext('setuhaul.online_judge.budget'))")
            spent = Decimal(str(c.execute("SELECT judge_spent_today()").fetchone()[0] or 0))
            if spent + amount > self.cfg.daily_budget_usd:
                self.refusal = "judge_daily_cap"
                return Reservation(rid, self.call_id, attempt, "BUDGET_EXHAUSTED", Decimal(0))
            status = c.execute("SELECT reserve_model_call(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               (rid, self.call_id, attempt, self.cfg.environment, purpose, model, provider, amount,
                                self._inner.daily_cap_usd)).fetchone()[0]
        if status == "BUDGET_EXHAUSTED":
            self.refusal = "shared_daily_cap"
        elif status == "DUPLICATE":
            self.refusal = "already_judged"
        return Reservation(rid, self.call_id, attempt, status, amount)

    def settle(self, r, **kw):
        return self._inner.settle(r, **kw)


class OnlineJudge:
    """Built once per worker container. `client` is an OpenRouterModel (pinned ZDR route, max_attempts=1) or a fake
    with .judge(system, user, tool) in tests; its .meter is replaced per judgement."""

    def __init__(self, cfg: JudgeConfig, db, client=None, tracer=None):
        self.cfg, self.db, self.client, self.tracer = cfg, db, client, tracer
        self.stats = {"considered": 0, "disabled": 0, "ineligible": 0, "sampled_out": 0, "scored": 0, "unparseable": 0,
                      "skipped_budget": 0, "errors": 0, "record_errors": 0}

    # ------------------------------------------------------------ the only entry point; never raises
    def after_turn(self, res, driver_text: str | None, language: str | None) -> dict:
        try:
            return self._after_turn(res, driver_text, language)
        except Exception as e:                                      # noqa: BLE001 — the judge never breaks a turn
            self.stats["errors"] += 1
            log.warning("online judge failed outside a call (%s)", type(e).__name__)
            return {"status": "ERROR", "reason": type(e).__name__}

    def _after_turn(self, res, driver_text, language) -> dict:
        self.stats["considered"] += 1
        if not self.cfg.enabled or self.client is None:
            self.stats["disabled"] += 1
            return {"status": "DISABLED", "reason": self.cfg.reason or "no_client"}
        why = ineligible(res, driver_text)
        if why:
            self.stats["ineligible"] += 1
            return {"status": "INELIGIBLE", "reason": why}
        if not sampled(res.turn_ref, self.cfg.sample_rate):
            self.stats["sampled_out"] += 1
            return {"status": "SAMPLED_OUT"}
        lang = LANGS.get(str(language or "en"), "en")
        example = {"id": str(res.turn_ref), "language": lang, "situation": SITUATION,
                   "conversation": [str(driver_text)[:2000]], "reply": res.reply}
        trace_id = str(uuid.uuid5(NAMESPACE, f"judge-trace:{res.turn_ref}"))
        meta = {"turn_ref": res.turn_ref, "parent_trace_id": res.turn_ref, "correlation_id": res.correlation_id,
                "driver_id": res.driver_id, "shipment_id": res.shipment_id, "facility_id": res.facility_id,
                "prompt_version": JC.JUDGE_PROMPT_VERSION, "eval_version": EVAL_VERSION, "advisory": True,
                "calibration_status": CALIBRATION, "component": "llm_judge",
                "thread_id": getattr(res, "conversation_ref", None)}
        # a child of its agent_turn when that turn was traced (same trace, follows the turn's sampling); a root run
        # of its own otherwise (tracer without child(), as before)
        parent = getattr(res, "trace_parent", None)
        if self.tracer is None:
            ctx = nullcontext(None)
        elif parent and hasattr(self.tracer, "child"):
            ctx = self.tracer.child(parent, trace_id, "llm_judge", metadata=meta)
        else:
            ctx = self.tracer.turn(trace_id, "llm_judge", metadata=meta)
        ledger = JudgeLedger(self.db, self.cfg, res.turn_ref)
        t0 = time.monotonic()
        with ctx as run:
            self.client.meter = ledger
            try:
                j = JC.score_reply(example, self.client, max_attempts=1)
                status, reason = j.status, j.reason
            except Exception as e:                                  # noqa: BLE001
                j = JC.Judgement(example_id=str(res.turn_ref), status="ERROR")
                cat = getattr(e, "category", None) or type(e).__name__
                if ledger.refusal:
                    status, reason = "SKIPPED", f"budget:{ledger.refusal}"
                else:
                    status, reason = "ERROR", f"provider:{cat}"[:200]
            finally:
                self.client.meter = None
            if run is not None:
                _safe(run.set, judge_status=status, judge_reason=reason, **{f"quality:{d}": (
                    j.dimensions[d].score if d in j.dimensions else None) for d in JC.DIMENSIONS})
        if status == "SKIPPED" and ledger.refusal == "already_judged":
            return {"status": "DUPLICATE"}                          # a redelivery: the first attempt owns the row
        self.stats[{"SCORED": "scored", "UNPARSEABLE": "unparseable", "SKIPPED": "skipped_budget"}.get(status, "errors")] += 1
        row = {"turn_ref": str(res.turn_ref), "correlation_id": res.correlation_id,
               "trace_id": trace_id if self.tracer is not None and getattr(self.tracer, "enabled", False) else None,
               "facility_id": res.facility_id, "driver_id": res.driver_id, "shipment_id": res.shipment_id,
               "driver_message_id": _int_or_none(getattr(res, "driver_message_id", None)), "status": status, "reason": reason, "model": j.served_model or getattr(self.client, "extract_model", None),
               "provider": j.actual_provider, "prompt_version": JC.JUDGE_PROMPT_VERSION, "eval_version": EVAL_VERSION,
               "details": {d: {"rationale": v.rationale, "evidence": v.evidence,
                               "insufficient_evidence": v.insufficient_evidence} for d, v in j.dimensions.items()},
               "input_tokens": j.input_tokens, "output_tokens": j.output_tokens,
               "cost_usd": None if j.cost_usd is None else str(j.cost_usd),
               "latency_ms": j.latency_ms if j.latency_ms is not None else int((time.monotonic() - t0) * 1000)}
        for d in JC.DIMENSIONS:
            row[d] = j.dimensions[d].score if d in j.dimensions else None
        self._record(row)
        if status == "SCORED":
            self._feedback(res.turn_ref, j)
        log.info("online judge turn=%s status=%s reason=%s cost_usd=%s advisory=true", res.turn_ref, status, reason,
                 row["cost_usd"])
        return {"status": status, "reason": reason, "scores": {d: row[d] for d in JC.DIMENSIONS}}

    def _record(self, row: dict) -> None:
        """As the agent role, in the turn's own facility scope (0043 forces RLS like agent_turn_metrics), outside the
        turn fence (the turn is over; the judgement is evidence, not part of the conversation)."""
        from psycopg.types.json import Jsonb
        from ..api.db import TURN_FENCE
        token = TURN_FENCE.set(None)
        try:
            fac = row.get("facility_id")
            with self.db.tx("agent", scope=[fac] if fac else None) as c:
                c.execute("SELECT record_turn_judgement(%s)", (Jsonb(row),))
        except Exception as e:                                      # noqa: BLE001 — visible, never fatal
            self.stats["record_errors"] += 1
            log.warning("ALERT online judge result not recorded (%s)", type(e).__name__)
        finally:
            TURN_FENCE.reset(token)

    def _feedback(self, turn_ref: str, j) -> None:
        """Best effort: attach quality:* feedback to the agent turn's root run in LangSmith (run id = turn_ref)."""
        client = getattr(getattr(self.tracer, "exporter", None), "client", None)
        if client is None or not getattr(self.tracer, "enabled", False):
            return
        for d in JC.DIMENSIONS:
            if d in j.dimensions:
                _safe(client.create_feedback, str(turn_ref), f"quality:{d}", j.dimensions[d].score,
                      f"advisory; {CALIBRATION.lower()}; prompt {JC.JUDGE_PROMPT_VERSION}")


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:                                               # noqa: BLE001
        return None


def online_judge_from_env(env: dict, db, tracer=None) -> OnlineJudge:
    """Never raises: any configuration or key problem yields a disabled judge with the reason logged."""
    cfg = config_from_env(env)
    if not cfg.enabled:
        log.info("online judge off: %s", cfg.reason)
        return OnlineJudge(cfg, db, None, tracer)
    try:
        from .openrouter import DEFAULT_PROVIDER, HAIKU_OR, OpenRouterModel, load_openrouter_key
        key = load_openrouter_key(env, cfg.environment)
        client = OpenRouterModel(key, extract_model=(env.get("SETUHAUL_OR_EXTRACT_MODEL") or HAIKU_OR).strip(),
                                 provider=(env.get("SETUHAUL_OR_PROVIDER") or DEFAULT_PROVIDER).strip(),
                                 read_timeout_s=cfg.timeout_s)
        client.max_attempts = 1                                     # bounded: never a provider retry for the judge
    except Exception as e:                                          # noqa: BLE001
        log.error("ALERT online judge client not built (%s): judging is skipped", type(e).__name__)
        return OnlineJudge(cfg, db, None, tracer)
    log.info("online judge on: sample_rate=%s daily_usd=%s run_cap_usd=%s timeout_s=%s advisory=true",
             cfg.sample_rate, cfg.daily_budget_usd, cfg.run_cap_usd, cfg.timeout_s)
    return OnlineJudge(cfg, db, client, tracer)
