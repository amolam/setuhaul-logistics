"""B-AI-3, templates vs Sonnet for option replies, in the order the cost review set (decision T42):
stages 1–4 deterministic checks (free), stage 5 a human review sheet (free), stage 6 a Sonnet judge (deferred).

    python evals/phrase_ablation.py                       # template arm + checks + review sheet: no paid calls
    python evals/phrase_ablation.py --arm sonnet --estimate-only
    python evals/phrase_ablation.py --arm sonnet --approved --max-usd 0.10    # only with explicit approval

Scope: before owner decision OD-3 a phrasing model was used for ONE reply type, the list of slot options; status,
confirmations, escalations and safety replies were always templates. Since OD-3 production sends the template for
option lists too and calls no phrasing model; `sonnet_arm` keeps a copy of the old loop for re-runs only.
So this ablation compares option replies only.

Situations: 20 option replies the end-to-end replay actually produced (`evals/results/replay_v4_scripted_*.json`),
picked for variety and listed by id below, frozen in `b_ai_3/situations.json`. Both arms get exactly the inputs the
orchestrator uses (`replies.phrasing_inputs`); the Sonnet arm runs the (pre-OD-3) orchestrator's draft -> grounding check
-> redraft -> template-fallback loop, so it measures what a driver would actually receive.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.golden import load  # noqa: E402
from setuhaul.agent import replies  # noqa: E402
from setuhaul.agent.models import ModelError  # noqa: E402

OUT = Path(__file__).resolve().parent / "b_ai_3"
# 4 non-English conversations, then 16 English ones for variety (delay, range, "after 7", gate-out, urgent, priority
# spoof, broken dock, in the yard, refused booking, repaired breakdown, grace boundary, a plain "yes").
PICK = ["H01#1", "H02#1", "H03#0", "H05#0", "BB01#1", "BB02#0", "BB03#0", "BB09#0", "BB10#1", "L07#0", "L04#1",
        "THR004#0", "THR008#0", "THR011#0", "AT01#1", "AT02#0", "AT04#0", "ADV02#0", "P05#0", "P07#0"]
DEV = re.compile(r"[ऀ-ॿ]")
HINGLISH = replies.HINGLISH_WORDS
ASK_NUMBER = re.compile(r"number|नंबर|nambar|bhejo|bhejein|bhejiye|bhej dijiye|reply with", re.I)
INTERNAL_ID = re.compile(r"\b(APT|SHP|THR|ESC|EXC)[\w-]*\d|[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
TIME_24 = re.compile(r"\b\d{1,2}:\d{2}\b(?!\s?(AM|PM|am|pm))")
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
APPROVAL = re.compile(r"approval|मंज़ूरी|मंजूरी|manzoori|approve", re.I)
RUBRIC = ["clarity (1-5)", "tone (1-5)", "language and register (1-5)", "states status without overpromising (1-5)",
          "answers what the driver asked, and shows that their stated constraint was applied (1-5)"]
# R1.9 S4-10: conversational completeness, which no code check catches. The reviewer flagged these two.
CONTEXT_NOTES = {
    "AT04": "The driver asked whether the old 10:00 D1 slot still works. Does the reply say what happens to it before "
            "offering new slots? (Only if the booking service states that status; the template does not today.)",
    "BB10": "The driver gave a gate-out constraint. Does the reply make clear the new options respect it?",
}


# ---------------------------------------------------------------- stages 1–4: what code can check
def checks(reply: str, options: list[dict], conv_lang: str) -> dict[str, list[str]]:
    """Problems per stage. An empty list means the stage passed. Register (Hinglish vs English) is reported as
    `register`, separately: code can see it, but whether it matters is a human judgement (stage 5)."""
    _, facts, _ = replies.phrasing_inputs(options, conv_lang)
    out = {"grounding": replies.grounding_violations(reply, facts), "completeness": [], "language": [], "policy": [],
           "register": []}
    for o in options:
        t = o["start"].split(" on ")[0]
        if o["dock_code"] not in reply or t not in reply:
            out["completeness"].append(f"option {o['dock_code']} {t} missing")
    numbered = len(re.findall(r"^\s*\d+[.)]\s", reply, re.M))
    if numbered < len(options):
        out["completeness"].append(f"{numbered} numbered lines for {len(options)} options")
    if not ASK_NUMBER.search(reply):
        out["completeness"].append("does not ask the driver to reply with a number")
    has_dev = bool(DEV.search(reply))
    if conv_lang == "hi" and not has_dev:
        out["language"].append("Hindi (Devanagari) driver, reply has no Devanagari")
    if conv_lang in ("en", "hi-Latn") and has_dev:
        out["language"].append("Latin-script driver, reply in Devanagari")
    prose = "\n".join(l for l in reply.splitlines() if not re.match(r"^\s*\d+[.)]\s", l))
    markers = len(HINGLISH.findall(prose))
    if conv_lang == "hi-Latn" and markers < 2:
        out["register"].append("Hinglish driver, reply in English")
    if re.search(r"coordinator|कोऑर्डिनेटर", reply, re.I):
        out["policy"].append("promises a coordinator in an options reply")
    if INTERNAL_ID.search(reply):
        out["policy"].append("shows an internal id")
    if len(reply) > 450:
        out["policy"].append(f"{len(reply)} characters (WhatsApp limit for this reply: 450)")
    if len([x for x in re.split(r"[.!?।]+", prose) if x.strip()]) > 3:
        out["policy"].append("more than 3 sentences outside the list")
    if TIME_24.search(reply):
        out["policy"].append("a time without AM/PM")
    if EMOJI.search(reply):
        out["policy"].append("emoji")
    for o in options:
        if o.get("needs_approval"):
            line = next((l for l in reply.splitlines() if o["dock_code"] in l and o["start"].split(" on ")[0] in l), "")
            if not APPROVAL.search(line):
                out["policy"].append(f"{o['dock_code']} needs approval but the reply does not say so")
    return out


# ---------------------------------------------------------------- situations
def conversation_language(s) -> str:
    for t in s.turns:
        lang = (t.x or {}).get("lang")
        if isinstance(lang, str):
            return lang
        if isinstance(lang, list) and lang:
            return lang[0]
    return "hi" if any(DEV.search(t.say) for t in s.turns) else "en"


def build_situations() -> list[dict]:
    path = sorted(glob.glob(str(Path(__file__).resolve().parent / "results" / "replay_v4_scripted_*.json")))[-1]
    rep = json.loads(Path(path).read_text(encoding="utf-8"))
    scen = {s.id: s for s in load("v4")[0]}
    turns = {f"{r['id']}#{t['turn']}": (r, t) for r in rep["scenarios"] for t in r["turns"] if t.get("shown")}
    out = []
    for key in PICK:
        r, t = turns[key]
        s = scen[r["id"]]
        out.append({"id": key, "conversation": [x.say for x in s.turns[:t["turn"] + 1]],
                    "conversation_language": conversation_language(s),
                    "turn_language": (t.get("extraction") or {}).get("language", "en"),
                    "options": t["shown"], "sent_reply": t["reply"]})
    return out


def freeze(situations: list[dict]) -> str:
    OUT.mkdir(exist_ok=True)
    p = OUT / "situations.json"
    if not p.exists():
        p.write_text(json.dumps(situations, indent=2, ensure_ascii=False), encoding="utf-8")
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------- arms
def reply_lang(sit: dict) -> str:
    """The orchestrator's rule (replies.reply_language) applied to this conversation."""
    return replies.reply_language(sit["conversation"][-1], sit["turn_language"], sit["conversation"][:-1])


def template_arm(sit: dict) -> str:
    """What the system sends: the template in the language the orchestrator chooses."""
    return replies.options_text(sit["options"], reply_lang(sit))


def sonnet_arm(sit: dict, model) -> tuple[str, list[str]]:
    """The phrasing loop production used before OD-3 (draft, checks, one redraft, then the template). Production no
    longer calls a phrasing model; this copy exists only so the ablation can be re-run."""
    lang, path = reply_lang(sit), []
    template, facts, block = replies.phrasing_inputs(sit["options"], lang)
    correction = None
    for _ in range(2):
        try:
            draft, _tr = model.phrase(block, correction)
        except ModelError:
            return template, path + ["phrase:template(model-error)"]
        v = replies.structure_violations(draft, template, lang) + replies.grounding_violations(draft, facts)
        if not v:
            return draft, path + ["phrase:model"]
        correction = "; ".join(v)
    return template, path + ["phrase:template(grounding)"]


def score(rows: list[dict]) -> dict:
    stages = ["grounding", "completeness", "language", "policy", "register"]
    return {k: f"{sum(not r['checks'][k] for r in rows)}/{len(rows)}" for k in stages}


def review_sheet(rows: list[dict], sonnet: dict[str, str] | None, digest: str) -> str:
    lines = [f"# B-AI-3 stage 5: human review sheet (situations {digest})", "",
             "For each situation, score each reply 1–5 on: " + "; ".join(RUBRIC) + ". Code already checked grounding, "
             "completeness, script and policy (results beside each reply). Judge what code cannot: is it clear on a phone, "
             "is the tone right for what the driver just said, does the language feel natural, would a driver act on it "
             "correctly?", ""]
    for r in rows:
        lines += [f"## {r['id']} ({r['conversation_language']})", "",
                  "**Driver said:** " + " → ".join(f"“{m}”" for m in r["conversation"]), "",
                  *([f"**Check especially:** {CONTEXT_NOTES[r['id'].split('#')[0]]}", ""] if r["id"].split("#")[0] in CONTEXT_NOTES else []),
                  "**A. Template (sent today):**", "", "```", r["reply"], "```",
                  "Code checks: " + ("all passed" if not any(v for v in r["checks"].values()) else
                                     "; ".join(f"{k}: {', '.join(v)}" for k, v in r["checks"].items() if v)), ""]
        if sonnet and r["id"] in sonnet:
            lines += ["**B. Sonnet:**", "", "```", sonnet[r["id"]], "```", ""]
        else:
            lines += ["**B. Sonnet:** not generated yet (needs approval; see README).", ""]
        lines += ["| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |",
                  "|---|---|---|---|---|---|---|", "| A | | | | | | |", "| B | | | | | | |", ""]
    return "\n".join(lines)


def main():
    from evals.budget import Budget, BudgetExceeded, Cache, GuardedModel, add_args, print_estimate
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["template", "sonnet"], default="template")
    add_args(ap)
    a = ap.parse_args()
    sits = build_situations()
    digest = freeze(sits)
    sits = json.loads((OUT / "situations.json").read_text(encoding="utf-8"))           # always score the frozen set
    rows = []
    for s in sits:
        reply = template_arm(s)
        rows.append({"id": s["id"], "conversation": s["conversation"], "conversation_language": s["conversation_language"],
                     "reply": reply, "checks": checks(reply, s["options"], s["conversation_language"])})
    result = {"situations": digest, "arm": "template", "scores": score(rows), "rows": rows}
    sonnet = None
    if a.arm == "sonnet":
        from evals.providers import model_from_env
        # zero spend unless explicitly approved: without --approved the cap is $0, so every paid call is refused
        budget = Budget(cap=0.0, approved=a.approved, max_usd=a.max_usd)
        raw = model_from_env()
        est = print_estimate("phrase_ablation sonnet", raw.phrase_model, len(sits) * 2, 0, "phrase", budget)
        if a.estimate_only:
            return
        try:
            budget.authorise(est)
        except BudgetExceeded as e:
            raise SystemExit(f"refused: {e}")
        model = GuardedModel(raw, budget, Cache(enabled=not a.no_cache))
        srows, sonnet = [], {}
        for s in sits:
            try:
                reply, path = sonnet_arm(s, model)
            except BudgetExceeded as e:
                print("STOPPED:", e)
                break
            sonnet[s["id"]] = reply
            srows.append({"id": s["id"], "reply": reply, "path": path,
                          "checks": checks(reply, s["options"], s["conversation_language"])})
        result["sonnet"] = {"scores": score(srows), "rows": srows, "cost": budget.report(est)}
    (OUT / f"results_{a.arm}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "review_sheet.md").write_text(review_sheet(rows, sonnet, digest), encoding="utf-8")
    print(json.dumps({"situations": digest, "template": result["scores"],
                      "sonnet": result.get("sonnet", {}).get("scores")}, indent=2))


if __name__ == "__main__":
    main()
