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


def test_the_dataset_and_the_human_template_are_untouched():
    assert DATASET.read_bytes() == render(records())                        # the frozen calibration dataset
    h = json.loads((ROOT / "evals/judge/human_scores_v1.json").read_text(encoding="utf-8"))
    assert all(v[d] is None for v in h["scores"].values() for d in R.DIMENSIONS)   # still empty: not filled with AI
    assert not (ROOT / "evals/judge/human_scores_v1.json").read_text(encoding="utf-8").count("ChatGPT")


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
    assert rep["disagreements"] == [] and rep["verdict"].startswith("PRELIMINARY PASS")


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
    assert rep["verdict"].startswith("PRELIMINARY PASS")                       # 91.7% >= 80%


def test_the_threshold_is_80_percent_of_comparable_ratings_and_fails_below_it():
    assert R.ACCEPT_WITHIN_1 == 0.80
    always5 = R.compare(fab({i: (5, 5, 5) for i in ACCEPTED}), REF)           # a judge that always says 5
    assert always5["overall"]["within_1"] == round(53 / 60, 3) and always5["verdict"].startswith("PRELIMINARY PASS")
    always1 = R.compare(fab({i: (1, 1, 1) for i in ACCEPTED}), REF)
    assert always1["overall"]["within_1"] < 0.8 and always1["verdict"].startswith("PRELIMINARY FAIL")
    assert always1["overall"]["direction"] == "under-scores" and always1["overall"]["mean_signed_error"] < 0


def test_the_contrasts_are_always_flagged_for_manual_inspection_even_when_they_agree():
    rep = R.compare(fab(ACCEPTED), REF)
    assert [m["id"] for m in rep["manual_inspection"]] == ["J16", "J17", "J18", "J19", "J20"]
    assert all(m["max_abs_diff"] == 0 for m in rep["manual_inspection"])
    md = R.to_markdown(rep)
    assert "Flagged for manual inspection" in md and all(f"| {i} |" in md for i in R.MANUAL_INSPECTION)


def test_unscored_and_null_ratings_are_not_comparable_and_are_listed():
    rep = R.compare(fab(ACCEPTED, unparseable=("J05",), nulls=(("J18", "language_match"),)), REF)
    assert rep["overall"]["comparable"] == 56
    nc = {(m["id"], m["dimension"], m["why"]) for m in rep["not_comparable"]}
    assert ("J05", "tone", "no scored judgement") in nc and ("J18", "language_match", "judge did not score it") in nc
    j18 = next(m for m in rep["manual_inspection"] if m["id"] == "J18")
    assert j18["judge"]["language_match"] is None


def test_the_report_never_reads_as_human_calibration():
    rep = R.compare(fab(ACCEPTED), REF)
    assert rep["independent_human_calibration"] is False and "NOT independent human calibration" in rep["label"]
    md = R.to_markdown(rep)
    assert "NOT independent human calibration" in md and "(not human calibration)" in rep["verdict"]


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
