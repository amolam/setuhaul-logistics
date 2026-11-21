"""Model clients. The orchestrator talks to a `ModelClient`; tests use `FakeModel`; deployed code gets its client from
`model_for()` (decision T52): OpenRouter (primary, synthetic data only) or Amazon Bedrock (optional).

Since OD-3 the only production model call is extraction (`Orchestrator._extract`). Every client returns the same
tool-call arguments, which the orchestrator validates against the strict extraction schema; any failure, of any
provider, ends in the rules reader. The provider is chosen once, when the process starts, and never changes during a
conversation. A model never books, approves, reserves capacity or changes booking state.

`BedrockModel` calls Amazon Bedrock's Converse API (boto3). It accepts only geographic inference profiles that match
the region: `au.` profiles in Sydney (ap-southeast-2, the project's region) and `in.` profiles in India; `global.`
profiles are refused. Every call returns a trace record with the fields decision T28 requires.

Why boto3 Converse and not the Anthropic SDK's Bedrock client: that client sends an API version Bedrock accepts
only for Claude 4.7+, and Haiku 4.5 is the extraction model (05_agent_observability_cost.md §1.2).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .extraction import RECORD_DRIVER_REPORT, SCHEMA_VERSION
from .prompts import EXTRACT_SYSTEM, PHRASE_SYSTEM, PROMPT_VERSION

HAIKU_IN = "in.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET_IN = "in.anthropic.claude-sonnet-5"
HAIKU_AU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"   # AU geographic profile; access in the account not yet verified
DEFAULT_BEDROCK_REGION = "ap-southeast-2"                    # Sydney: the project's fixed AWS region (T52)
# A geographic inference profile keeps requests inside its geography. Each allowed region accepts only its own prefix.
PROFILE_PREFIX = {"ap-southeast-2": "au.", "ap-south-1": "in.", "ap-south-2": "in."}
INDIA_REGIONS = {"ap-south-1", "ap-south-2"}
ALLOWED_ENVIRONMENTS = frozenset({"dev", "eval", "ci", "staging", "pilot"})   # `production` is refused (Phase B)
KEY_FROM_ENV_ENVIRONMENTS = frozenset({"dev", "eval", "ci"})                  # staging/pilot: Secrets Manager only


class ModelError(Exception):
    """Any model failure (timeout, throttling, refusal, schema, budget). The orchestrator falls back to code paths.

    `category` names the failure for logs and the turn result; `retryable` tells `with_retry` whether one more attempt
    is safe. Clients that retry on their own (OpenRouter) raise retryable=False once they have done so."""

    def __init__(self, message: str, category: str = "model_error", retryable: bool = True):
        super().__init__(message)
        self.category, self.retryable = category, retryable


def assert_allowed_profile(model_id: str, region: str) -> None:
    """Fail closed unless the model id is the geographic profile of this region (au. in Sydney, in. in India)."""
    prefix = PROFILE_PREFIX.get(region)
    if prefix is None:
        raise ModelError(f"CONFIG: Bedrock region {region} is not allowed (allowed: {sorted(PROFILE_PREFIX)})",
                         "config", False)
    arn = rf"arn:aws:bedrock:{region}:\d{{12}}:(application-)?inference-profile/{prefix}"
    if not (model_id.startswith(prefix) or re.match(arn.replace(".", r"\."), model_id)):
        raise ModelError(f"CONFIG: {model_id} is not a {prefix} geographic inference profile for {region}",
                         "config", False)


def assert_india_only(model_id: str, region: str) -> None:
    """The original India-only rule (decisions T4/T24), kept for an India deployment. The Sydney pilot uses T52."""
    if region not in INDIA_REGIONS:
        raise ModelError(f"RESIDENCY: region {region} is not ap-south-1 / ap-south-2", "config", False)
    try:
        assert_allowed_profile(model_id, region)
    except ModelError as e:
        raise ModelError(f"RESIDENCY: {e}", "config", False) from None


@dataclass
class Trace:
    """Decision T28: what was called, with which prompt and schema, at what cost."""
    purpose: str
    model_id: str
    region: str
    prompt_version: str = PROMPT_VERSION
    schema_version: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    outcome: str = "ok"
    # T52 usage tracking (OpenRouter): never any prompt or driver text
    call_id: str | None = None
    requested_provider: str | None = None
    actual_provider: str | None = None
    attempts: int = 1
    cost_usd: float | None = None


class ModelClient(Protocol):
    def extract(self, user_block: str) -> tuple[dict, Trace]: ...
    def phrase(self, facts_block: str, correction: str | None = None) -> tuple[str, Trace]: ...


@dataclass
class FakeModel:
    """Scripted model for tests: returns queued answers, or raises when the script says so."""
    extractions: list[Any] = field(default_factory=list)       # dict, or an Exception to raise
    replies: list[Any] = field(default_factory=list)           # str, or an Exception to raise
    calls: list[tuple[str, str]] = field(default_factory=list)

    def _next(self, q: list, purpose: str, block: str):
        self.calls.append((purpose, block))
        if not q:
            raise ModelError(f"fake {purpose}: script exhausted")
        v = q.pop(0)
        if isinstance(v, Exception):
            raise v
        return v(block) if callable(v) else v                  # a callable builds its answer from the prompt

    def extract(self, user_block: str) -> tuple[dict, Trace]:
        return self._next(self.extractions, "extract", user_block), Trace("extract", "fake-haiku", "ap-south-1", schema_version=SCHEMA_VERSION)

    def phrase(self, facts_block: str, correction: str | None = None) -> tuple[str, Trace]:
        return self._next(self.replies, "phrase", facts_block + (f"\n[correction] {correction}" if correction else "")), \
            Trace("phrase", "fake-sonnet", "ap-south-1")


class BedrockModel:
    """Amazon Bedrock Converse with a geographic inference profile. Optional provider (T52); boto3 is imported lazily.
    `phrase_model` is for evaluation only (no production reply calls a model since OD-3)."""

    def __init__(self, region: str = DEFAULT_BEDROCK_REGION, extract_model: str | None = None,
                 phrase_model: str | None = None, timeout_s: float = 8.0, client: Any = None):
        extract_model = extract_model or (HAIKU_AU if region == DEFAULT_BEDROCK_REGION else HAIKU_IN)
        for m in (extract_model, phrase_model):
            if m is not None:
                assert_allowed_profile(m, region)
        self.region, self.extract_model, self.phrase_model = region, extract_model, phrase_model
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(read_timeout=timeout_s, connect_timeout=3,
                                                retries={"max_attempts": 2, "mode": "standard"}))
        self.client = client

    def _call(self, purpose: str, model_id: str, **kw) -> tuple[dict, Trace]:
        t0 = time.monotonic()
        tr = Trace(purpose, model_id, self.region, schema_version=SCHEMA_VERSION if purpose == "extract" else None)
        try:
            r = self.client.converse(modelId=model_id, **kw)
        except Exception as e:                                       # botocore errors, timeouts, throttling
            tr.outcome, tr.latency_ms = f"error:{type(e).__name__}", int((time.monotonic() - t0) * 1000)
            raise ModelError(str(e)) from e
        u = r.get("usage", {})
        tr.input_tokens, tr.output_tokens = u.get("inputTokens", 0), u.get("outputTokens", 0)
        tr.cache_read_tokens = u.get("cacheReadInputTokens", 0)
        tr.latency_ms = r.get("metrics", {}).get("latencyMs", int((time.monotonic() - t0) * 1000))
        if r.get("stopReason") in ("guardrail_intervened", "content_filtered"):
            tr.outcome = r["stopReason"]
            raise ModelError(tr.outcome)
        return r, tr

    def extract(self, user_block: str) -> tuple[dict, Trace]:
        spec = RECORD_DRIVER_REPORT
        r, tr = self._call(
            "extract", self.extract_model,
            system=[{"text": EXTRACT_SYSTEM}, {"cachePoint": {"type": "default"}}],
            messages=[{"role": "user", "content": [{"text": user_block}]}],
            toolConfig={"tools": [{"toolSpec": {"name": spec["name"], "description": spec["description"],
                                                "inputSchema": {"json": spec["input_schema"]}}}],
                        "toolChoice": {"tool": {"name": spec["name"]}}},
            inferenceConfig={"maxTokens": 600, "temperature": 0})
        for block in r["output"]["message"]["content"]:
            if "toolUse" in block and block["toolUse"]["name"] == spec["name"]:
                inp = block["toolUse"]["input"]
                return (json.loads(inp) if isinstance(inp, str) else inp), tr
        tr.outcome = "error:no_tool_call"
        raise ModelError("extraction returned no tool call")

    def phrase(self, facts_block: str, correction: str | None = None) -> tuple[str, Trace]:
        if not self.phrase_model:
            raise ModelError("no phrasing model configured (OD-3: production replies are templates)", "config", False)
        text = facts_block + (f"\n\nYour previous draft broke a rule: {correction}. Write it again." if correction else "")
        r, tr = self._call("phrase", self.phrase_model,
                           system=[{"text": PHRASE_SYSTEM}, {"cachePoint": {"type": "default"}}],
                           messages=[{"role": "user", "content": [{"text": text}]}],
                           inferenceConfig={"maxTokens": 300})     # Sonnet 5 rejects a non-default temperature
        parts = [b["text"] for b in r["output"]["message"]["content"] if "text" in b]
        if not parts:
            raise ModelError("empty reply")
        return "".join(parts).strip(), tr


def model_for(env: dict | None = None, *, db: Any = None, secrets_client: Any = None, http_client: Any = None,
              bedrock_client: Any = None) -> ModelClient:
    """The one way deployed code gets a model client (decision T52). Fails closed on anything not stated explicitly.

    SETUHAUL_ENV             dev | eval | ci | staging | pilot (`production` is refused in Phase B)
    SETUHAUL_DATA_CLASS      must be `synthetic`
    SETUHAUL_MODEL_PROVIDER  openrouter | bedrock (no default)
    OpenRouter credentials   staging/pilot: AWS Secrets Manager (SETUHAUL_OPENROUTER_SECRET_ID); dev/eval/ci may use
                             OPENROUTER_API_KEY. Staging and pilot also need the usage ledger (`db`) and
                             SETUHAUL_LLM_DAILY_BUDGET_USD.
    Bedrock                  optional, region default ap-southeast-2 (au. profile); not metered yet, so it is accepted
                             in dev/eval/ci only.
    """
    import os
    env = os.environ if env is None else env
    environment = (env.get("SETUHAUL_ENV") or "").strip().lower()
    if not environment:
        raise ModelError("CONFIG: SETUHAUL_ENV must be set explicitly", "config", False)
    if environment not in ALLOWED_ENVIRONMENTS:
        raise ModelError(f"CONFIG: SETUHAUL_ENV={environment!r} is not allowed (allowed: {sorted(ALLOWED_ENVIRONMENTS)}; "
                         "production is not enabled)", "config", False)
    if (env.get("SETUHAUL_DATA_CLASS") or "").strip().lower() != "synthetic":
        raise ModelError("CONFIG: SETUHAUL_DATA_CLASS=synthetic is required (synthetic data only)", "config", False)
    provider = (env.get("SETUHAUL_MODEL_PROVIDER") or "").strip().lower()
    if provider == "openrouter":
        from .openrouter import openrouter_from_env
        return openrouter_from_env(env, environment, db=db, secrets_client=secrets_client, http_client=http_client)
    if provider == "bedrock":
        if environment not in KEY_FROM_ENV_ENVIRONMENTS:
            raise ModelError(f"CONFIG: Bedrock is optional and not metered yet; not accepted in {environment}",
                             "config", False)
        return BedrockModel(region=env.get("SETUHAUL_BEDROCK_REGION") or DEFAULT_BEDROCK_REGION,
                            extract_model=env.get("SETUHAUL_BEDROCK_EXTRACT_MODEL") or None,
                            phrase_model=env.get("SETUHAUL_BEDROCK_PHRASE_MODEL") or None,
                            timeout_s=10, client=bedrock_client)
    raise ModelError(f"CONFIG: SETUHAUL_MODEL_PROVIDER must be openrouter or bedrock, got {provider or 'nothing'!r}",
                     "config", False)


def with_retry(fn: Callable[[], Any], attempts: int = 2) -> Any:
    """One retry on a retryable model error, then give up (the caller falls back to code: templates / rules).
    An error marked retryable=False (a client that already retried, a budget or configuration refusal, an ambiguous
    timeout that may have been billed) is not tried again."""
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except ModelError as e:
            last = e
            if not getattr(e, "retryable", True):
                break
    raise last