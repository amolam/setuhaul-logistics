"""The model-provider switch for development and evaluation (decision T38). Not part of the deployed package.

    SETUHAUL_MODEL_PROVIDER = bedrock (default) | openrouter
    SETUHAUL_ENV            = must be set; OpenRouter only in dev | eval | ci
    SETUHAUL_DATA_CLASS     = synthetic   (required for OpenRouter)
    OPENROUTER_API_KEY      = the key (never logged)

`bedrock` returns the same India-only client deployed code uses. `openrouter` sends prompts to providers outside
India, so it is fail-closed (review, step 3 update):
  * the environment must be named explicitly and be on the allowlist; a missing or unknown value is refused;
  * the data class must be declared synthetic;
  * the client needs a `SyntheticEvalOnly` capability, and both the capability and the client re-check the real
    process environment, so passing a hand-made env dict cannot unlock it in a deployed process;
  * `setuhaul/` (the deployed package) never imports this module; `tests/test_agent_units.py` checks that.
`data_collection: deny` is a provider preference, not a guarantee, which is why the synthetic-data rule stays.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from setuhaul.agent.extraction import RECORD_DRIVER_REPORT, SCHEMA_VERSION
from setuhaul.agent.models import ModelClient, ModelError, Trace, production_model
from setuhaul.agent.prompts import EXTRACT_SYSTEM, PHRASE_SYSTEM

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HAIKU_OR = "anthropic/claude-haiku-4.5"          # the same models as the India profiles, served outside India
SONNET_OR = "anthropic/claude-sonnet-5"
EVAL_ENVIRONMENTS = frozenset({"dev", "eval", "ci"})


def _check(env: dict) -> str:
    environment = (env.get("SETUHAUL_ENV") or "").lower()
    if environment not in EVAL_ENVIRONMENTS:
        raise ModelError(f"RESIDENCY: OpenRouter needs SETUHAUL_ENV in {sorted(EVAL_ENVIRONMENTS)}, got {environment or 'nothing'!r}")
    if (env.get("SETUHAUL_DATA_CLASS") or "").lower() != "synthetic":
        raise ModelError("RESIDENCY: OpenRouter needs SETUHAUL_DATA_CLASS=synthetic (no real driver data)")
    return environment


@dataclass(frozen=True)
class SyntheticEvalOnly:
    """Proof that this process is an evaluation run on synthetic data. Checked against the real environment."""
    environment: str

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SyntheticEvalOnly":
        given = _check(os.environ if env is None else env)
        if env is not None:
            _check(os.environ)                                     # a hand-made dict cannot unlock a deployed process
        return cls(given)


class OpenRouterModel:
    """OpenRouter chat completions with the same prompts, schema, forced tool call and trace fields as BedrockModel.
    Results are comparable for accuracy and tokens; latency and price are OpenRouter's, not Mumbai's."""

    def __init__(self, api_key: str, capability: SyntheticEvalOnly, extract_model: str = HAIKU_OR,
                 phrase_model: str = SONNET_OR, timeout_s: float = 30.0, client: Any = None):
        if not isinstance(capability, SyntheticEvalOnly):
            raise ModelError("RESIDENCY: OpenRouter needs a SyntheticEvalOnly capability")
        _check(os.environ)                                          # fail closed even if constructed directly
        if not api_key:
            raise ModelError("OPENROUTER_API_KEY is not set")
        import httpx
        self.extract_model, self.phrase_model = extract_model, phrase_model
        self.client = client or httpx.Client(timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "setuhaul-eval"}

    def _call(self, purpose: str, model_id: str, body: dict) -> tuple[dict, Trace]:
        t0 = time.monotonic()
        tr = Trace(purpose, model_id, "openrouter", schema_version=SCHEMA_VERSION if purpose == "extract" else None)
        body = {"model": model_id, "provider": {"data_collection": "deny", "require_parameters": True}, **body}
        try:
            r = self.client.post(OPENROUTER_URL, headers=self._headers, json=body)
            data = r.json()
        except Exception as e:                                       # network errors, timeouts, non-JSON bodies
            tr.outcome, tr.latency_ms = f"error:{type(e).__name__}", int((time.monotonic() - t0) * 1000)
            raise ModelError(str(e)) from e
        tr.latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            tr.outcome = f"error:http_{r.status_code}"
            raise ModelError(f"OpenRouter {r.status_code}: {err.get('message', str(data)[:200])}")
        u = data.get("usage") or {}
        tr.input_tokens, tr.output_tokens = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        tr.cache_read_tokens = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        tr.region = f"openrouter:{data.get('provider', '?')}"              # which upstream provider served it
        return data, tr

    def extract(self, user_block: str) -> tuple[dict, Trace]:
        spec = RECORD_DRIVER_REPORT
        data, tr = self._call("extract", self.extract_model, {
            "messages": [{"role": "system", "content": [{"type": "text", "text": EXTRACT_SYSTEM,
                                                         "cache_control": {"type": "ephemeral"}}]},
                         {"role": "user", "content": user_block}],
            "tools": [{"type": "function", "function": {"name": spec["name"], "description": spec["description"],
                                                        "parameters": spec["input_schema"]}}],
            "tool_choice": {"type": "function", "function": {"name": spec["name"]}},
            "temperature": 0, "max_tokens": 600})
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") == spec["name"]:
                try:
                    args = fn.get("arguments") or "{}"
                    return (json.loads(args) if isinstance(args, str) else args), tr
                except json.JSONDecodeError as e:
                    tr.outcome = "error:bad_json"
                    raise ModelError(f"tool arguments are not JSON: {e}") from e
        tr.outcome = "error:no_tool_call"
        raise ModelError("extraction returned no tool call")

    def phrase(self, facts_block: str, correction: str | None = None) -> tuple[str, Trace]:
        text = facts_block + (f"\n\nYour previous draft broke a rule: {correction}. Write it again." if correction else "")
        data, tr = self._call("phrase", self.phrase_model, {
            "messages": [{"role": "system", "content": [{"type": "text", "text": PHRASE_SYSTEM,
                                                         "cache_control": {"type": "ephemeral"}}]},
                         {"role": "user", "content": text}],
            "max_tokens": 300})                                      # Sonnet 5 rejects a non-default temperature
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content or not content.strip():
            raise ModelError("empty reply")
        return content.strip(), tr


def model_from_env(env: dict | None = None) -> ModelClient:
    """The switch between AWS (India Bedrock) and OpenRouter, for development and evaluation only."""
    env = os.environ if env is None else env
    provider = (env.get("SETUHAUL_MODEL_PROVIDER") or "bedrock").lower()
    if not env.get("SETUHAUL_ENV"):
        raise ModelError("SETUHAUL_ENV must be set explicitly")
    if provider == "bedrock":
        return production_model(env)
    if provider == "openrouter":
        cap = SyntheticEvalOnly.from_env(env)
        return OpenRouterModel(env.get("OPENROUTER_API_KEY", ""), cap,
                               extract_model=env.get("SETUHAUL_OR_EXTRACT_MODEL", HAIKU_OR),
                               phrase_model=env.get("SETUHAUL_OR_PHRASE_MODEL", SONNET_OR))
    raise ModelError(f"unknown SETUHAUL_MODEL_PROVIDER {provider!r}")
