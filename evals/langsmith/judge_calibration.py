"""Offline LLM judge (Part A): compare the judge with the operator's own scores on the 20 calibration replies.

    python -m evals.langsmith.judge_calibration evals/results/judge_<ts>.json     # after the approved paid run

Inputs: the run's results file and evals/judge/human_scores_v1.json (the operator fills it from
evals/judge/calibration_sheet_v1.md; a null human score means "not enough to judge" and is left out of every number).

Acceptance (fixed in advance; changing it is an owner decision, never done silently):
  at least ACCEPT_WITHIN_1 (80%) of all paired scores are within ±1 of the human score.
Reported as well: mean absolute error, agreement and bias (judge minus human; + means the judge over-scores) per
dimension and per language, the contrast replies on their own, and every pair that is 2+ points apart.
A failed calibration means the judge stays EXPERIMENTAL; its scores are advisory either way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.langsmith.llm_judge import DIMENSIONS  # noqa: E402

HUMAN = ROOT / "evals" / "judge" / "human_scores_v1.json"
ACCEPT_WITHIN_1 = 0.80


def pairs(judgements: list[dict], human: dict, records: dict) -> list[dict]:
    out = []
    by_id = {j["example_id"]: j for j in judgements}
    for ex_id, hs in human.items():
        j = by_id.get(ex_id)
        if not j or j.get("status") != "SCORED":
            continue
        for d in DIMENSIONS:
            h, g = hs.get(d), (j["dimensions"].get(d) or {}).get("score")
            if h is None or g is None:
                continue
            out.append({"id": ex_id, "dimension": d, "human": int(h), "judge": int(g),
                        "language": records.get(ex_id, {}).get("language"),
                        "source": records.get(ex_id, {}).get("source")})
    return out


def _stats(ps: list[dict]) -> dict:
    if not ps:
        return {"pairs": 0}
    diffs = [p["judge"] - p["human"] for p in ps]
    return {"pairs": len(ps), "within_1": round(sum(abs(x) <= 1 for x in diffs) / len(ps), 3),
            "exact": round(sum(x == 0 for x in diffs) / len(ps), 3),
            "mae": round(sum(abs(x) for x in diffs) / len(ps), 3), "bias": round(sum(diffs) / len(ps), 3)}


def report(judgements: list[dict], human: dict, records: dict) -> dict:
    ps = pairs(judgements, human, records)
    overall = _stats(ps)
    rep = {"acceptance": f"within ±1 >= {ACCEPT_WITHIN_1:.0%} of all paired scores",
           "overall": overall,
           "by_dimension": {d: _stats([p for p in ps if p["dimension"] == d]) for d in DIMENSIONS},
           "by_language": {lg: _stats([p for p in ps if p["language"] == lg]) for lg in sorted({p["language"] for p in ps})},
           "contrast_only": _stats([p for p in ps if p["source"] == "contrast"]),
           "disagreements_2plus": [p for p in ps if abs(p["judge"] - p["human"]) >= 2],
           "missing": sorted(set(human) - {p["id"] for p in ps})}
    passed = overall.get("pairs", 0) > 0 and overall["within_1"] >= ACCEPT_WITHIN_1
    rep["verdict"] = "ACCEPTED (advisory scores)" if passed else "EXPERIMENTAL (calibration not met)"
    rep["systematic"] = {d: ("over-scores" if s.get("bias", 0) >= 0.5 else "under-scores" if s.get("bias", 0) <= -0.5
                             else "no systematic bias") for d, s in rep["by_dimension"].items() if s.get("pairs")}
    return rep


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit(__doc__)
    from evals.langsmith.judge_run import load_dataset
    run = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    hum = json.loads(HUMAN.read_text(encoding="utf-8"))
    recs, version = load_dataset()
    if hum.get("dataset_version") != version or run.get("dataset_version") != version:
        raise SystemExit(f"REFUSED: the run, the human scores and the dataset must share one version ({version})")
    scores = hum.get("scores") or {}
    if not any(v.get(d) is not None for v in scores.values() for d in DIMENSIONS):
        raise SystemExit("human_scores_v1.json has no scores yet: fill it from calibration_sheet_v1.md first")
    print(json.dumps(report(run["judgements"], scores, {r["id"]: r for r in recs}), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
