"""B-EV-1b: build the held-out set h1 from G3 v4 (07_evaluation_plan.md §2: paraphrase, time shift, extra detail).

    SETUHAUL_ENV=eval SETUHAUL_DATA_CLASS=synthetic OPENROUTER_API_KEY=... python evals/make_heldout.py

Independence rules, because the person who wrote the extraction prompt also wrote G3:
  * paraphrases and extra-detail variants are written by a DIFFERENT model (Sonnet 5), which never sees the extraction
    prompt, the schema or the labels: only the driver's message and a plain list of facts that must survive;
  * labels are never written by the generator: each variant inherits its base turn's labels (time-shift variants get
    them shifted by code);
  * automatic checks drop any variant that loses or invents a number, a clock time or an order reference, or
    changes the script (Devanagari vs Latin); drops are recorded;
  * the set is frozen (hash in `h1/README.md`) BEFORE any model is evaluated on it, and is never used to tune prompts.
Each variant is a one-turn scenario carrying its base turn's context, so `evals/run_golden.py --version h1` grades it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from evals.golden import ROOT, Scenario, load  # noqa: E402
from evals.providers import OPENROUTER_URL, require_synthetic_eval  # noqa: E402

GENERATOR = "anthropic/claude-sonnet-5"
SHIFT_MIN = 40
OUT = ROOT / "h1"
DEV = re.compile(r"[ऀ-ॿ]")
CLOCK = re.compile(r"\b(\d{1,2})([:.])(\d{2})\b")
OTHER_TIME = re.compile(r"\b\d{1,2}\s*(am|pm|baje)\b|\b(after|by|at|before|till|until)\s+\d{1,2}\b(?![:.])|gyarah|"
                        r"\d{1,2}\s*o.clock", re.I)
REF = re.compile(r"ORD-\d{6}-\d{3}", re.I)

SYSTEM = """You help build a test set for a logistics chat assistant in India. You rewrite WhatsApp messages written by \
truck drivers. For each message write two versions:
1. "paraphrase": the same message said differently, as another driver might write it. Same meaning, same facts.
2. "detail": the original message with one short sentence of irrelevant detail added (weather, food, family, road \
chatter) that changes no fact. For one-word answers (yes, no, a number) keep the answer first and add the detail \
as a separate sentence.
Rules for both versions:
- Keep every number, clock time, duration, order reference and dock code exactly as written. Do not add new times.
- Keep the same language and script: English stays English; Hindi in Devanagari stays Devanagari; Hinglish (Hindi in \
Latin letters) stays Hinglish.
- Keep every fact in the "keep" list true.
- Do not add any danger, injury or accident unless the original has one. Do not turn a plain yes into a conditional one.
Reply with JSON only: {"items": [{"i": <number>, "paraphrase": "...", "detail": "..."}]}"""

INTENT = {"REPORT_DELAY": "reports being late", "ASK_OPTIONS": "asks which slots are possible",
          "CHECK_STATUS": "asks about the status of their booking", "EARLY_ARRIVAL": "says they arrived early",
          "BOOK": "chooses or books a slot", "CANCEL": "asks to cancel", "OTHER": "says something else"}
LANG = {"en": "English", "hi": "Hindi in Devanagari script", "hi-Latn": "Hinglish in Latin letters"}


def facts(t) -> list[str]:
    x, f = t.x or {}, []
    it = x.get("intent")
    if isinstance(it, str):
        f.append("the driver " + INTENT[it])
    if x.get("safety") is True:
        f.append("there is a danger, injury or safety problem")
    elif x.get("safety") is False or "safety" not in x:
        f.append("there is no danger, injury or accident")
    if "yes" in x:
        f.append("the driver gives a plain yes" if x["yes"] else "the driver does NOT give a plain, unconditional yes")
    if x.get("pick") is not None:
        f.append(f"the driver chooses option number {x['pick'] + 1}")
    elif "pick" in x:
        f.append("the driver does not choose any of the listed options")
    if x.get("exc") == "BREAKDOWN":
        f.append("there is a problem with the truck")
    if x.get("kind") == "REPAIR_DURATION":
        f.append("the number of minutes is how long the repair takes")
    lang = x.get("lang") if isinstance(x.get("lang"), str) else ("hi" if DEV.search(t.say) else None)
    if lang:
        f.append("language: " + LANG[lang])
    return f


def numbers(s: str) -> list[str]:
    return sorted(re.findall(r"\d+", s))


def problems(orig: str, var: str) -> list[str]:
    bad = []
    if not var or var.strip() == orig.strip():
        bad.append("empty or unchanged")
    on, vn = numbers(orig), numbers(var)
    missing = [n for n in set(on) if on.count(n) > vn.count(n)]
    if missing:
        bad.append(f"numbers lost: {missing}")
    new_clock = {m.group(0) for m in CLOCK.finditer(var)} - {m.group(0) for m in CLOCK.finditer(orig)}
    if new_clock:
        bad.append(f"new clock times: {sorted(new_clock)}")
    if {r.upper() for r in REF.findall(orig)} != {r.upper() for r in REF.findall(var)}:
        bad.append("order reference changed")
    if bool(DEV.search(orig)) != bool(DEV.search(var)):
        bad.append("script changed")
    return bad


def ask_generator(client: httpx.Client, key: str, items: list[dict]) -> tuple[list[dict], dict]:
    body = {"model": GENERATOR, "max_tokens": 2000,
            "provider": {"data_collection": "deny"},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps({"messages": items}, ensure_ascii=False)}]}
    r = client.post(OPENROUTER_URL, headers={"Authorization": f"Bearer {key}", "X-Title": "setuhaul-heldout"}, json=body)
    d = r.json()
    if r.status_code != 200 or "error" in d:
        raise RuntimeError(f"generator {r.status_code}: {d.get('error')}")
    text = d["choices"][0]["message"]["content"]
    text = text if isinstance(text, str) else "".join(p.get("text", "") for p in text)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0))["items"], {"usage": d.get("usage"), "provider": d.get("provider")}


def shift_hhmm(hhmm: str, minutes: int) -> str | None:
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + minutes
    return None if t >= 24 * 60 else f"{t // 60:02d}:{t % 60:02d}"


def shift_text(text: str, minutes: int) -> str | None:
    def rep(mo):
        h, sep, mm = int(mo.group(1)), mo.group(2), int(mo.group(3))
        t = h * 60 + mm + minutes
        nh, nm = t // 60, t % 60
        if h >= 13 or h == 0:                     # 24-hour style stays 24-hour
            return f"{nh:02d}{sep}{nm:02d}" if nh < 24 else "XX"
        nh12 = (nh - 1) % 12 + 1                  # 12-hour style stays 12-hour (code resolves AM/PM from the clock)
        return f"{nh12}{sep}{nm:02d}"
    out = CLOCK.sub(rep, text)
    return None if "XX" in out else out


def time_shift(s: Scenario, i: int, t) -> dict | None:
    if not CLOCK.search(t.say) or OTHER_TIME.search(t.say):
        return None
    now = shift_hhmm(t.at or s.now, SHIFT_MIN)
    text = shift_text(t.say, SHIFT_MIN)
    if not now or not text:
        return None
    x = dict(t.x)
    for k in ("eta", "earliest", "latest"):
        if x.get(k):
            x[k] = shift_hhmm(x[k], SHIFT_MIN)
            if x[k] is None:
                return None
    ctx = t.ctx.model_dump()
    if ctx.get("base_eta"):
        ctx["base_eta"] = shift_hhmm(ctx["base_eta"], SHIFT_MIN)
    ctx["options"] = [f"{o.split()[0]} {shift_hhmm(o.split()[1], SHIFT_MIN)}" for o in ctx.get("options", [])]
    if any(o.endswith("None") for o in ctx["options"]) or (t.ctx.base_eta and not ctx["base_eta"]):
        return None
    return {"say": text, "now": now, "x": x, "ctx": ctx}


def variant(s: Scenario, i: int, t, kind: str, say: str, now: str | None = None, x: dict | None = None,
            ctx: dict | None = None) -> dict:
    turn = t.model_dump(exclude_none=True)
    turn.update(say=say, x=x if x is not None else t.x)
    turn.pop("at", None)
    if ctx is not None:
        turn["ctx"] = ctx
    return {"id": f"{s.id}#{i}~{kind}", "group": s.group, "title": f"{kind} variant of {s.id} turn {i}",
            "source": f"held-out h1 from G3 v4 {s.id} turn {i}", "config": s.config, "driver": s.driver,
            "shipment": s.shipment, "now": now or t.at or s.now, "turns": [turn], "gates": s.gates,
            "notes": f"base: {t.say}"}


def main():
    import argparse
    from evals.budget import add_args, from_args, print_estimate
    ap = argparse.ArgumentParser()
    add_args(ap)
    a = ap.parse_args()
    budget, _ = from_args(a)                        # generation is never cached: each call writes new variants
    key = os.environ.get("OPENROUTER_API_KEY", "")
    require_synthetic_eval()                         # synthetic data, evaluation environment only (T52)
    scenarios, ds = load("v4")
    out, log, dropped = [], [], []
    client = httpx.Client(timeout=90)
    n_calls = sum(1 for s in scenarios if any(t.x and t.duplicate_of is None for t in s.turns))
    est = print_estimate("make_heldout", GENERATOR, n_calls, 0, "generate", budget)
    if a.estimate_only:
        return
    budget.authorise(est)
    for s in scenarios:
        turns = [(i, t) for i, t in enumerate(s.turns) if t.x and t.duplicate_of is None]
        if not turns:
            continue
        for i, t in turns:
            ts = time_shift(s, i, t)
            if ts:
                out.append(variant(s, i, t, "time", ts["say"], ts["now"], ts["x"], ts["ctx"]))
        items = [{"i": n, "message": t.say, "keep": facts(t)} for n, (i, t) in enumerate(turns)]
        try:
            budget.reserve(GENERATOR, "generate")
            got, meta = ask_generator(client, key, items)
            u = meta.get("usage") or {}
            budget.charge(GENERATOR, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        except Exception as e:
            log.append({"scenario": s.id, "error": str(e)})
            print(s.id, "generator error:", e, flush=True)
            continue
        log.append({"scenario": s.id, "meta": meta, "items": got})
        by_n = {g.get("i"): g for g in got if isinstance(g, dict)}
        for n, (i, t) in enumerate(turns):
            g = by_n.get(n, {})
            for kind in ("paraphrase", "detail"):
                v = (g.get(kind) or "").strip()
                bad = problems(t.say, v)
                if bad:
                    dropped.append({"base": f"{s.id}#{i}", "kind": kind, "variant": v, "why": bad})
                else:
                    out.append(variant(s, i, t, kind, v))
        print(s.id, "ok", flush=True)
        time.sleep(0.2)
    for r in out:                                     # every variant must be a valid scenario
        Scenario(**r)
    OUT.mkdir(exist_ok=True)
    (OUT / "scenarios.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in out),
                                         encoding="utf-8")
    (OUT / "generation.json").write_text(json.dumps({"base_dataset": ds, "generator": GENERATOR, "system": SYSTEM,
                                                     "dropped": dropped, "calls": log}, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    kinds = {k: sum(r["id"].endswith("~" + k) for r in out) for k in ("paraphrase", "detail", "time")}
    print(json.dumps({"variants": len(out), **kinds, "dropped": len(dropped), "cost": budget.report(est)}, indent=2))


if __name__ == "__main__":
    main()
