"""The offline LLM judge as a LangSmith dataset experiment, reusing the existing G.1/G.2 LangSmith setup.

Nothing new in AWS: the judge runs on the owner's machine (judge_run.py --paid --langsmith), against the frozen
30-reply synthetic dataset, and LangSmith Cloud records it as an offline experiment:

  dataset     setuhaul-llm-judge-<dataset version>   30 examples (inputs: language, situation, driver messages,
              reply; outputs: the accepted AI reference scores for the 20 calibration records). Created once and
              matched by example id and content hash; a stored example whose content differs is reported, never
              replaced. The name carries the version, so a new dataset version is a new dataset.
  experiment  llm-judge-<dataset version>-<UTC>   one session on that dataset, with the model, provider, served model
              version, prompt version, dataset fingerprint, cost, tokens and the acceptance statuses as metadata;
              one run per judged example, linked to its dataset example, holding the scores, rationales, exact quotes,
              tokens, cost and latency; feedback quality:clarity / quality:tone / quality:language_match per run
              (the comment is the rationale and the quote).

Same client, endpoints, key handling and redaction guard as G.2 (observability/langsmith.LangSmithClient,
g2_smoke.config, redaction.find_leaks). The LangSmith key and workspace come from the owner's shell
(LANGSMITH_API_KEY, LANGSMITH_ENDPOINT, LANGSMITH_WORKSPACE_ID); they are never printed.

Order (so a LangSmith problem never wastes a paid call, and an upload problem never causes a second one):
  1. preflight: LangSmith configuration, then the dataset sync. Any failure here refuses the paid run;
  2. the paid run (judge_run.paid_run: no retries, run cap, ledger);
  3. the experiment upload. If it fails, the local results file stands and `--langsmith-upload <file>` retries the
     UPLOAD only, never the model calls.
No online evaluator is attached to any tracing project, and no pilot trace is read.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

DATASET_PREFIX = "setuhaul-llm-judge-"


def ls_config(env: dict) -> dict:
    """The G.2 configuration rules (key present, allowed endpoint, workspace id, dev/eval only, never the pilot DSN),
    without requiring trace export: an experiment upload is not tracing."""
    from evals.langsmith.g2_smoke import config
    cfg = config(dict(env, LANGSMITH_TRACING="true"))
    return cfg


def dataset_name(version: str) -> str:
    return f"{DATASET_PREFIX}{version}"


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def examples(recs: list[dict], version: str, reference: dict | None) -> list[dict]:
    scores = (reference or {}).get("scores") or {}
    out = []
    for r in recs:
        inputs = {"example_id": r["id"], "language": r["language"], "situation": r["situation"],
                  "driver_messages": r["conversation"], "reply": r["reply"]}
        outputs = {"ai_reference_scores": scores.get(r["id"]),
                   "reference_note": "AI reference (not human calibration)" if r["id"] in scores else None}
        out.append({"example_id": r["id"], "inputs": inputs, "outputs": outputs,
                    "metadata": {"example_id": r["id"], "dataset_version": version, "calibration": r["calibration"],
                                 "source": r["source"], "content_sha": _sha([inputs, outputs]), "synthetic": True}})
    return out


def _guard(obj) -> None:
    from setuhaul.observability.redaction import find_leaks
    leaks = find_leaks(obj)
    if leaks:
        raise ValueError(f"LangSmith payload matches a redaction pattern: {leaks}")


def sync_dataset(client, exs: list[dict], version: str) -> dict:
    name = dataset_name(version)
    _guard([{"i": e["inputs"], "o": e["outputs"]} for e in exs])
    found, created = client.find_dataset(name), False
    if found is None:
        found = client.create_dataset(name, f"SetuHaul offline LLM judge: frozen synthetic dataset {version} "
                                            "(30 replies; synthetic data only)",
                                      {"dataset_version": version, "examples": len(exs), "synthetic": True})
        created = True
    ds_id = found["id"]
    existing = {} if created else {(x.get("metadata") or {}).get("example_id"): x for x in client.list_examples(ds_id)}
    new = [e for e in exs if e["example_id"] not in existing]
    if new:
        client.create_examples(ds_id, [{"inputs": e["inputs"], "outputs": e["outputs"], "metadata": e["metadata"]}
                                       for e in new])
        existing.update({(x.get("metadata") or {}).get("example_id"): x for x in client.list_examples(ds_id)})
    drift = sorted(e["example_id"] for e in exs if e["example_id"] in existing
                   and (existing[e["example_id"]].get("metadata") or {}).get("content_sha") != e["metadata"]["content_sha"])
    missing = sorted(e["example_id"] for e in exs if not (existing.get(e["example_id"]) or {}).get("id"))
    return {"dataset_name": name, "dataset_id": ds_id, "tenant_id": found.get("tenant_id"), "created": created,
            "examples_added": [e["example_id"] for e in new], "content_drift": drift, "missing": missing,
            "example_ids": {k: x.get("id") for k, x in existing.items() if k}}


def upload_experiment(client, ds: dict, result: dict, comparison: dict | None) -> dict:
    """One experiment session; one run per judgement linked to its dataset example; quality:* feedback per run."""
    version = result["dataset_version"]
    name = f"llm-judge-{version}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    meta = {"kind": "offline_llm_judge", "dataset_version": version, "prompt_version": result["prompt_version"],
            "model": result["model"], "provider": result["provider"], "served_models": result.get("served_models"),
            "charged_usd": result["actual"]["charged_usd_by_ledger"], "requests": result["actual"].get("requests"),
            "input_tokens": result["actual"]["input_tokens"], "output_tokens": result["actual"]["output_tokens"],
            "advisory_only": True, "independent_human_calibration": False,
            "statuses": (comparison or {}).get("statuses")}
    sess = client.create_experiment(name, ds["dataset_id"], meta) or {}
    posts, feedback = [], []
    for j in result["judgements"]:
        rid, now = str(uuid.uuid4()), datetime.now(timezone.utc)
        dims = j.get("dimensions") or {}
        posts.append({
            "id": rid, "trace_id": rid, "name": f"llm-judge:{j['example_id']}", "run_type": "llm",
            "start_time": now.isoformat(), "end_time": now.isoformat(), "session_name": name,
            "dotted_order": f"{now:%Y%m%dT%H%M%S%fZ}{rid}",
            "reference_example_id": ds["example_ids"].get(j["example_id"]),
            "inputs": {"example_id": j["example_id"]},
            "outputs": {"status": j["status"], "reason": j.get("reason"),
                        "scores": {d: (v or {}).get("score") for d, v in dims.items()},
                        "rationale": {d: (v or {}).get("rationale") for d, v in dims.items()},
                        "evidence": {d: (v or {}).get("evidence") for d, v in dims.items()},
                        "insufficient_evidence": {d: (v or {}).get("insufficient_evidence") for d, v in dims.items()}},
            "extra": {"metadata": {"example_id": j["example_id"], "attempts": j["attempts"],
                                   "input_tokens": j.get("input_tokens"), "output_tokens": j.get("output_tokens"),
                                   "cost_usd": j.get("cost_usd"), "latency_ms": j.get("latency_ms"),
                                   "served_model": j.get("served_model"), "actual_provider": j.get("actual_provider"),
                                   "prompt_version": result["prompt_version"], "dataset_version": version}},
            "tags": ["setuhaul", "llm-judge", "offline", "synthetic"]})
        for d, v in dims.items():
            if (v or {}).get("score") is not None:
                feedback.append((rid, f"quality:{d}", v["score"], f"{v['rationale']} | quote: \"{v['evidence']}\""[:500]))
    _guard([{k: p[k] for k in ("inputs", "outputs", "extra", "name", "tags")} for p in posts] + [meta]
           + [c for *_, c in feedback])
    client.post_runs(posts)
    for rid, k, s, c in feedback:
        client.create_feedback(rid, k, s, c)
    return {"experiment_name": name, "experiment_id": sess.get("id"), "tenant_id": sess.get("tenant_id"),
            "runs": len(posts), "feedback": len(feedback)}


def experiment_url(cfg: dict, ds: dict, exp: dict) -> str | None:
    tid = exp.get("tenant_id") or ds.get("tenant_id")
    if not (tid and exp.get("experiment_id")):
        return None
    return f"{cfg['ui']}/o/{tid}/datasets/{ds['dataset_id']}/compare?selectedSessions={exp['experiment_id']}"
