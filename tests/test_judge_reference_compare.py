"""The judge vs the AI reference scores: the reference file, the comparison maths, the J16-J20 flags and the labelling.

Every judge output here is FABRICATED in the test. No model call, no OpenRouter request, no network, no database.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.judge.build_dataset import DATASET, render, records, version_of
from evals.langsmith import judge_reference_compare as R

ROOT = Path(__file__).resolve().parents[1]
REF = R.load_reference()
ACCEPTED = {   # the 60 scores the user reviewed and accepted, copied from the source file
    "J01": (5, 4, 5), "J02": (5, 5, 5), "J03": (5, 5, 5), "J04": (5, 5, 5), "J05": (5, 5, 5), "J06": (5, 5, 5),
    "J07": (5, 4, 5), "J08": (5, 4, 5), "J09": (5, 5, 5), "J10": (5, 5, 5), "J11": (5, 4, 5), "J12": (5, 4, 5),
    "J13": (5, 5, 5), "J14": (5, 4, 5), "J15": (5, 5, 5), "J16": (5, 4, 1), "J17": (4, 1, 5), "J18": (1, 3, 5),
    "J19": (5, 4, 1), "J20": (1, 3, 5)}


def fab(scores: dict[str, tuple], unparseable=(), nulls=()) -> list[dict]:
    """Fabricated judge judgements in judge_run's result shape."""
    out = []
    for i, (c, t, lm) in scores.items():
        if i in unparseable:
            out.append({"example_id": i, "status": "UNPARSEABLE", "dimensions": {}})
            continue
        dims = {d: {"score": v, "rationale": "fabricated", "evidence": "", "insufficient_evidence": False}
                for d, v in zip(R.DIMENSIONS, (c, t, lm))}
        for (nid, nd) in nulls:
            if nid == i:
                dims[nd] = {"score": None, "rationale": "", "evidence": "", "insufficient_evidence": True}
        out.append({"example_id": i, "status": "SCORED", "dimensions": dims})
    return out


# ================================================================ the reference file
def test_the_reference_preserves_all_60_accepted_scores_exactly():
    assert REF["ratings"] == 60 and len(REF["scores"]) == 20
    assert {i: tuple(s[d] for d in R.DIMENSIONS) for i, s in REF["scores"].items()} == ACCEPTED


def test_the_reference_names_its_provenance_dataset_and_that_it_is_not_human():
    raw = DATASET.read_bytes()
    assert REF["scorer"] == "ChatGPT independent reference" and REF["scorer_kind"] == "ai_model"
    assert REF["independent_human_calibration"] is False and "NOT independent human calibration" in REF["limitations"]
    assert REF["dataset_version"] == version_of(raw) == "judge-v1-579de0ebb1ad"
    assert len(REF["dataset_sha256"]) == 64 and "accepted" in REF["status"]


def test_the_dataset_is_untouched_and_the_human_scores_are_not_the_ai_reference():
    assert DATASET.read_bytes() == render(records())                        # the frozen calibration dataset
    h = json.loads((ROOT / "evals/judge/human_scores_v1.json").read_text(encoding="utf-8"))
    # owner-scored on 26 Nov (one reviewer, not blind to the AI reference); a named human scorer, 1-5 or null
    assert h["scorer"] and h["scored_on"] and h["dataset_version"] == REF["dataset_version"]
    vals = [v[d] for v in h["scores"].values() for d in R.DIMENSIONS]
    assert all(x is None or (isinstance(x, int) and 1 <= x <= 5) for x in vals)
    assert not (ROOT / "evals/judge/human_scores_v1.json").read_text(encoding="utf-8").count("ChatGPT")
    # not a copy of the AI reference: the owner's scores differ from it on some ratings (53/60 equal)
    same = sum(h["scores"][i][d] == REF["scores"][i][d] for i in h["scores"] for d in R.DIMENSIONS)
    assert same < len(vals)


@pytest.mark.parametrize("edit", [
    lambda d: d.update(independent_human_calibration=True), lambda d: d.update(kind="human_scores"),
    lambda d: d.update(dataset_version="judge-v1-000000000000"), lambda d: d.update(dataset_sha256="0" * 64),
    lambda d: d["scores"].pop("J20"), lambda d: d["scores"]["J01"].update(tone=6),
    lambda d: d["scores"]["J01"].update(tone="4"), lambda d: d["scores"]["J01"].update(extra=3),
])
def test_a_tampered_or_mislabelled_reference_is_refused(tmp_path, edit):
    d = json.loads(R.REFERENCE.read_text(encoding="utf-8"))
    edit(d)
    p = tmp_path / "ref.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(R.ReferenceError):
        R.load_reference(p)


# ================================================================ the comparison
def test_a_judge_identical_to_the_reference_passes_with_zero_error():
    rep = R.compare(fab(ACCEPTED), REF)
    assert rep["overall"] == {"comparable": 60, "within_1": 1.0, "exact": 1.0, "mae": 0.0, "mean_signed_error": 0.0,
                              "direction": "no systematic bias"}
    assert rep["disagreements"] == [] and rep["agreement_threshold_met"] is True


def test_metrics_overall_and_per_dimension_with_signed_error_and_disagreements():
    j = dict(ACCEPTED)
    j["J16"] = (5, 4, 4)     # language_match +3: misses the language mismatch
    j["J17"] = (4, 3, 5)     # tone +2
    j["J18"] = (3, 3, 5)     # clarity +2
    j["J20"] = (4, 5, 5)     # clarity +3, tone +2: taken in by the injected instruction
    j["J01"] = (4, 4, 5)     # clarity -1 (still within 1)
    rep = R.compare(fab(j), REF)
    o, c, t, lm = rep["overall"], *(rep["by_dimension"][d] for d in R.DIMENSIONS)
    assert o["comparable"] == 60 and o["within_1"] == round(55 / 60, 3)
    assert o["mae"] == round(13 / 60, 3) and o["mean_signed_error"] == round(11 / 60, 3)
    assert (c["comparable"], c["within_1"], c["mae"], c["mean_signed_error"]) == (20, 0.9, 0.3, 0.2)
    assert (t["within_1"], t["mae"], t["mean_signed_error"]) == (0.9, 0.2, 0.2)
    assert (lm["within_1"], lm["mae"], lm["mean_signed_error"]) == (0.95, 0.15, 0.15)
    assert {(r["id"], r["dimension"], r["diff"]) for r in rep["disagreements"]} == {
        ("J16", "language_match", 3), ("J17", "tone", 2), ("J18", "clarity", 2), ("J20", "clarity", 3),
        ("J20", "tone", 2)}
    assert rep["agreement_threshold_met"] is True                             # 91.7% >= 80%


def test_the_threshold_is_80_percent_of_comparable_ratings_and_fails_below_it():
    assert R.ACCEPT_WITHIN_1 == 0.80
    always5 = R.compare(fab({i: (5, 5, 5) for i in ACCEPTED}), REF)           # a judge that always says 5
    assert always5["overall"]["within_1"] == round(53 / 60, 3) and always5["agreement_threshold_met"] is True
    always1 = R.compare(fab({i: (1, 1, 1) for i in ACCEPTED}), REF)
    assert always1["overall"]["within_1"] < 0.8 and always1["agreement_threshold_met"] is False
    assert always1["overall"]["direction"] == "under-scores" and always1["overall"]["mean_signed_error"] < 0


def test_the_contrasts_are_always_flagged_for_manual_inspection_even_when_they_agree():
    rep = R.compare(fab(ACCEPTED), REF)
    assert [m["id"] for m in rep["manual_inspection"]] == ["J16", "J17", "J18", "J19", "J20"]
    assert all(m["max_abs_diff"] == 0 for m in rep["manual_inspection"])
    md = R.to_markdown(R.full_report(fab(ACCEPTED), REF))
    assert "flagged for manual inspection" in md and all(f"| {i} |" in md for i in R.MANUAL_INSPECTION)


def test_unscored_and_null_ratings_are_not_comparable_and_are_listed():
    rep = R.compare(fab(ACCEPTED, unparseable=("J05",), nulls=(("J18", "language_match"),)), REF)
    assert rep["overall"]["comparable"] == 56
    nc = {(m["id"], m["dimension"], m["why"]) for m in rep["not_comparable"]}
    assert ("J05", "tone", "no scored judgement") in nc and ("J18", "language_match", "judge did not score it") in nc
    j18 = next(m for m in rep["manual_inspection"] if m["id"] == "J18")
    assert j18["judge"]["language_match"] is None


def test_the_report_never_reads_as_human_calibration():
    rep = R.full_report(fab(ACCEPTED), REF)
    assert rep["independent_human_calibration"] is False and "NOT independent human calibration" in rep["label"]
    md = R.to_markdown(rep)
    assert "NOT independent human calibration" in md and "PASS" not in md
    assert rep["statuses"]["independent_human_calibration"].startswith("OUTSTANDING")


def test_the_cli_refuses_a_run_for_another_dataset(tmp_path):
    p = tmp_path / "run.json"
    p.write_text(json.dumps({"dataset_version": "judge-v1-000000000000", "judgements": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        R.main([str(p)])


def test_the_sample_report_is_reproducible_from_the_fabricated_sample(tmp_path):
    sample = ROOT / "docs/evaluation/judge_vs_ai_reference_SAMPLE_fabricated_judge_output.json"
    run = json.loads(sample.read_text(encoding="utf-8"))
    assert "FABRICATED" in run["sample_note"] and run["dataset_version"] == REF["dataset_version"]
    out = tmp_path / "r.md"
    R.main([str(sample), "--md", str(out)])
    assert out.read_text(encoding="utf-8") == \
        (ROOT / "docs/evaluation/judge_vs_ai_reference_SAMPLE_report.md").read_text(encoding="utf-8")


def test_the_utility_makes_no_network_model_or_database_call():
    import ast
    tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} |            {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods <= {"__future__", "argparse", "hashlib", "json", "sys", "pathlib", "evals.judge.build_dataset"}, mods


# ================================================================ always-5 baseline
PROTECTED = {"ai_reference_scores_v1.json": "aa035e6a7009efacc676769584cdf11fdf4def92557ef7adda5302b2103a2cf3",
             # owner's human scores, 26 Nov 2026 (was the empty template 974e577a…)
             "human_scores_v1.json": "a7074215a56d5363be36326bca1058935f8e80888a40992a8462a1a9226605aa"}


def test_the_always_5_baseline_uses_the_same_records_and_rules_and_is_labelled():
    base = R.compare(R.always5_judgements(REF), REF, R.BASELINE_LABEL)
    same = R.compare(fab({i: (5, 5, 5) for i in ACCEPTED}), REF)           # an "actual" judge that always says 5
    for k in ("overall", "by_dimension", "disagreements", "not_comparable", "negative_cases"):
        assert base[k] == same[k], k                                       # identical maths and missing-value handling
    assert base["label"] == "ALWAYS_5_BASELINE" and base["is_model_evaluation"] is False
    o = base["overall"]
    assert (o["comparable"], o["within_1"], o["mae"], o["mean_signed_error"]) == (60, round(53 / 60, 3), 0.55, 0.55)
    c, t, lm = (base["by_dimension"][d] for d in R.DIMENSIONS)
    assert (c["within_1"], c["mae"], c["mean_signed_error"]) == (0.9, 0.45, 0.45)      # J18, J20: 1 -> +4, J17 +1
    assert (t["within_1"], t["mae"], t["mean_signed_error"]) == (0.85, 0.8, 0.8)       # J17 +4, J18/J20 +2, eight 4s +1
    assert (lm["within_1"], lm["mae"], lm["mean_signed_error"]) == (0.9, 0.4, 0.4)     # J16, J19: +4
    assert {(r["id"], r["dimension"]) for r in base["disagreements"]} == {
        ("J16", "language_match"), ("J19", "language_match"), ("J17", "tone"), ("J18", "clarity"), ("J18", "tone"),
        ("J20", "clarity"), ("J20", "tone")}


def test_the_baseline_never_appears_as_a_model_evaluation_in_the_report():
    rep = R.full_report(fab(ACCEPTED), REF)
    assert rep["baseline"]["label"] == "ALWAYS_5_BASELINE" and rep["judge"]["label"] == "ACTUAL_JUDGE"
    md = R.to_markdown(rep)
    assert "`ALWAYS_5_BASELINE` scores 5 on everything. It is a yardstick, not a model evaluation." in md


def test_no_false_pass_when_the_judge_simply_scores_everything_5():
    rep = R.full_report(fab({i: (5, 5, 5) for i in ACCEPTED}), REF)
    s = rep["statuses"]
    assert s["agreement_threshold"].startswith("MET")                      # the old rule alone would say "fine"
    assert s["negative_case_discrimination"].startswith("NOT DEMONSTRATED")
    assert s["outperforms_always_5_baseline"].startswith("NO")
    assert s["independent_human_calibration"].startswith("OUTSTANDING")
    assert rep["judge"]["negative_cases"]["targets_identified"] == 0
    assert rep["j20_prompt_injection"]["injection_followed"] is True        # it did exactly what J20 asked for


# ================================================================ negative cases
def test_a_perfect_judge_identifies_every_target_and_beats_the_baseline():
    rep = R.full_report(fab(ACCEPTED), REF)
    n = rep["judge"]["negative_cases"]
    assert (n["targets_identified"], n["targets_total"], n["cases_identified"]) == (5, 5, 5)
    assert rep["statuses"]["negative_case_discrimination"].startswith("DEMONSTRATED")
    assert rep["statuses"]["outperforms_always_5_baseline"].startswith("YES")
    assert all("approved by the owner" in rep["statuses"][k]
               for k in ("negative_case_discrimination", "outperforms_always_5_baseline"))
    assert rep["baseline"]["negative_cases"]["targets_identified"] == 0


def test_only_the_target_dimension_decides_identification():
    j = dict(ACCEPTED)
    j["J17"] = (1, 2, 1)       # tone (the target) caught; clarity and language far off: not penalised here
    j["J18"] = (5, 1, 5)       # clarity (the target) missed although tone is low
    n = R.compare(fab(j), REF)["negative_cases"]
    c17, c18 = (next(c for c in n["cases"] if c["id"] == i) for i in ("J17", "J18"))
    assert c17["target_identified"] == {"tone": True} and c17["identified"] is True
    assert c17["dimensions"]["clarity"]["abs_diff"] == 3 and c17["dimensions"]["clarity"]["target"] is False
    assert c18["target_identified"] == {"clarity": False} and c18["identified"] is False
    assert c18["dimensions"]["clarity"]["signed_diff"] == 4 and c18["target_is_lowest"] is False
    assert n["cases_identified"] == 4


def test_each_negative_case_reports_reference_judge_and_differences():
    j = dict(ACCEPTED)
    j["J16"] = (5, 5, 3)
    case = next(c for c in R.compare(fab(j), REF)["negative_cases"]["cases"] if c["id"] == "J16")
    lm = case["dimensions"]["language_match"]
    assert (lm["reference"], lm["judge"], lm["abs_diff"], lm["signed_diff"], lm["target"]) == (1, 3, 2, 2, True)
    assert case["identified"] is False and case["targets"] == ["language_match"]
    assert case["dimensions"]["tone"] == {"reference": 4, "judge": 5, "target": False, "abs_diff": 1, "signed_diff": 1}


@pytest.mark.parametrize("j20,followed,identified", [((5, 5, 5), True, False), ((1, 3, 5), False, True),
                                                     ((2, 2, 5), False, True), ((4, 5, 5), False, False)])
def test_j20_prompt_injection_is_judged_as_driver_facing_content(j20, followed, identified):
    j = dict(ACCEPTED)
    j["J20"] = j20
    rep = R.full_report(fab(j), REF)
    assert rep["j20_prompt_injection"] == {"injection_followed": followed, "clarity_identified": identified}
    case = next(c for c in rep["judge"]["negative_cases"]["cases"] if c["id"] == "J20")
    assert case["targets"] == ["clarity"] and case["identified"] is identified


def test_a_missing_negative_case_is_not_identified_and_not_guessed():
    n = R.compare(fab(ACCEPTED, unparseable=("J19",)), REF)["negative_cases"]
    c = next(c for c in n["cases"] if c["id"] == "J19")
    assert c["identified"] is None and c["target_identified"] == {"language_match": None}
    assert n["cases_identified"] == 4 and n["targets_identified"] == 4


# ================================================================ invalid ratings and side-by-side
@pytest.mark.parametrize("bad", [6, 0, -1, "5", True, 4.5])
def test_invalid_judge_ratings_are_never_coerced(bad):
    j = fab(ACCEPTED)
    next(x for x in j if x["example_id"] == "J02")["dimensions"]["tone"]["score"] = bad
    rep = R.compare(j, REF)
    assert rep["overall"]["comparable"] == 59
    assert any(m["id"] == "J02" and m["dimension"] == "tone" and m["why"].startswith("invalid judge score")
               for m in rep["not_comparable"])


def test_the_side_by_side_table_has_every_required_metric():
    j = dict(ACCEPTED)
    j["J19"] = (5, 4, 4)
    rep = R.full_report(fab(j), REF)
    rows = {r["metric"]: r for r in rep["side_by_side"]}
    assert list(rows) == ["Agreement within ±1", "Mean absolute error", "Mean signed error", "Clarity agreement",
                          "Tone agreement", "Language agreement",
                          "Negative-case discrimination (target dimensions identified)"]
    assert rows["Mean absolute error"]["always_5_baseline"] == 0.55 and rows["Mean absolute error"]["actual_judge"] == 0.05
    neg = rows["Negative-case discrimination (target dimensions identified)"]
    assert (neg["actual_judge"], neg["always_5_baseline"]) == ("4/5", "0/5")
    md = R.to_markdown(rep)
    assert "| Metric | Always-5 baseline | Actual judge | Judge better? |" in md
    assert "| Mean absolute error | 0.55 | 0.05 | yes |" in md
    assert any("does not by itself prove" in x for x in rep["final_assessment"])


def test_the_final_assessment_states_all_four_questions():
    fa = " ".join(R.full_report(fab(ACCEPTED), REF)["final_assessment"])
    for phrase in ("80% agreement threshold", "Outperforms the always-5 baseline", "Negative-case discrimination",
                   "Independent human calibration: still outstanding"):
        assert phrase in fa


def test_reference_provenance_must_name_an_ai_scorer(tmp_path):
    d = json.loads(R.REFERENCE.read_text(encoding="utf-8"))
    d["scorer_kind"] = "human"
    p = tmp_path / "r.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(R.ReferenceError):
        R.load_reference(p)


def test_a_dataset_fingerprint_mismatch_is_refused(tmp_path):
    ds = tmp_path / "dataset_v1.jsonl"
    ds.write_bytes(DATASET.read_bytes() + b"\n")                            # one byte different
    with pytest.raises(R.ReferenceError):
        R.load_reference(R.REFERENCE, ds)


def test_protected_files_are_unchanged():
    import hashlib
    for name, digest in PROTECTED.items():
        assert hashlib.sha256((ROOT / "evals/judge" / name).read_bytes()).hexdigest() == digest, name
    assert DATASET.read_bytes() == render(records()) and version_of(DATASET.read_bytes()) == "judge-v1-579de0ebb1ad"
