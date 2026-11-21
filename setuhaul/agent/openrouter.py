"""OpenRouter client: the primary model provider for the synthetic-data Sydney pilot (decision T52).

What every request carries, and why:
  * a fixed model (Claude Haiku 4.5 for extraction) and ONE pinned upstream provider: `provider.order` and
    `provider.only` name it, `allow_fallbacks` is false, so OpenRouter can neither re-route to another provider nor
    substitute another model; no `models` fallback list is ever sent;
  * `zdr: true` (only zero-data-retention endpoints) and `data_collection: "deny"`; `require_parameters: true`;
  * if the pinned route cannot serve the request under those rules, OpenRouter answers 503 and the turn falls back
    to the rules reader: the request is never re-routed. A response from any other provider is refused.

ZDR is OpenRouter's routing promise about the endpoints it selects; it does not by itself prove the upstream
provider's compliance. The data is synthetic throughout (SETUHAUL_DATA_CLASS=synthetic is mandatory).

Retries are bounded and per attempt metered (migration 0023): one retry for a connection failure before the request
was sent, a 429 whose Retry-After is short, or a 500/502. A read timeout, a 504/408, a malformed answer, an
authentication or credit error, a provider-restriction 503 and a budget refusal are never retried. Every error this
client raises is final (retryable=False); the orchestrator then uses the rules reader, never another provider.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Callable

from .extraction import RECORD_DRIVER_REPORT, SCHEMA_VERSION
from .metering import ModelBudget
from .models import KEY_FROM_ENV_ENVIRONMENTS, ModelError, Trace
from .prompts import EXTRACT_SYSTEM, PHRASE_SYSTEM

log = logging.getLogger("setuhaul.openrouter")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HAIKU_OR = "anthropic/claude-haiku-4.5"
SONNET_OR = "anthropic/claude-sonnet-5"          # evaluation only (OD-3: no phrasing model in production)
# Upstream routes checked on 21 Nov 2026 against OpenRouter's public endpoint lists (no model call): both serve
# anthropic/claude-haiku-4.5 and are on the ZDR list with safety_retention false. Slug -> provider name in responses.
VERIFIED_ZDR_ROUTES = {"amazon-bedrock": "Amazon Bedrock", "google-vertex": "Google"}
DEFAULT_PROVIDER = "amazon-bedrock"
PILOT_MODELS = frozenset({HAIKU_OR})            # the only model staging and pilot may call
# USD per token, OpenRouter's listed price for Haiku 4.5 (21 Nov 2026); unknown models reserve at a high bound.
PRICE = {HAIKU_OR: (Decimal("0.000001"), Decimal("0.000005"))}
UNKNOWN_PRICE = (Decimal("0.000015"), Decimal("0.000075"))
MIN_RESERVATION = Decimal("0.001")
CONNECT_TIMEOUT_S, READ_TIMEOUT_S, MAX_RETRY_WAIT_S = 3.0, 10.0, 2.0
REJECTED_BEFORE_INFERENCE = {400, 401, 402, 403, 404, 429, 503}   # OpenRouter does not bill these


def load_openrouter_key(env: dict, environment: str, secrets_client: Any = None) -> str:
    """staging/pilot: AWS Secrets Manager only (SETUHAUL_OPENROUTER_SECRET_ID). dev/eval/ci: OPENROUTER_API_KEY, or the
    secret. The key is returned to the client and never logged or put in an error message."""
    secret_id = (env.get("SETUHAUL_OPENROUTER_SECRET_ID") or "").strip()
    if environment in KEY_FROM_ENV_ENVIRONMENTS:
        key = (env.get("OPENROUTER_API_KEY") or "").strip()
        if key:
            return key
        if not secret_id:
            raise ModelError("CONFIG: OPENROUTER_API_KEY or SETUHAUL_OPENROUTER_SECRET_ID is required", "config", False)
    else:
        if env.get("OPENROUTER_API_KEY"):
            raise ModelError(f"CONFIG: OPENROUTER_API_KEY is not accepted in {environment}; use Secrets Manager "
                             "(SETUHAUL_OPENROUTER_SECRET_ID)", "config", False)
        if not secret_id:
            raise ModelError(f"CONFIG: SETUHAUL_OPENROUTER_SECRET_ID is required in {environment}", "config", False)
    try:
        if secrets_client is None:
            import boto3
            secrets_client = boto3.client("secretsmanager",
                                          region_name=env.get("SETUHAUL_AWS_REGION") or "ap-southeast-2")
        raw = secrets_client.get_secret_value(SecretId=secret_id)["SecretString"]
    except Exception as e:                                        # the message never includes the secret value
        raise ModelError(f"CONFIG: could not read secret {secret_id} ({type(e).__name__})", "config", False) from None
    key = raw.strip()
    if key.startswith("{"):
        try:
            doc = json.loads(key)
            key = (doc.get("api_key") or doc.get("OPENROUTER_API_KEY") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            key = ""
    if not key:
        raise ModelError(f"CONFIG: secret {secret_id} holds no OpenRouter key", "config", False)
    return key


class OpenRouterModel:
    """OpenRouter chat completions with the same prompts, schema and forced tool call as BedrockModel."""

    def __init__(self, api_key: str, *, extract_model: str = HAIKU_OR, phrase_model: str | None = None,
                 provider: str = DEFAULT_PROVIDER, meter: ModelBudget | None = None, client: Any = None,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S, read_timeout_s: float = READ_TIMEOUT_S,
                 sleep: Callable[[float], None] = time.sleep, max_retry_wait_s: float = MAX_RETRY_WAIT_S):
        if not api_key:
            raise ModelError("CONFIG: no OpenRouter API key", "config", False)
        if provider not in VERIFIED_ZDR_ROUTES:
            raise ModelError(f"CONFIG: provider {provider!r} is not a verified ZDR route {sorted(VERIFIED_ZDR_ROUTES)}",
                             "config", False)
        import httpx
        self.extract_model, self.phrase_model, self.provider = extract_model, phrase_model, provider
        self.provider_name = VERIFIED_ZDR_ROUTES[provider]
        self.meter, self.sleep, self.max_retry_wait_s = meter, sleep, max_retry_wait_s
        self.client = client or httpx.Client(timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s,
                                                                   write=5.0, pool=connect_timeout_s))
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "setuhaul"}

    # ---------------------------------------------------------------- the request
    def routing(self) -> dict:
        return {"order": [self.provider], "only": [self.provider], "allow_fallbacks": False,
                "require_parameters": True, "data_collection": "deny", "zdr": True}

    def _reserve_usd(self, model_id: str, body: dict) -> Decimal:
        p_in, p_out = PRICE.get(model_id, UNKNOWN_PRICE)
        est_in = len(json.dumps(body)) // 3 + 1                  # generous: a token is ~4 characters
        return max(MIN_RESERVATION, ((est_in * p_in) + (int(body.get("max_tokens", 600)) * p_out)) * Decimal("1.2"))

    def _call(self, purpose: str, model_id: str, body: dict) -> tuple[dict, Trace]:
        import httpx
        body = {"model": model_id, "provider": self.routing(), **body}
        call_id = uuid.uuid4()
        tr = Trace(purpose, model_id, f"openrouter:{self.provider}",
                   schema_version=SCHEMA_VERSION if purpose == "extract" else None)
        tr.call_id, tr.requested_provider = str(call_id), self.provider
        for attempt in (1, 2):
            tr.attempts = attempt
            res = None
            if self.meter is not None:
                try:
                    res = self.meter.reserve(call_id, attempt, purpose, model_id, self.provider,
                                             self._reserve_usd(model_id, body))
                except Exception as e:                            # no ledger, no paid call
                    tr.outcome = "error:meter"
                    raise ModelError(f"usage ledger unavailable ({type(e).__name__})", "meter_error", False) from None
                if res.status != "RESERVED":
                    tr.outcome = f"error:{res.status.lower()}"
                    raise ModelError(f"model call not sent: {res.status}", res.status.lower(), False)
            t0 = time.monotonic()
            retry, sent, category, status, data, headers = False, True, None, None, None, {}
            try:
                r = self.client.post(OPENROUTER_URL, headers=self._headers, json=body)
                status, headers = r.status_code, r.headers
                try:
                    data = r.json()
                except ValueError:
                    data = None
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                sent, category, retry = False, ("connect_timeout" if isinstance(e, httpx.ConnectTimeout)
                                                else "connect_error"), True
            except httpx.TimeoutException:
                category = "read_timeout"                         # may have been processed and billed: never retried
            except httpx.TransportError:
                category = "transport_error"                      # ambiguous once the request was written
            latency = int((time.monotonic() - t0) * 1000)
            tr.latency_ms = latency
            wait = 0.0
            if category is None:
                if status == 200 and isinstance(data, dict) and "error" not in data:
                    category = None
                elif status == 429:
                    category = "rate_limited"
                    wait = _retry_after(headers)
                    retry = wait <= self.max_retry_wait_s
                elif status in (401, 403):
                    category = "auth"
                elif status == 402:
                    category = "credits"
                elif status == 503:
                    category = "provider_unavailable"             # no endpoint meets the pinned/ZDR rules: no re-route
                elif status in (500, 502):
                    category, retry = "server_error", True
                elif status in (408, 504):
                    category = "upstream_timeout"                 # ambiguous: not retried
                elif status == 200:
                    category = "malformed"
                else:
                    category = "bad_request"
            usage = (data or {}).get("usage") or {} if isinstance(data, dict) else {}
            actual = data.get("provider") if isinstance(data, dict) else None
            cost = usage.get("cost")
            if category is not None and status in REJECTED_BEFORE_INFERENCE and cost is None:
                cost = 0                                           # rejected by OpenRouter before any inference
            if category is None and actual != self.provider_name:
                category = "provider_mismatch"                     # served by a route we did not pin: refuse it
            if res is not None:
                try:
                    self.meter.settle(res, ok=category is None, sent=sent, actual_provider=actual,
                                      input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                                      cost_usd=cost, latency_ms=latency, error=category,
                                      generation_id=data.get("id") if isinstance(data, dict) else None)
                except Exception as e:                            # the ledger keeps the reservation (conservative)
                    log.error("model_call settle failed call=%s attempt=%s (%s)", call_id, attempt, type(e).__name__)
            log.info("model_call call=%s attempt=%s purpose=%s model=%s requested=%s actual=%s status=%s category=%s "
                     "latency_ms=%s in=%s out=%s cost=%s", call_id, attempt, purpose, model_id, self.provider, actual,
                     status, category, latency, usage.get("prompt_tokens"), usage.get("completion_tokens"), cost)
            if category is None:
                tr.input_tokens, tr.output_tokens = usage.get("prompt_tokens", 0) or 0, usage.get("completion_tokens", 0) or 0
                tr.cache_read_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                tr.cost_usd, tr.region, tr.actual_provider = cost, f"openrouter:{actual}", actual
                return data, tr
            if category in ("auth", "credits"):
                log.error("ALERT model_call %s: OpenRouter refused the key or its credit (status %s)", category, status)
            if retry and attempt == 1:
                if wait:
                    self.sleep(wait)
                continue
            tr.outcome = f"error:{category}"
            err = (data or {}).get("error") if isinstance(data, dict) else None
            detail = err.get("message", "")[:200] if isinstance(err, dict) else ""
            raise ModelError(f"OpenRouter {category} (status {status}) {detail}".strip(), category, False)
        raise ModelError("unreachable", "model_error", False)     # pragma: no cover

    # ---------------------------------------------------------------- the two purposes
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
                    raise ModelError(f"tool arguments are not JSON: {e}", "malformed", False) from None
        tr.outcome = "error:no_tool_call"
        raise ModelError("extraction returned no tool call", "malformed", False)

    def phrase(self, facts_block: str, correction: str | None = None) -> tuple[str, Trace]:
        if not self.phrase_model:
            raise ModelError("no phrasing model configured (OD-3: production replies are templates)", "config", False)
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
            raise ModelError("empty reply", "malformed", False)
        return content.strip(), tr


def _retry_after(headers) -> float:
    try:
        v = float(headers.get("Retry-After", "") or 0)
    except (TypeError, ValueError):
        return float("inf")                                       # unknown wait: do not retry
    return v if v > 0 else 0.5


def openrouter_from_env(env: dict, environment: str, *, db: Any = None, secrets_client: Any = None,
                        http_client: Any = None) -> OpenRouterModel:
    """Build the pinned, metered client from configuration (called by models.model_for)."""
    pilot = environment not in KEY_FROM_ENV_ENVIRONMENTS
    model = (env.get("SETUHAUL_OR_EXTRACT_MODEL") or HAIKU_OR).strip()
    provider = (env.get("SETUHAUL_OR_PROVIDER") or DEFAULT_PROVIDER).strip()
    if pilot and model not in PILOT_MODELS:
        raise ModelError(f"CONFIG: model {model!r} is not approved for {environment} {sorted(PILOT_MODELS)}", "config", False)
    if provider not in VERIFIED_ZDR_ROUTES:
        raise ModelError(f"CONFIG: provider {provider!r} is not a verified ZDR route {sorted(VERIFIED_ZDR_ROUTES)}",
                         "config", False)
    meter = None
    cap = (env.get("SETUHAUL_LLM_DAILY_BUDGET_USD") or "").strip()
    if pilot or cap:
        if db is None:
            raise ModelError(f"CONFIG: the usage ledger (database) is required in {environment}", "config", False)
        try:
            cap_d = Decimal(cap)
        except Exception:
            raise ModelError("CONFIG: SETUHAUL_LLM_DAILY_BUDGET_USD must be set to a number of US dollars", "config",
                             False) from None
        if cap_d <= 0:
            raise ModelError("CONFIG: SETUHAUL_LLM_DAILY_BUDGET_USD must be positive", "config", False)
        meter = ModelBudget(db, environment, cap_d)
    key = load_openrouter_key(env, environment, secrets_client)
    phrase = None if pilot else (env.get("SETUHAUL_OR_PHRASE_MODEL") or SONNET_OR)
    return OpenRouterModel(key, extract_model=model, phrase_model=phrase, provider=provider, meter=meter,
                           client=http_client)
