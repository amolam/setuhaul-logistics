"""Phase G.1 (G2): the LangSmith evaluation datasets, built deterministically from the repository.

Sources (nothing is invented here):
  * evals/phase_g/dataset.jsonl          the 67 Phase G scenarios (baseline 65 pass / 2 fail: I14, I15)
  * evals/langsmith/rules_reader_cases.jsonl   the G.1 rules-reader regression cases (deadlines, arrivals, negatives)
  * evals/langsmith/g1_conversations.jsonl     G.1 scenarios for thin categories (fallback by failure category,
                                               expired slots, idempotency), run by the Phase G executors

Every example carries: inputs, expected behaviour (outputs), metadata (source id, original category, executor kind,
tags), ONE of the 15 brief categories, and the names of the deterministic assertions that judge it. The dataset
version is a content hash, so the same files always give the same version and any edit gives a new one.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PHASE_G = ROOT / "evals" / "phase_g" / "dataset.jsonl"
RULES_CASES = HERE / "rules_reader_cases.jsonl"
G1_CONVERSATIONS = HERE / "g1_conversations.jsonl"        # G.1 scenarios run by the unchanged Phase G executors
DATASET_PREFIX = "setuhaul-g1"

CATEGORIES = ("intent", "tool_selection", "parameter_extraction", "eta_delay", "deadline", "arrival", "optimization",
              "booking", "approval", "authn_authz", "cross_facility_isolation", "idempotency", "expired_slots",
              "hindi_hinglish", "model_unavailable_fallback")

# One primary category per Phase G scenario (the original category stays in metadata and tags).
PRIMARY = {
    "I01": "intent", "I02": "parameter_extraction", "I03": "arrival", "I04": "intent", "I05": "intent",
    "I06": "eta_delay", "I07": "eta_delay", "I08": "hindi_hinglish", "I09": "hindi_hinglish", "I10": "hindi_hinglish",
    "I11": "parameter_extraction", "I12": "parameter_extraction", "I13": "intent", "I14": "deadline", "I15": "arrival",
    "I16": "intent",
    "C01": "optimization", "C02": "tool_selection", "C03": "parameter_extraction", "C04": "booking", "C05": "booking",
    "C06": "booking", "C07": "booking", "C08": "model_unavailable_fallback", "C09": "model_unavailable_fallback",
    "C10": "tool_selection", "C11": "tool_selection", "C12": "tool_selection", "C13": "tool_selection",
    "C14": "eta_delay", "C15": "booking", "C16": "optimization", "C17": "booking", "C18": "arrival",
    "C19": "authn_authz", "C20": "expired_slots",
    "A01": "optimization", "A02": "cross_facility_isolation", "A03": "authn_authz", "A04": "authn_authz",
    "A05": "authn_authz", "A06": "cross_facility_isolation", "A07": "authn_authz", "A08": "authn_authz",
    "A09": "authn_authz", "A10": "approval", "A11": "approval", "A12": "cross_facility_isolation",
    "A13": "cross_facility_isolation", "A14": "parameter_extraction", "A15": "approval",
    "T01": "tool_selection", "T02": "authn_authz", "T03": "authn_authz", "T04": "cross_facility_isolation",
    "T05": "idempotency", "T06": "approval", "T07": "booking",
    **{f"O0{i}": "optimization" for i in range(1, 10)},
}
EXTRA_TAGS = {"I08": ["eta_delay"], "I09": ["intent"], "I10": ["intent", "safety"], "I15": ["hindi_hinglish"],
              "C05": ["hallucination_resistance", "no_fabricated_confirmation"], "C06": ["no_fabricated_confirmation"],
              "C17": ["no_fabricated_confirmation"], "C14": ["latency"], "C15": ["cost"], "C19": ["privacy"],
              "C09": ["optimization"], "C18": ["optimization"], "C01": ["eta_delay"], "C03": ["optimization"]}

# Which deterministic evaluators apply to which category (evaluators.py decides "not applicable" per row as well).
ASSERTIONS = {
    "intent": ["intent"], "tool_selection": ["tool_selection", "no_unauthorized_booking"],
    "parameter_extraction": ["intent", "parameters"], "eta_delay": ["intent", "eta"],
    "deadline": ["intent", "deadline"], "arrival": ["intent", "arrival"],
    "optimization": ["optimizer_invoked", "hard_constraints", "no_cross_facility_leak"],
    "booking": ["no_unauthorized_booking", "no_fabricated_confirmation", "tool_selection"],
    "approval": ["approval_required"], "authn_authz": ["authorization"],
    "cross_facility_isolation": ["no_cross_facility_leak", "authorization"], "idempotency": ["idempotency"],
    "expired_slots": ["expired_offers", "no_unauthorized_booking"], "hindi_hinglish": ["intent", "parameters", "arrival"],
    "model_unavailable_fallback": ["fallback", "no_unauthorized_booking", "optimizer_invoked"],
}


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def build() -> tuple[list[dict], dict]:
    """(examples, manifest). Deterministic: the order and content follow the source files."""
    examples = []
    for r in _rows(PHASE_G):
        cat = PRIMARY[r["id"]]
        examples.append({
            "id": r["id"], "category": cat, "inputs": r["input"], "outputs": {"expected": r["expected"]},
            "metadata": {"source": "evals/phase_g/dataset.jsonl", "source_id": r["id"], "title": r["title"],
                         "original_category": r["category"], "kind": r["input"]["kind"],
                         "tags": sorted({r["category"], *EXTRA_TAGS.get(r["id"], [])}), "synthetic": True,
                         "baseline_known_failure": r["id"] in ("I14", "I15")},
            "assertions": ASSERTIONS[cat]})
    for r in _rows(G1_CONVERSATIONS):
        examples.append({
            "id": r["id"], "category": r["category"], "inputs": r["input"], "outputs": {"expected": r["expected"]},
            "metadata": {"source": "evals/langsmith/g1_conversations.jsonl", "source_id": r["id"], "title": r["title"],
                         "kind": r["input"]["kind"], "synthetic": True, "tags": ["g1_regression", r["category"]]},
            "assertions": ASSERTIONS[r["category"]]})
    for r in _rows(RULES_CASES):
        examples.append({
            "id": r["id"], "category": r["category"], "inputs": {"kind": "rules_case", "say": r["say"]},
            "outputs": {"expected": r["expect"]},
            "metadata": {"source": "evals/langsmith/rules_reader_cases.jsonl", "source_id": r["id"],
                         "negative": bool(r.get("negative")), "kind": "rules_case", "synthetic": True,
                         "tags": ["g1_regression", r["category"]]},
            "assertions": ASSERTIONS[r["category"]]})
    unknown = sorted({e["category"] for e in examples} - set(CATEGORIES))
    if unknown:
        raise ValueError(f"categories outside the 15: {unknown}")
    blob = json.dumps(examples, sort_keys=True, ensure_ascii=False).encode()
    version = "g1-" + hashlib.sha256(blob).hexdigest()[:12]
    counts = {c: sum(e["category"] == c for e in examples) for c in CATEGORIES}
    manifest = {"dataset_name": f"{DATASET_PREFIX}-agent-eval", "dataset_version": version, "examples": len(examples),
                "phase_g_scenarios": sum(e["metadata"]["source"].endswith("phase_g/dataset.jsonl") for e in examples),
                "g1_regression_cases": sum("g1_regression" in e["metadata"]["tags"] for e in examples),
                "by_category": counts, "categories": list(CATEGORIES)}
    return examples, manifest


def langsmith_examples(examples: list[dict], version: str) -> list[dict]:
    """The body LangSmith's examples API takes: inputs / outputs / metadata (the category and assertions ride in
    metadata, with the dataset version so an experiment can always be traced back to the exact content)."""
    return [{"inputs": e["inputs"], "outputs": e["outputs"],
             "metadata": {**e["metadata"], "example_id": e["id"], "category": e["category"],
                          "assertions": e["assertions"], "dataset_version": version}} for e in examples]


if __name__ == "__main__":
    ex, man = build()
    print(json.dumps(man, indent=1))
