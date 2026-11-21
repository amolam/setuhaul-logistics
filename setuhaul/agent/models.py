"""Model clients. The orchestrator talks to a `ModelClient`; tests use `FakeModel`; deployed code uses `BedrockModel`
through `production_model()`. Development and evaluation on synthetic data may use the OpenRouter client in
`evals/providers.py` (decision T38), which this package never imports.

`BedrockModel` calls Amazon Bedrock's Converse API (boto3) with the **India geographic inference profiles** only
(decision T4 / T24): Haiku 4.5 for extraction (forced tool call against the strict schema). `phrase()` (Sonnet 5,
no tools) is kept for the B-AI-3 ablation and the spike only: since owner decision OD-3 the production orchestrator
never calls it, and every reply is a template. It refuses, in code, any model id that is not an India profile and any region outside ap-south-1 /
ap-south-2; IAM denies `global.` profiles as well (spike B2). Every call returns a trace record with the fields
decision T28 requires.

Why boto3 Converse and not the Anthropic SDK's Bedrock client: that client sends an API version Bedrock accepts
only for Claude 4.7+, and the India profile here serves Haiku 4.5 (05_agent_observability_cost.md §1.2).
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
INDIA_REGIONS = {"ap-south-1", "ap-south-2"}
_INDIA_ID = re.compile(r"^(in\.|arn:aws:bedrock:ap-south-[12]:\d{12}:(application-)?inference-profile/in\.)")


class ModelError(Exception):
    """Any model failure (timeout, throttling, refusal, schema). The orchestrator falls back to code paths."""


def assert_india_only(model_id: str, region: str) -> None:
    if region not in INDIA_REGIONS:
        raise ModelError(f"RESIDENCY: region {region} is not ap-south-1 / ap-south-2")
    if not _INDIA_ID.match(model_id):
        raise ModelError(f"RESIDENCY: {model_id} is not an India (in.) inference profile")


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
    """Amazon Bedrock Converse, India profiles only. boto3 is imported lazily so tests do not need it."""

    def __init__(self, region: str = "ap-south-1", extract_model: str = HAIKU_IN, phrase_model: str = SONNET_IN,
                 timeout_s: float = 8.0, client: Any = None):
        for m in (extract_model, phrase_model):
            assert_india_only(m, region)
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
        text = facts_block + (f"\n\nYour previous draft broke a rule: {correction}. Write it again." if correction else "")
        r, tr = self._call("phrase", self.phrase_model,
                           system=[{"text": PHRASE_SYSTEM}, {"cachePoint": {"type": "default"}}],
                           messages=[{"role": "user", "content": [{"text": text}]}],
                           inferenceConfig={"maxTokens": 300})     # Sonnet 5 rejects a non-default temperature
        parts = [b["text"] for b in r["output"]["message"]["content"] if "text" in b]
        if not parts:
            raise ModelError("empty reply")
        return "".join(parts).strip(), tr


def production_model(env: dict | None = None) -> BedrockModel:
    """The only model entry point for deployed code: India Bedrock profiles, nothing else (review: fail closed).

    SETUHAUL_ENV must be set explicitly. SETUHAUL_MODEL_PROVIDER, if set, must be `bedrock`. A non-India client does
    not exist in this package at all: the OpenRouter evaluation provider lives in `evals/providers.py`, which is not
    part of the deployed package (decision T38)."""
    import os
    env = os.environ if env is None else env
    if not env.get("SETUHAUL_ENV"):
        raise ModelError("SETUHAUL_ENV must be set explicitly")
    provider = env.get("SETUHAUL_MODEL_PROVIDER", "bedrock").lower()
    if provider != "bedrock":
        raise ModelError(f"RESIDENCY: deployed code uses India Bedrock only, not {provider!r}")
    return BedrockModel(region=env.get("SETUHAUL_BEDROCK_REGION", "ap-south-1"), timeout_s=30)


def with_retry(fn: Callable[[], Any], attempts: int = 2) -> Any:
    """One retry on a model error, then give up (the caller falls back to code: templates / rules)."""
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except ModelError as e:
            last = e
    raise last
