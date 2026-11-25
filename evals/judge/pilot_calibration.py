"""Human calibration of the ONLINE pilot judge (Full Activation, item B). Two steps, both run by the operator:

    # 1. export a scoring sheet from the pilot database (read-only; masked text only; synthetic pilot)
    python -m evals.judge.pilot_calibration export --dsn "<read-only DSN>" --limit 40 --out <folder>
    # 2. HUMAN reviewers fill the human_* columns, independently, without looking at the judge's scores first
    #    (the sheet puts the judge columns last and the instructions say to hide them while scoring)
    python -m evals.judge.pilot_calibration report <folder>/pilot_calibration_sheet.csv

The sheet has one row per sampled judged pilot turn (status SCORED), (a deterministic pseudo-random sample, md5 of turn_ref), plus the two offline
false-positive cases J13 (correct Hinglish scored as a language mismatch) and J14 (an actionable no-slot reply scored
as vague) for an explicit false-positive review. Each row: the driver's request (masked body), the assistant's reply,
blank human_clarity / human_tone / human_language_match (1-5, or blank = not enough to judge), human_rationale,
human_evidence (a quote from the reply), reviewer_id, reviewer_kind, and then the judge's scores and rationale.

The report REFUSES to produce an outcome unless every scored row says reviewer_kind=human and has a reviewer_id:
model-generated reference scores (ai_reference_scores_v1.json) and judge self-evaluation never count as human
calibration. Acceptance is the existing rule (judge_calibration.ACCEPT_WITHIN_1: at least 80% of paired scores within
+-1); the report also gives MAE, bias per dimension and language, every 2+ point disagreement, and the J13 / J14
outcome. Until a report says CALIBRATED, every judge score stays advisory (agent_turn_judgements.calibration_status).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from setuhaul.agent.judge_core import DIMENSIONS  # noqa: E402

DATASET = ROOT / "evals" / "judge" / "dataset_v1.jsonl"
FALSE_POSITIVE_CASES = {"J13": "language_match", "J14": "clarity"}
COLUMNS = (["row_id", "source", "language", "driver_request", "assistant_reply"]
           + [f"human_{d}" for d in DIMENSIONS] + ["human_rationale", "human_evidence", "reviewer_id", "reviewer_kind"]
           + [f"judge_{d}" for d in DIMENSIONS] + ["judge_rationale", "false_positive_review"])
INSTRUCTIONS = """Human calibration sheet: online LLM judge (advisory), synthetic pilot

1. Each reviewer works alone. Hide the judge_* columns while scoring (they are last on purpose).
2. Score clarity, tone and language_match 1-5 exactly as the judge rubric says (LLM_JUDGE.md section 3);
   leave a score blank only when the reply gives too little to judge that dimension.
3. human_rationale: one sentence. human_evidence: a few words copied exactly from assistant_reply.
4. reviewer_id: your initials or staff id; reviewer_kind: human. Never paste model output into the human columns.
5. Rows with false_positive_review set: say in human_rationale whether the judge's low score was a false positive.
"""

SAMPLE_SQL = """
WITH j AS (
  SELECT j.turn_ref, j.driver_message_id, j.clarity, j.tone, j.language_match, j.details
    FROM setuhaul.agent_turn_judgements j
   WHERE j.status = 'SCORED' AND j.driver_message_id IS NOT NULL)
SELECT j.turn_ref, m.body_masked AS driver_request,
       (SELECT a.body_masked FROM setuhaul.chat_messages a
         WHERE a.thread_id = m.thread_id AND a.sender_type = 'AGENT' AND a.message_id > m.message_id
         ORDER BY a.message_id LIMIT 1) AS assistant_reply,
       j.clarity, j.tone, j.language_match, j.details
  FROM j JOIN setuhaul.chat_messages m ON m.message_id = j.driver_message_id AND m.sender_type = 'DRIVER'
 ORDER BY md5(j.turn_ref::text)
 LIMIT %s
"""


def _lang(text: str) -> str:
    from setuhaul.agent.replies import detect_language
    try:
        return detect_language(text or "")
    except Exception:                                               # noqa: BLE001
        return "en"


def export(dsn: str, limit: int, out: Path, conn=None) -> Path:
    """Read-only export. `conn` is injected by tests."""
    import psycopg
    out.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or psycopg.connect(dsn)
    try:
        if own:
            conn.execute("SET default_transaction_read_only = on")
        rows = conn.execute(SAMPLE_SQL, (limit,)).fetchall()
    finally:
        if own:
            conn.close()
    recs = []
    for i, (ref, req, reply, c, t, lm, details) in enumerate(rows, 1):
        if not reply:
            continue
        det = details if isinstance(details, dict) else json.loads(details or "{}")
        recs.append({"row_id": f"P{i:03d}", "source": f"pilot:{ref}", "language": _lang(reply),
                     "driver_request": req, "assistant_reply": reply, "judge_clarity": c, "judge_tone": t,
                     "judge_language_match": lm,
                     "judge_rationale": " | ".join(f"{d}: {(det.get(d) or {}).get('rationale', '')}" for d in DIMENSIONS),
                     "false_positive_review": ""})
    ds = {r["id"]: r for r in (json.loads(x) for x in DATASET.read_text(encoding="utf-8").splitlines() if x.strip())}
    for rid, dim in FALSE_POSITIVE_CASES.items():
        r = ds[rid]
        recs.append({"row_id": rid, "source": "offline:dataset_v1", "language": r["language"],
                     "driver_request": " / ".join(r["conversation"]), "assistant_reply": r["reply"],
                     "false_positive_review": f"judge run 2 scored {dim} low; is that a false positive?"})
    path = out / "pilot_calibration_sheet.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in recs:
            w.writerow({k: r.get(k, "") for k in COLUMNS})
    (out / "INSTRUCTIONS.txt").write_text(INSTRUCTIONS, encoding="utf-8")
    return path


def _score(v):
    v = (v or "").strip()
    if not v:
        return None
    n = int(v)
    if not 1 <= n <= 5:
        raise ValueError(f"score {n} outside 1-5")
    return n


def report(sheet: Path) -> dict:
    from evals.langsmith.judge_calibration import ACCEPT_WITHIN_1
    rows = list(csv.DictReader(sheet.open(encoding="utf-8")))
    scored = [r for r in rows if any((r.get(f"human_{d}") or "").strip() for d in DIMENSIONS)]
    if not scored:
        return {"outcome": "NOT_STARTED", "reason": "no human scores in the sheet"}
    bad = [r["row_id"] for r in scored if (r.get("reviewer_kind") or "").strip().lower() != "human"
           or not (r.get("reviewer_id") or "").strip()]
    if bad:
        return {"outcome": "REFUSED", "reason": "rows without reviewer_kind=human and a reviewer_id", "rows": bad}
    pairs, disagreements = [], []
    for r in scored:
        for d in DIMENSIONS:
            h, j = _score(r.get(f"human_{d}")), _score(r.get(f"judge_{d}"))
            if h is None or j is None:
                continue
            pairs.append({"row": r["row_id"], "dim": d, "lang": r["language"], "human": h, "judge": j})
            if abs(h - j) >= 2:
                disagreements.append({"row": r["row_id"], "dim": d, "human": h, "judge": j})

    def stats(ps):
        if not ps:
            return {"pairs": 0}
        return {"pairs": len(ps), "within_1": round(sum(abs(p["judge"] - p["human"]) <= 1 for p in ps) / len(ps), 3),
                "mae": round(sum(abs(p["judge"] - p["human"]) for p in ps) / len(ps), 3),
                "bias": round(sum(p["judge"] - p["human"] for p in ps) / len(ps), 3)}
    fp = {}
    for rid, dim in FALSE_POSITIVE_CASES.items():
        r = next((x for x in rows if x["row_id"] == rid), None)
        fp[rid] = {"dimension": dim, "human_score": _score(r.get(f"human_{dim}")) if r else None,
                   "human_note": (r or {}).get("human_rationale", "")}
    overall = stats(pairs)
    passed = overall.get("pairs", 0) > 0 and overall["within_1"] >= ACCEPT_WITHIN_1
    return {"outcome": "CALIBRATED" if passed else "NOT_CALIBRATED",
            "acceptance": f"within +-1 >= {ACCEPT_WITHIN_1:.0%} of paired scores (human vs judge)",
            "reviewers": sorted({r["reviewer_id"].strip() for r in scored}),
            "overall": overall, "by_dimension": {d: stats([p for p in pairs if p["dim"] == d]) for d in DIMENSIONS},
            "by_language": {lg: stats([p for p in pairs if p["lang"] == lg]) for lg in sorted({p["lang"] for p in pairs})},
            "disagreements_2plus": disagreements, "false_positive_review": fp,
            "note": "Advisory either way; CALIBRATED only changes calibration_status after the owner accepts this report."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pilot_calibration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--dsn", required=True)
    e.add_argument("--limit", type=int, default=40)
    e.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("report")
    r.add_argument("sheet", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "export":
        print(export(a.dsn, max(1, min(200, a.limit)), a.out))
        return 0
    rep = report(a.sheet)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep["outcome"] in ("CALIBRATED", "NOT_CALIBRATED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
