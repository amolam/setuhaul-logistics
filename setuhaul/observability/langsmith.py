"""Phase G.1: LangSmith Cloud over its REST API (no LangSmith SDK; see LANGSMITH_OBSERVABILITY.md §Decision).

  LangSmithClient   a small, bounded httpx client: runs batch (traces), datasets, examples, experiments, feedback.
  LangSmithExporter the tracing.Exporter that posts redacted runs (`POST /runs/batch`).
  tracer_from_env() builds the process tracer from configuration; any problem disables tracing (never the agent).

Configuration (all optional; tracing is OFF unless SETUHAUL_LANGSMITH_TRACING=on):
  SETUHAUL_LANGSMITH_TRACING     on | off (default off)
  SETUHAUL_LANGSMITH_SECRET_ID   staging/pilot: the Secrets Manager secret holding the key ({"api_key": "..."} or the
                                 bare key). dev/eval/ci may instead set LANGSMITH_API_KEY.
  SETUHAUL_LANGSMITH_PROJECT     default by environment: setuhaul-dev | setuhaul-synthetic-staging |
                                 setuhaul-synthetic-pilot. A pilot may not write to a dev project and vice versa.
  SETUHAUL_LANGSMITH_ENDPOINT    https://api.smith.langchain.com (default), https://eu.api.smith.langchain.com or
                                 https://apac.api.smith.langchain.com only
  SETUHAUL_LANGSMITH_UI_URL      the project's page in the LangSmith UI (links only; https://[eu.]smith.langchain.com/…)
  SETUHAUL_TRACE_CONTENT         on | off (default off): export sanitised driver text. Synthetic data only.
  SETUHAUL_LANGSMITH_SAMPLE_RATE 0..1 (default 1.0)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..agent.errors import EMPTY_SECRET, MISSING_SECRET, SECRET_RETRIEVAL_ERROR, classify_secret_error, key_shape
from .tracing import Exporter, Tracer, TracerConfig

log = logging.getLogger("setuhaul.langsmith")
ALLOWED_ENDPOINTS = ("https://api.smith.langchain.com", "https://eu.api.smith.langchain.com",
                     "https://apac.api.smith.langchain.com")      # LangSmith Cloud US / EU / APAC (Sydney)
ALLOWED_UI = ("https://smith.langchain.com/", "https://eu.smith.langchain.com/", "https://apac.smith.langchain.com/")
DEFAULT_PROJECT = {"dev": "setuhaul-dev", "eval": "setuhaul-dev", "ci": "setuhaul-dev",
                   "staging": "setuhaul-synthetic-staging", "pilot": "setuhaul-synthetic-pilot"}
KEY_FROM_ENV = frozenset({"dev", "eval", "ci"})
TIMEOUT_S = 2.0                     # dataset/experiment calls (evaluation tooling)
EXPORT_TIMEOUT_S = 1.0              # trace export from the agent Lambda


class LangSmithConfigError(Exception):
    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


def _on(v: str | None) -> bool:
    return (v or "").strip().lower() in ("on", "true", "1", "yes")


def endpoint_from_env(env: dict) -> str:
    ep = (env.get("SETUHAUL_LANGSMITH_ENDPOINT") or ALLOWED_ENDPOINTS[0]).strip().rstrip("/")
    if ep not in ALLOWED_ENDPOINTS:
        raise LangSmithConfigError(f"LangSmith endpoint {ep!r} is not allowed {ALLOWED_ENDPOINTS}", "INVALID_REQUEST")
    return ep


def project_from_env(env: dict, environment: str) -> str:
    project = (env.get("SETUHAUL_LANGSMITH_PROJECT") or DEFAULT_PROJECT.get(environment, "setuhaul-dev")).strip()
    if environment in ("pilot", "staging") and project == DEFAULT_PROJECT["dev"]:
        raise LangSmithConfigError("the synthetic pilot may not write to the dev project", "INVALID_REQUEST")
    if environment in KEY_FROM_ENV and project in (DEFAULT_PROJECT["pilot"], DEFAULT_PROJECT["staging"]):
        raise LangSmithConfigError("dev/eval/ci may not write to the pilot project", "INVALID_REQUEST")
    return project


def ui_url_from_env(env: dict) -> str | None:
    u = (env.get("SETUHAUL_LANGSMITH_UI_URL") or "").strip()
    return u if u and u.startswith(ALLOWED_UI) else None


def load_langsmith_key(env: dict, environment: str, secrets_client: Any = None) -> str:
    """Same rules as the OpenRouter key: Secrets Manager in staging/pilot; never logged, never in a message."""
    secret_id = (env.get("SETUHAUL_LANGSMITH_SECRET_ID") or "").strip()
    if environment in KEY_FROM_ENV and (env.get("LANGSMITH_API_KEY") or "").strip():
        return env["LANGSMITH_API_KEY"].strip()
    if environment not in KEY_FROM_ENV and env.get("LANGSMITH_API_KEY"):
        raise LangSmithConfigError("LANGSMITH_API_KEY is not accepted here; use SETUHAUL_LANGSMITH_SECRET_ID",
                                   "INVALID_REQUEST")
    if not secret_id:
        raise LangSmithConfigError("no LangSmith key configured", MISSING_SECRET)
    try:
        if secrets_client is None:
            import boto3
            secrets_client = boto3.client("secretsmanager",
                                          region_name=env.get("SETUHAUL_AWS_REGION") or "ap-southeast-2")
        raw = secrets_client.get_secret_value(SecretId=secret_id).get("SecretString")
    except Exception as e:                                        # never the value, never the ARN
        raise LangSmithConfigError(f"could not read the LangSmith secret ({type(e).__name__})",
                                   classify_secret_error(e)) from None
    if not isinstance(raw, str):
        raise LangSmithConfigError("the LangSmith secret has no SecretString", SECRET_RETRIEVAL_ERROR)
    key = raw.strip()
    if key.startswith("{"):
        try:
            key = str(json.loads(key).get("api_key") or "").strip()
        except (ValueError, AttributeError):
            raise LangSmithConfigError("the LangSmith secret is not readable JSON", SECRET_RETRIEVAL_ERROR) from None
    if not key:
        raise LangSmithConfigError("the LangSmith secret holds no key", EMPTY_SECRET)
    return key


class LangSmithClient:
    """Bounded REST calls (2 s timeout, no retries here: callers decide). The key is only ever in a header."""

    def __init__(self, api_key: str, endpoint: str = ALLOWED_ENDPOINTS[0], http: Any = None, timeout_s: float = TIMEOUT_S):
        if endpoint.rstrip("/") not in ALLOWED_ENDPOINTS:
            raise LangSmithConfigError(f"LangSmith endpoint {endpoint!r} is not allowed", "INVALID_REQUEST")
        import httpx
        self.endpoint = endpoint.rstrip("/")
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 1.5)))
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    def _req(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        r = self.http.request(method, f"{self.endpoint}{path}", headers=self._headers,
                              content=None if body is None else json.dumps(body, default=str), params=params)
        if r.status_code >= 300:
            raise RuntimeError(f"LangSmith {method} {path} -> HTTP {r.status_code}")   # never echo the body or key
        try:
            return r.json()
        except ValueError:
            return None

    # traces
    def post_runs(self, posts: list[dict]) -> None:
        self._req("POST", "/runs/batch", {"post": posts, "patch": []})

    # datasets / examples
    def find_dataset(self, name: str) -> dict | None:
        found = self._req("GET", "/datasets", params={"name": name}) or []
        return found[0] if isinstance(found, list) and found else None

    def create_dataset(self, name: str, description: str, metadata: dict) -> dict:
        return self._req("POST", "/datasets", {"name": name, "description": description, "data_type": "kv",
                                               "metadata": metadata})

    def create_examples(self, dataset_id: str, examples: list[dict]) -> Any:
        return self._req("POST", "/examples/bulk", [dict(e, dataset_id=dataset_id) for e in examples])

    def list_examples(self, dataset_id: str, limit: int = 100) -> list[dict]:
        """G.2: the dataset's examples (evaluation tooling only; never called by the agent)."""
        found = self._req("GET", "/examples", params={"dataset": dataset_id, "limit": limit}) or []
        return found if isinstance(found, list) else []

    def get_run(self, run_id: str) -> dict | None:
        """G.2: one run as LangSmith stored it (its `app_path` is the UI link). Evaluation tooling only."""
        return self._req("GET", f"/runs/{run_id}")

    # experiments
    def create_experiment(self, name: str, dataset_id: str, metadata: dict) -> dict:
        return self._req("POST", "/sessions", {"name": name, "reference_dataset_id": dataset_id,
                                               "extra": {"metadata": metadata}})

    def create_feedback(self, run_id: str, key: str, score: float | None, comment: str | None = None) -> Any:
        return self._req("POST", "/feedback", {"run_id": run_id, "key": key, "score": score, "comment": comment})


class LangSmithExporter(Exporter):
    def __init__(self, client: LangSmithClient):
        self.client = client

    def post(self, batch: list[dict]) -> None:
        self.client.post_runs(batch)


def tracer_from_env(env: dict | None = None, *, secrets_client: Any = None, http: Any = None) -> Tracer:
    """The process tracer. Off unless SETUHAUL_LANGSMITH_TRACING=on; any configuration problem leaves it off with one
    log line naming the category (tracing is non-critical: the agent never fails because of it)."""
    env = dict(os.environ) if env is None else env
    environment = (env.get("SETUHAUL_ENV") or "dev").strip().lower()
    cfg = TracerConfig(enabled=False, environment=environment,
                       agent_version=(env.get("SETUHAUL_AGENT_VERSION") or env.get("SETUHAUL_RELEASE") or "unknown")[:40],
                       ui_url=ui_url_from_env(env))
    if not _on(env.get("SETUHAUL_LANGSMITH_TRACING")):
        return Tracer(cfg)
    try:
        if (env.get("SETUHAUL_DATA_CLASS") or "").strip().lower() != "synthetic":
            raise LangSmithConfigError("LangSmith tracing needs SETUHAUL_DATA_CLASS=synthetic", "INVALID_REQUEST")
        cfg.project = project_from_env(env, environment)
        endpoint = endpoint_from_env(env)
        key = load_langsmith_key(env, environment, secrets_client)
        cfg.content_allowed = _on(env.get("SETUHAUL_TRACE_CONTENT"))
        try:
            cfg.sample_rate = min(1.0, max(0.0, float(env.get("SETUHAUL_LANGSMITH_SAMPLE_RATE") or 1.0)))
        except ValueError:
            cfg.sample_rate = 1.0
        cfg.enabled = True
        log.info("langsmith tracing on project=%s endpoint=%s key_shape=%s content=%s sample=%s", cfg.project, endpoint,
                 key_shape(key), cfg.content_allowed, cfg.sample_rate)
        # export timeout 1 s: a flush is bounded by its budget plus at most one in-flight post (LANGSMITH_OBSERVABILITY §5)
        return Tracer(cfg, LangSmithExporter(LangSmithClient(key, endpoint, http=http, timeout_s=EXPORT_TIMEOUT_S)))
    except LangSmithConfigError as e:
        log.warning("langsmith tracing OFF (%s): %s", e.category, e)
        return Tracer(cfg)
    except Exception as e:                                         # noqa: BLE001
        log.warning("langsmith tracing OFF (UNKNOWN: %s)", type(e).__name__)
        return Tracer(cfg)
