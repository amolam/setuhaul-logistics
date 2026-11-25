"""Preliminary comparison: the offline LLM judge against the AI REFERENCE scores (not human calibration).

    python -m evals.langsmith.judge_reference_compare evals/results/judge_<UTC>.json [--out report.json] [--md report.md]

Reference: evals/judge/ai_reference_scores_v1.json, which holds 60 ratings (J01-J20 x clarity, tone, language_match) from
an AI model ("ChatGPT independent reference"), reviewed and accepted by the user. It is NOT independent human
calibration. Agreement with it is model-to-model agreement. It can show gross disagreement or systematic bias, but
it cannot establish that the judge matches human drivers or native-speaker reviewers. Every report says so, and
`independent_human_calibration` is always false. Human calibration (judge_calibration.py, human_scores_v1.json) stays
a separate, still-open step.

Computed (a rating is comparable when both the judge and the reference have an integer score):
  overall agreement within +-1 and mean absolute error; the same per dimension (clarity, tone and language_match
  always reported separately); mean signed error (judge minus reference: + means the judge over-scores);
  every individual disagreement of 2 or more points; the five contrast replies J16-J20 are ALWAYS listed for manual
  inspection, whatever their agreement.
Preliminary acceptance: at least 80% of all comparable dimension-level ratings within +-1. This is fixed here and is not
changed silently. The result is advisory: it never gates a release and never changes a business outcome.

Offline only: reads two local JSON files. No model call, no OpenRouter request, no network, no database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DIMENSIONS = ("clarity", "tone", "language_match")
REFERENCE = ROOT / "evals" / "judge" / "ai_reference_scores_v1.json"
DATASET = ROOT / "evals" / "judge" / "dataset_v1.jsonl"
ACCEPT_WITHIN_1 = 0.80
MANUAL_INSPECTION = ("J16", "J17", "J18", "J19", "J20")
DISAGREEMENT_AT = 2
NOT_HUMAN = ("AI reference scores (model-generated, reviewed and accepted by the user). This is NOT independent human "
             "calibration: agreement here is model-to-model agreement.")


class ReferenceError(ValueError):
    pass


def load_reference(path: Path = REFERENCE, dataset: Path = DATASET) -> dict:
    """The reference file, checked against the frozen dataset (version and sha256) and for its 60 integer ratings."""
    from evals.judge.build_dataset import version_of
    ref = json.loads(path.read_text(encoding="utf-8"))
    raw = dataset.read_bytes()
    if ref.get("kind") != "ai_reference_scores" or ref.get("independent_human_calibration") is not False:
        raise ReferenceError("not an AI reference score file (or it claims to be human calibration)")
    if ref.get("dataset_version") != version_of(raw) or ref.get("dataset_sha256") != hashlib.sha256(raw).hexdigest():
        raise ReferenceError("the reference scores were made for a different dataset version")
    cal = {json.loads(line)["id"] for line in raw.decode("utf-8").splitlines()
           if line.strip() and json.loads(line)["calibration"]}
    scores = ref.get("scores") or {}
    if set(scores) != cal:
        raise ReferenceError("the reference must score exactly the calibration records")
    for i, s in scores.items():
        if set(s) != set(DIMENSIONS) or any(not (s[d] is None or (type(s[d]) is int and 1 <= s[d] <= 5))
                                            for d in DIMENSIONS):
            raise ReferenceError(f"{i}: scores must be 1-5 integers (or null) for exactly {DIMENSIONS}")
    return ref


def _judge_scores(judgements: list[dict]) -> dict[str, dict]:
    """{example_id: {dimension: score or None}}, only for SCORED judgements (anything else is not comparable)."""
    out = {}
    for j in judgements:
        if j.get("status") != "SCORED":
            continue
        dims = j.get("dimensions") or {}
        out[j["example_id"]] = {d: (dims.get(d) or {}).get("score") for d in DIMENSIONS}
    return out


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"comparable": 0, "within_1": None, "mae": None, "mean_signed_error": None, "direction": "n/a"}
    diffs = [r["judge"] - r["reference"] for r in rows]
    mse = sum(diffs) / len(diffs)
    return {"comparable": len(rows), "within_1": round(sum(abs(x) <= 1 for x in diffs) / len(rows), 3),
            "exact": round(sum(x == 0 for x in diffs) / len(rows), 3),
            "mae": round(sum(abs(x) for x in diffs) / len(rows), 3), "mean_signed_error": round(mse, 3),
            "direction": "over-scores" if mse >= 0.5 else "under-scores" if mse <= -0.5 else "no systematic bias"}


def compare(judgements: list[dict], reference: dict) -> dict:
    ref = reference["scores"]
    got = _judge_scores(judgements)
    rows, missing = [], []
    for ex_id in sorted(ref):
        for d in DIMENSIONS:
            r, g = ref[ex_id][d], (got.get(ex_id) or {}).get(d)
            if type(r) is int and type(g) is int:
                rows.append({"id": ex_id, "dimension": d, "judge": g, "reference": r, "diff": g - r})
            else:
                missing.append({"id": ex_id, "dimension": d,
                                "why": "no reference score" if type(r) is not int else
                                       "judge did not score it" if ex_id in got else "no scored judgement"})
    overall = _stats(rows)
    passed = bool(rows) and overall["within_1"] >= ACCEPT_WITHIN_1
    inspect = []
    for ex_id in MANUAL_INSPECTION:
        inspect.append({"id": ex_id, "reference": ref.get(ex_id), "judge": got.get(ex_id),
                        "max_abs_diff": max((abs(r["diff"]) for r in rows if r["id"] == ex_id), default=None)})
    return {
        "kind": "judge_vs_ai_reference", "label": NOT_HUMAN, "independent_human_calibration": False,
        "dataset_version": reference["dataset_version"], "reference_file_version": reference.get("file_version"),
        "reference_scorer": reference.get("scorer"),
        "acceptance": f"preliminary: >= {ACCEPT_WITHIN_1:.0%} of comparable dimension-level ratings within +-1",
        "overall": overall,
        "by_dimension": {d: _stats([r for r in rows if r["dimension"] == d]) for d in DIMENSIONS},
        "disagreements": [r for r in rows if abs(r["diff"]) >= DISAGREEMENT_AT],
        "manual_inspection": inspect,
        "not_comparable": missing,
        "verdict": ("PRELIMINARY PASS against the AI reference (not human calibration)" if passed else
                    "PRELIMINARY FAIL against the AI reference: the judge stays experimental"),
    }


def to_markdown(rep: dict, note: str | None = None) -> str:
    o = rep["overall"]
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"                     # noqa: E731
    lines = [f"# Judge vs AI reference scores ({rep['dataset_version']})", "",
             f"> **{rep['label']}** Reference scorer: {rep['reference_scorer']}.", ""]
    if note:
        lines += [f"> {note}", ""]
    lines += [f"**Verdict:** {rep['verdict']}  ", f"**Acceptance:** {rep['acceptance']}", "",
              "| Scope | Comparable | Within ±1 | Exact | MAE | Mean signed error | Direction |", "|---|---|---|---|---|---|---|"]
    for name, s in [("Overall", o)] + [(d, rep["by_dimension"][d]) for d in DIMENSIONS]:
        lines.append(f"| {name} | {s['comparable']} | {pct(s['within_1'])} | {pct(s.get('exact'))} | {s['mae']} | "
                     f"{s['mean_signed_error']} | {s['direction']} |")
    lines += ["", "## Disagreements of 2 or more points", ""]
    lines += ([f"- {r['id']} {r['dimension']}: judge {r['judge']}, reference {r['reference']} ({r['diff']:+d})"
               for r in rep["disagreements"]] or ["- none"])
    lines += ["", "## Flagged for manual inspection (J16–J20, the deliberate contrasts)", "",
              "| Id | Reference (clarity / tone / language) | Judge | Largest gap |", "|---|---|---|---|"]
    for m in rep["manual_inspection"]:
        fmt = lambda s: "—" if not s else " / ".join(str(s.get(d)) for d in DIMENSIONS)   # noqa: E731
        lines.append(f"| {m['id']} | {fmt(m['reference'])} | {fmt(m['judge'])} | {m['max_abs_diff']} |")
    if rep["not_comparable"]:
        lines += ["", f"## Not comparable ({len(rep['not_comparable'])})", ""]
        lines += [f"- {m['id']} {m['dimension']}: {m['why']}" for m in rep["not_comparable"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare a judge run with the AI reference scores (offline).")
    ap.add_argument("judge_results", type=Path, help="evals/results/judge_<UTC>.json from an approved run")
    ap.add_argument("--reference", type=Path, default=REFERENCE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--md", type=Path)
    a = ap.parse_args(argv)
    ref = load_reference(a.reference)
    run = json.loads(a.judge_results.read_text(encoding="utf-8"))
    if run.get("dataset_version") != ref["dataset_version"]:
        raise SystemExit(f"REFUSED: the judge run is for {run.get('dataset_version')}, the reference for "
                         f"{ref['dataset_version']}")
    rep = compare(run["judgements"], ref)
    if a.out:
        a.out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.md:
        a.md.write_text(to_markdown(rep, run.get("sample_note")), encoding="utf-8")
    print(to_markdown(rep, run.get("sample_note")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
