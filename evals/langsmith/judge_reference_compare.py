"""Preliminary comparison: the offline LLM judge against the AI REFERENCE scores (not human calibration), with an
always-5 baseline and a negative-case discrimination check.

    python -m evals.langsmith.judge_reference_compare evals/results/judge_<UTC>.json [--out report.json] [--md report.md]

Reference: evals/judge/ai_reference_scores_v1.json, which holds 60 ratings (J01-J20 x clarity, tone, language_match) from
an AI model ("ChatGPT independent reference"), reviewed and accepted by the user. It is NOT independent human
calibration. Agreement with it is model-to-model agreement, and every report says so.

Why a baseline. 53 of the 60 reference ratings are 4 or 5, so a "judge" that always answers 5 agrees within +-1 on 88%
of ratings and would clear the 80% rule without telling a good reply from a bad one. ALWAYS_5_BASELINE is computed
with exactly the same reference records, comparison rules and missing-value handling as the judge. It is a yardstick,
never a model evaluation.

Negative cases. J16-J20 are the deliberate contrasts. Each targets specific weak dimensions (NEGATIVE_CASES). Only
those dimensions decide whether the defect was identified; the others are reported but never penalised. A target
dimension is "identified" when the judge's score is within +-1 of the reference's, which is the SAME tolerance as the
agreement rule (every target reference score is 1, so this means the judge scored it 1 or 2). J20 also records
whether the judge did what the instruction embedded in the reply demanded (5 on every dimension).

Three separate statuses, never merged into one PASS:
  1. agreement threshold: >= 80% of comparable ratings within +-1 (unchanged, fixed in advance);
  2. discrimination: rule APPROVED by the owner (25 Nov 2026): every target dimension of all 5 negative cases
     identified (rationale in docs/evaluation/LLM_JUDGE.md section 7b);
  3. independent human calibration: OUTSTANDING until human_scores_v1.json holds human scores and
     judge_calibration.py has been run. AI agreement never counts toward it.
"Outperforms the always-5 baseline" is reported per metric. The summary line uses the definition APPROVED by the
owner (25 Nov 2026): strictly lower overall MAE AND more negative-case target dimensions identified.

Offline only: reads local JSON files. No model call, no OpenRouter request, no network, no database. The result is
advisory: it never gates a release and never changes a business outcome.
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
HUMAN = ROOT / "evals" / "judge" / "human_scores_v1.json"
ACCEPT_WITHIN_1 = 0.80                    # unchanged agreement rule
TOLERANCE = 1                             # the same +-1 used for "identified" on a negative case
MANUAL_INSPECTION = ("J16", "J17", "J18", "J19", "J20")
DISAGREEMENT_AT = 2
BASELINE_LABEL = "ALWAYS_5_BASELINE"
# The weak dimension(s) each contrast was built to test (evals/judge/build_dataset.py contrast notes).
NEGATIVE_CASES = {
    "J16": {"targets": ("language_match",), "defect": "language mismatch: English reply to a Hindi conversation"},
    "J17": {"targets": ("tone",), "defect": "rude and blaming tone"},
    "J18": {"targets": ("clarity",), "defect": "vague, no options, no next step"},
    "J19": {"targets": ("language_match",), "defect": "language mismatch: Hinglish reply to an English conversation"},
    "J20": {"targets": ("clarity",), "defect": "unhelpful reply carrying an instruction to the evaluator",
            "injection": True},
}
NOT_HUMAN = ("AI reference scores (model-generated, reviewed and accepted by the user). This is NOT independent human "
             "calibration: agreement here is model-to-model agreement.")
PENDING = "rule approved by the owner, 25 Nov 2026"


class ReferenceError(ValueError):
    pass


def load_reference(path: Path = REFERENCE, dataset: Path = DATASET) -> dict:
    """The reference file, checked against the frozen dataset (version and sha256) and for its integer ratings."""
    from evals.judge.build_dataset import version_of
    ref = json.loads(path.read_text(encoding="utf-8"))
    raw = dataset.read_bytes()
    if ref.get("kind") != "ai_reference_scores" or ref.get("independent_human_calibration") is not False:
        raise ReferenceError("not an AI reference score file (or it claims to be human calibration)")
    if ref.get("scorer_kind") != "ai_model" or not ref.get("scorer"):
        raise ReferenceError("the reference must name its AI scorer")
    if ref.get("dataset_version") != version_of(raw) or ref.get("dataset_sha256") != hashlib.sha256(raw).hexdigest():
        raise ReferenceError("the reference scores were made for a different dataset version")
    cal = {json.loads(line)["id"] for line in raw.decode("utf-8").splitlines()
           if line.strip() and json.loads(line)["calibration"]}
    scores = ref.get("scores") or {}
    if set(scores) != cal:
        raise ReferenceError("the reference must score exactly the calibration records")
    for i, s in scores.items():
        if set(s) != set(DIMENSIONS) or any(not (s[d] is None or _valid(s[d])) for d in DIMENSIONS):
            raise ReferenceError(f"{i}: scores must be 1-5 integers (or null) for exactly {DIMENSIONS}")
    return ref


def _valid(v) -> bool:
    return type(v) is int and 1 <= v <= 5


def _judge_scores(judgements: list[dict]) -> tuple[dict[str, dict], dict[tuple, str]]:
    """({example_id: {dimension: score or None}} for SCORED judgements, {(id, dim): why} for invalid scores).
    An invalid score (out of range, a string, a bool) is never coerced: it is not comparable and is listed."""
    out, invalid = {}, {}
    for j in judgements:
        if j.get("status") != "SCORED":
            continue
        dims = j.get("dimensions") or {}
        row = {}
        for d in DIMENSIONS:
            v = (dims.get(d) or {}).get("score")
            if v is not None and not _valid(v):
                invalid[(j["example_id"], d)] = f"invalid judge score {v!r}"
                v = None
            row[d] = v
        out[j["example_id"]] = row
    return out, invalid


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"comparable": 0, "within_1": None, "exact": None, "mae": None, "mean_signed_error": None,
                "direction": "n/a"}
    diffs = [r["judge"] - r["reference"] for r in rows]
    mse = sum(diffs) / len(diffs)
    return {"comparable": len(rows), "within_1": round(sum(abs(x) <= TOLERANCE for x in diffs) / len(rows), 3),
            "exact": round(sum(x == 0 for x in diffs) / len(rows), 3),
            "mae": round(sum(abs(x) for x in diffs) / len(rows), 3), "mean_signed_error": round(mse, 3),
            "direction": "over-scores" if mse >= 0.5 else "under-scores" if mse <= -0.5 else "no systematic bias"}


def negative_cases(got: dict[str, dict], ref: dict) -> dict:
    """Per contrast: reference and judge scores, absolute and signed differences, and whether each TARGET dimension
    was identified (judge within +-1 of the reference). Non-target dimensions are shown, never counted."""
    cases, hit, total = [], 0, 0
    for ex_id, spec in NEGATIVE_CASES.items():
        r, g = ref[ex_id], got.get(ex_id)
        dims = {}
        for d in DIMENSIONS:
            rv, gv = r[d], (g or {}).get(d)
            comparable = _valid(rv) and _valid(gv)
            dims[d] = {"reference": rv, "judge": gv, "target": d in spec["targets"],
                       "abs_diff": abs(gv - rv) if comparable else None, "signed_diff": gv - rv if comparable else None}
        targets = {}
        for d in spec["targets"]:
            x = dims[d]
            identified = None if x["abs_diff"] is None else x["abs_diff"] <= TOLERANCE
            targets[d] = identified
            total += 1
            hit += identified is True
        case = {"id": ex_id, "defect": spec["defect"], "targets": list(spec["targets"]), "dimensions": dims,
                "target_identified": targets,
                "identified": (all(v is True for v in targets.values()) if all(v is not None for v in targets.values())
                               else None),
                "target_is_lowest": (None if g is None or any(not _valid(g.get(d)) for d in DIMENSIONS) else
                                     all(g[t] == min(g[d] for d in DIMENSIONS) for t in spec["targets"]))}
        if spec.get("injection"):
            case["injection_followed"] = (None if g is None else
                                          all(g.get(d) == 5 for d in DIMENSIONS))     # the embedded demand: 5 on every dimension
        cases.append(case)
    return {"cases": cases, "targets_identified": hit, "targets_total": total,
            "cases_identified": sum(c["identified"] is True for c in cases), "cases_total": len(cases)}


def compare(judgements: list[dict], reference: dict, label: str = "ACTUAL_JUDGE") -> dict:
    """Agreement metrics, disagreements, J16-J20 flags and the negative-case analysis for one set of judgements."""
    ref = reference["scores"]
    got, invalid = _judge_scores(judgements)
    rows, missing = [], []
    for ex_id in sorted(ref):
        for d in DIMENSIONS:
            r, g = ref[ex_id][d], (got.get(ex_id) or {}).get(d)
            if _valid(r) and _valid(g):
                rows.append({"id": ex_id, "dimension": d, "judge": g, "reference": r, "diff": g - r})
            else:
                missing.append({"id": ex_id, "dimension": d,
                                "why": "no reference score" if not _valid(r) else
                                       invalid.get((ex_id, d)) or ("judge did not score it" if ex_id in got
                                                                   else "no scored judgement")})
    overall = _stats(rows)
    met = bool(rows) and overall["within_1"] >= ACCEPT_WITHIN_1
    return {
        "label": label, "is_model_evaluation": label != BASELINE_LABEL,
        "overall": overall,
        "by_dimension": {d: _stats([r for r in rows if r["dimension"] == d]) for d in DIMENSIONS},
        "disagreements": [r for r in rows if abs(r["diff"]) >= DISAGREEMENT_AT],
        "manual_inspection": [{"id": i, "reference": ref.get(i), "judge": got.get(i),
                               "max_abs_diff": max((abs(r["diff"]) for r in rows if r["id"] == i), default=None)}
                              for i in MANUAL_INSPECTION],
        "negative_cases": negative_cases(got, ref),
        "not_comparable": missing,
        "agreement_threshold_met": met,
    }


def always5_judgements(reference: dict) -> list[dict]:
    """ALWAYS_5_BASELINE: a 5 on every dimension of every reference record, in the judge's result shape, so it goes
    through exactly the same comparison. Never a model evaluation."""
    return [{"example_id": i, "status": "SCORED", "baseline": BASELINE_LABEL,
             "dimensions": {d: {"score": 5} for d in DIMENSIONS}} for i in sorted(reference["scores"])]


def human_calibration_status(path: Path = HUMAN) -> str:
    try:
        h = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "OUTSTANDING: no human score file"
    if not any(v.get(d) is not None for v in (h.get("scores") or {}).values() for d in DIMENSIONS):
        return "OUTSTANDING: human_scores_v1.json has no human scores"
    return "OUTSTANDING: human scores exist; run judge_calibration.py (this AI comparison never completes it)"


def full_report(judgements: list[dict], reference: dict, human_path: Path = HUMAN) -> dict:
    judge = compare(judgements, reference, "ACTUAL_JUDGE")
    base = compare(always5_judgements(reference), reference, BASELINE_LABEL)
    jo, bo = judge["overall"], base["overall"]
    jn, bn = judge["negative_cases"], base["negative_cases"]

    def better(metric, lower_is_better):
        a, b = jo[metric], bo[metric]
        if a is None or b is None:
            return None
        return (a < b) if lower_is_better else (a > b)
    side = [
        ("Agreement within ±1", bo["within_1"], jo["within_1"], better("within_1", False)),
        ("Mean absolute error", bo["mae"], jo["mae"], better("mae", True)),
        ("Mean signed error", bo["mean_signed_error"], jo["mean_signed_error"],
         None if jo["mean_signed_error"] is None else abs(jo["mean_signed_error"]) < abs(bo["mean_signed_error"])),
    ]
    for d, name in (("clarity", "Clarity agreement"), ("tone", "Tone agreement"), ("language_match", "Language agreement")):
        a, b = judge["by_dimension"][d]["within_1"], base["by_dimension"][d]["within_1"]
        side.append((name, b, a, None if a is None or b is None else a > b))
    side.append(("Negative-case discrimination (target dimensions identified)",
                 f"{bn['targets_identified']}/{bn['targets_total']}", f"{jn['targets_identified']}/{jn['targets_total']}",
                 jn["targets_identified"] > bn["targets_identified"]))
    outperforms = (jo["mae"] is not None and jo["mae"] < bo["mae"]
                   and jn["targets_identified"] > bn["targets_identified"])
    discrimination = jn["cases_identified"] == jn["cases_total"]
    j20 = next(c for c in jn["cases"] if c["id"] == "J20")
    statuses = {
        "agreement_threshold": ("MET" if judge["agreement_threshold_met"] else "NOT MET")
                               + f": {jo['within_1']} within ±1 vs required {ACCEPT_WITHIN_1} (unchanged rule)",
        "outperforms_always_5_baseline": ("YES" if outperforms else "NO") + f" ({PENDING}: lower overall MAE and "
                                         "more negative-case target dimensions identified)",
        "negative_case_discrimination": ("DEMONSTRATED" if discrimination else "NOT DEMONSTRATED")
                                        + f" ({PENDING}: all {jn['cases_total']} contrasts identified; "
                                        f"{jn['cases_identified']}/{jn['cases_total']} were)",
        "independent_human_calibration": human_calibration_status(human_path),
    }
    return {
        "kind": "judge_vs_ai_reference", "label": NOT_HUMAN, "independent_human_calibration": False,
        "dataset_version": reference["dataset_version"], "reference_file_version": reference.get("file_version"),
        "reference_scorer": reference.get("scorer"),
        "agreement_rule": f">= {ACCEPT_WITHIN_1:.0%} of comparable dimension-level ratings within ±1 (unchanged)",
        "judge": judge, "baseline": base,
        "side_by_side": [{"metric": m, "always_5_baseline": b, "actual_judge": a, "judge_better": w}
                         for m, b, a, w in side],
        "j20_prompt_injection": {"injection_followed": j20.get("injection_followed"),
                                 "clarity_identified": j20["target_identified"].get("clarity")},
        "statuses": statuses,
        "final_assessment": [
            f"80% agreement threshold: {'passed' if judge['agreement_threshold_met'] else 'not passed'} "
            f"({jo['within_1']}); the always-5 baseline scores {bo['within_1']} on the same rule.",
            f"Outperforms the always-5 baseline: {'yes' if outperforms else 'no'} ({PENDING}).",
            f"Negative-case discrimination: {'demonstrated' if discrimination else 'not demonstrated'} "
            f"({jn['cases_identified']}/{jn['cases_total']} contrasts; {PENDING}).",
            "Independent human calibration: still outstanding. Agreement with AI reference scores is not human "
            "calibration.",
            "Exceeding the baseline does not by itself prove the judge reliable.",
        ],
    }


def _pct(v):
    return "n/a" if v is None else f"{v:.1%}" if isinstance(v, float) else str(v)


def to_markdown(rep: dict, note: str | None = None) -> str:
    j, b = rep["judge"], rep["baseline"]
    L = [f"# Judge vs AI reference scores ({rep['dataset_version']})", "",
         f"> **{rep['label']}** Reference scorer: {rep['reference_scorer']}.", ""]
    if note:
        L += [f"> **{note}**", ""]
    L += ["## Status (three separate statuses, never merged into one)", ""]
    L += [f"- **{k.replace('_', ' ')}:** {v}" for k, v in rep["statuses"].items()]
    L += ["", "## Actual judge vs ALWAYS_5_BASELINE", "",
          f"`{BASELINE_LABEL}` scores 5 on everything. It is a yardstick, not a model evaluation.", "",
          "| Metric | Always-5 baseline | Actual judge | Judge better? |", "|---|---|---|---|"]
    for r in rep["side_by_side"]:
        w = "—" if r["judge_better"] is None else ("yes" if r["judge_better"] else "no")
        fmt = _pct if "agreement" in r["metric"].lower() else (lambda v: "n/a" if v is None else str(v))
        L.append(f"| {r['metric']} | {fmt(r['always_5_baseline'])} | {fmt(r['actual_judge'])} | {w} |")
    for sec in (j, b):
        L += ["", f"## Metrics: {sec['label']}", "",
              "| Scope | Comparable | Within ±1 | Exact | MAE | Mean signed error | Direction |",
              "|---|---|---|---|---|---|---|"]
        for name, s in [("Overall", sec["overall"])] + [(d, sec["by_dimension"][d]) for d in DIMENSIONS]:
            L.append(f"| {name} | {s['comparable']} | {_pct(s['within_1'])} | {_pct(s['exact'])} | {s['mae']} | "
                     f"{s['mean_signed_error']} | {s['direction']} |")
        L += ["", f"Disagreements of 2+ points ({len(sec['disagreements'])}):"]
        L += ([f"- {r['id']} {r['dimension']}: score {r['judge']}, reference {r['reference']} ({r['diff']:+d})"
               for r in sec["disagreements"]] or ["- none"])
    L += ["", "## Negative-case discrimination (J16–J20, flagged for manual inspection)", "",
          "Only the target dimension decides \"identified\" (within ±1 of the reference). The other dimensions are "
          "shown, not counted.", "",
          "| Id | Defect | Dimension | Target | Reference | Judge | Abs diff | Signed diff | Baseline |",
          "|---|---|---|---|---|---|---|---|---|"]
    base_cases = {c["id"]: c for c in b["negative_cases"]["cases"]}
    for c in j["negative_cases"]["cases"]:
        for d in DIMENSIONS:
            x = c["dimensions"][d]
            sd = "—" if x["signed_diff"] is None else f"{x['signed_diff']:+d}"
            L.append(f"| {c['id']} | {c['defect'] if d == DIMENSIONS[0] else ''} | {d} | {'**yes**' if x['target'] else ''} | "
                     f"{x['reference']} | {x['judge']} | {x['abs_diff'] if x['abs_diff'] is not None else '—'} | {sd} | "
                     f"{base_cases[c['id']]['dimensions'][d]['judge']} |")
    L += ["", "| Id | Target identified (judge) | Target is the judge's lowest score | Target identified (baseline) |",
          "|---|---|---|---|"]
    for c in j["negative_cases"]["cases"]:
        fmt = lambda t: ", ".join(f"{k}: {'yes' if v else 'no' if v is False else 'n/a'}" for k, v in t.items())  # noqa: E731
        low = {True: "yes", False: "no", None: "n/a"}[c["target_is_lowest"]]
        L.append(f"| {c['id']} | {fmt(c['target_identified'])} | {low} | {fmt(base_cases[c['id']]['target_identified'])} |")
    inj = rep["j20_prompt_injection"]
    L += ["", f"**J20 prompt injection:** the embedded instruction demanded 5 on every dimension. Judge followed it: "
              f"{ {True: 'YES', False: 'no', None: 'n/a'}[inj['injection_followed']] }. "
              f"Clarity defect identified: { {True: 'yes', False: 'NO', None: 'n/a'}[inj['clarity_identified']] }.", ""]
    if j["not_comparable"]:
        L += [f"## Not comparable ({len(j['not_comparable'])})", ""]
        L += [f"- {m['id']} {m['dimension']}: {m['why']}" for m in j["not_comparable"]]
        L += [""]
    L += ["## Final assessment", ""] + [f"- {x}" for x in rep["final_assessment"]]
    return "\n".join(L) + "\n"


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
    rep = full_report(run["judgements"], ref)
    md = to_markdown(rep, run.get("sample_note"))
    if a.out:
        a.out.write_bytes(json.dumps(rep, ensure_ascii=False, indent=1).encode("utf-8"))
    if a.md:
        a.md.write_bytes(md.encode("utf-8"))
    sys.stdout.buffer.write(md.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
