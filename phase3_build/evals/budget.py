"""Spend control for every runner that can call a paid model (review of 18 Nov: tokenomics, Phase 1).

Policy (decision T42):
  1. No paid inference without a cost estimate first. `--estimate-only` prints it and stops.
  2. No run above the cap (default $0.10, SETUHAUL_RUN_CAP_USD) without `--approved --max-usd X`.
  3. Every call, retries included, is checked against the run's remaining budget BEFORE it is made; the run stops
     when the next call would not fit. One counter per run, so retries or parallel callers cannot exceed it.
  4. Results are cached by (model, prompt version, schema version, the exact system prompt + tool schema, and the
     model input). An unchanged scenario makes zero API calls. `--no-cache` forces fresh calls.
  5. Every run records its estimate, its cost, how many calls were made and how many came from the cache. Cost is
     labelled by how it is known: `computed` (provider-reported tokens x published price) here; `inferred` only in
     after-the-fact ledgers (see COSTS.md). An inferred number is never presented as measured.

Cache key normalisation, stated so it can be challenged: option ids (fresh UUIDs every run) become OPT1..OPTn by
position, and calendar dates become <DATE>, because the extraction returns clock times only. A cached
`selected_option_id` is stored as its position and mapped back to the current id.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from setuhaul.agent.extraction import RECORD_DRIVER_REPORT, SCHEMA_VERSION
from setuhaul.agent.models import Trace
from setuhaul.agent.prompts import EXTRACT_SYSTEM, PHRASE_SYSTEM, PROMPT_VERSION

CACHE = Path(__file__).resolve().parent / "cache" / "model_calls.jsonl"
DEFAULT_CAP = float(os.environ.get("SETUHAUL_RUN_CAP_USD", "0.10"))

# Published list prices, USD per million tokens (input, output). Batch variants are half price on OpenRouter.
PRICES = {
    "anthropic/claude-haiku-4.5": (1.00, 5.00), "anthropic/claude-haiku-4.5:batch": (0.50, 2.50),
    "anthropic/claude-sonnet-5": (2.00, 10.00), "anthropic/claude-sonnet-5:batch": (1.00, 5.00),
    "in.anthropic.claude-haiku-4-5-20251001-v1:0": (1.00, 5.00), "in.anthropic.claude-sonnet-5": (2.00, 10.00),
}
# Measured averages per call (G3 and h1 runs, 18 Nov): extraction 1,950 in / 331 out.
TYPICAL = {"extract": (1950, 335), "phrase": (900, 120), "generate": (460, 170)}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|opt-\d+-[A-Za-z0-9-]+")
_DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")


class BudgetExceeded(Exception):
    """The next paid call would exceed this run's limit. Nothing was sent."""


def price(model_id: str) -> tuple[float, float]:
    for k, v in PRICES.items():
        if model_id == k:
            return v
    raise BudgetExceeded(f"no published price for {model_id!r}: refusing to call a model whose cost is unknown")


def cost(model_id: str, tokens_in: int, tokens_out: int) -> float:
    pi, po = price(model_id)
    return (tokens_in * pi + tokens_out * po) / 1e6


def estimate(model_id: str, calls: int, purpose: str = "extract") -> float:
    tin, tout = TYPICAL[purpose]
    return round(cost(model_id, tin * calls, tout * calls), 4)


@dataclass
class Budget:
    """One spending limit for one run."""
    cap: float = DEFAULT_CAP
    approved: bool = False
    max_usd: float | None = None
    spent: float = 0.0
    calls: int = 0
    cached: int = 0
    refused: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def limit(self) -> float:
        if self.approved:
            if self.max_usd is None:
                raise BudgetExceeded("--approved needs an explicit --max-usd")
            return self.max_usd
        return self.cap

    def authorise(self, estimated: float) -> None:
        """Rule 2: refuse the whole run up front if its estimate is over the limit."""
        if estimated > self.limit:
            raise BudgetExceeded(f"estimated ${estimated:.4f} is over the ${self.limit:.2f} limit"
                                 + ("" if self.approved else "; rerun with --approved --max-usd <amount>"))

    def reserve(self, model_id: str, purpose: str) -> float:
        """Rule 3: before each call (retries too), make sure a typical call still fits."""
        need = estimate(model_id, 1, purpose)
        with self._lock:
            if self.spent + need > self.limit:
                self.refused += 1
                raise BudgetExceeded(f"next call (~${need:.4f}) would pass the ${self.limit:.2f} limit "
                                     f"(${self.spent:.4f} spent): run stopped")
        return need

    def charge(self, model_id: str, tokens_in: int, tokens_out: int) -> float:
        c = cost(model_id, tokens_in, tokens_out)
        with self._lock:
            self.spent += c
            self.calls += 1
        return c

    def report(self, estimated: float | None = None) -> dict:
        return {"estimated_usd": estimated, "spent_usd": round(self.spent, 4), "basis": "computed",
                "paid_calls": self.calls, "cached_calls": self.cached, "refused_calls": self.refused,
                "limit_usd": self.limit}


def _canon(block: str) -> tuple[str, list[str]]:
    ids: list[str] = []

    def rep(m):
        v = m.group(0)
        if v not in ids:
            ids.append(v)
        return f"OPT{ids.index(v) + 1}"
    return _DATES.sub("<DATE>", _UUID.sub(rep, block)), ids


def _key(model_id: str, purpose: str, block: str) -> str:
    system = EXTRACT_SYSTEM + json.dumps(RECORD_DRIVER_REPORT, sort_keys=True) if purpose == "extract" else PHRASE_SYSTEM
    h = hashlib.sha256()
    for part in (model_id, purpose, PROMPT_VERSION, SCHEMA_VERSION, system, block):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


class Cache:
    def __init__(self, path: Path = CACHE, enabled: bool = True):
        self.path, self.enabled = path, enabled
        self.rows: dict[str, dict] = {}
        if enabled and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.rows[r["key"]] = r

    def has(self, model_id: str, purpose: str, block: str) -> bool:
        return self.enabled and _key(model_id, purpose, _canon(block)[0]) in self.rows

    def get(self, model_id: str, purpose: str, block: str):
        if not self.enabled:
            return None
        canon, ids = _canon(block)
        r = self.rows.get(_key(model_id, purpose, canon))
        if r is None:
            return None
        out = r["output"]
        if purpose == "extract" and isinstance(out, dict) and str(out.get("selected_option_id") or "").startswith("OPT"):
            n = int(out["selected_option_id"][3:]) - 1
            out = {**out, "selected_option_id": ids[n] if n < len(ids) else None}
        return out

    def put(self, model_id: str, purpose: str, block: str, output: Any, trace: Trace) -> None:
        if not self.enabled:
            return
        canon, ids = _canon(block)
        if purpose == "extract" and isinstance(output, dict) and output.get("selected_option_id") in ids:
            output = {**output, "selected_option_id": f"OPT{ids.index(output['selected_option_id']) + 1}"}
        row = {"key": _key(model_id, purpose, canon), "model": model_id, "purpose": purpose,
               "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION, "output": output,
               "tokens_in": trace.input_tokens, "tokens_out": trace.output_tokens, "served_by": trace.region}
        self.rows[row["key"]] = row
        self.path.parent.mkdir(exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class GuardedModel:
    """Wraps any ModelClient: cache first, then budget check, then the paid call, then charge and store."""

    def __init__(self, model, budget: Budget, cache: Cache):
        self.model, self.budget, self.cache = model, budget, cache
        self.extract_model = getattr(model, "extract_model", "?")
        self.phrase_model = getattr(model, "phrase_model", "?")

    def _run(self, purpose: str, model_id: str, block: str, call):
        hit = self.cache.get(model_id, purpose, block)
        if hit is not None:
            self.budget.cached += 1
            return hit, Trace(purpose, model_id, "cache", outcome="cached")
        self.budget.reserve(model_id, purpose)                       # a failed call (ModelError) is not charged:
        out, tr = call()                                             # the provider reports no usage for it
        self.budget.charge(model_id, tr.input_tokens, tr.output_tokens)
        self.cache.put(model_id, purpose, block, out, tr)
        return out, tr

    def extract(self, user_block: str):
        return self._run("extract", self.extract_model, user_block, lambda: self.model.extract(user_block))

    def phrase(self, facts_block: str, correction: str | None = None):
        block = facts_block + (f"\n[correction] {correction}" if correction else "")
        return self._run("phrase", self.phrase_model, block, lambda: self.model.phrase(facts_block, correction))


def add_args(ap) -> None:
    ap.add_argument("--estimate-only", action="store_true", help="print the cost estimate and stop; no paid calls")
    ap.add_argument("--approved", action="store_true", help="allow a run above the cap, up to --max-usd")
    ap.add_argument("--max-usd", type=float, help="the spending limit for an approved run")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached results (every call is paid)")


def from_args(a) -> tuple[Budget, Cache]:
    if a.approved and a.max_usd is None:
        raise SystemExit("--approved needs an explicit --max-usd")
    return Budget(approved=a.approved, max_usd=a.max_usd), Cache(enabled=not a.no_cache)


def print_estimate(runner: str, model_id: str, calls: int, cached: int, purpose: str, budget: Budget) -> float:
    est = estimate(model_id, calls, purpose)
    tin, tout = TYPICAL[purpose]
    try:
        budget.authorise(est)
        status = "APPROVED" if calls else "NOTHING TO PAY FOR"
    except BudgetExceeded as e:
        status = f"REFUSED: {e}"
    print(f"RUN COST ESTIMATE\n  runner: {runner}\n  model: {model_id}\n  paid calls: {calls} (cached: {cached})\n"
          f"  tokens: ~{tin * calls:,} in / ~{tout * calls:,} out\n  estimated cost: ${est:.4f}\n"
          f"  limit: ${budget.limit:.2f}\n  status: {status}", flush=True)
    return est


def backfill(result_file: Path, cache: Cache | None = None) -> int:
    """Seed the cache from a recorded golden run (Level 1: reuse recorded outputs). Only rows whose run used the
    CURRENT prompt and schema versions are added; the key also hashes the current prompt text, so a later edit to
    the prompt without a version bump still misses."""
    from evals.golden import load, user_block
    cache = cache or Cache()
    d = json.loads(Path(result_file).read_text(encoding="utf-8"))
    run = d["run"]
    if run.get("prompt_version") != PROMPT_VERSION or run.get("schema_version") != SCHEMA_VERSION or not run.get("model_id"):
        return 0
    version = run["dataset"].split("+")[0]
    scenarios = {s.id: s for s in load(version)[0]}
    n = 0
    for r in d["turns"]:
        if "raw" not in r or r.get("error") or r["scenario"] not in scenarios:
            continue
        s = scenarios[r["scenario"]]
        t = s.turns[r["turn"]]
        block = user_block(s, t)
        if cache.has(run["model_id"], "extract", block):
            continue
        tr = Trace("extract", run["model_id"], r.get("served_by", "recorded"))
        cache.put(run["model_id"], "extract", block, r["raw"], tr)
        n += 1
    return n
