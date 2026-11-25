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
import math
import re
import time
import uuid
from decimal import Decimal
from typing import Any, Callable

import psycopg

from .errors import (BUDGET_CONFIGURATION_ERROR, BUDGET_EXCEEDED, EMPTY_SECRET, INVALID_REQUEST, MISSING_SECRET,
                     PROVIDER_AUTH_ERROR, SECRET_RETRIEVAL_ERROR, UNKNOWN_PROVIDER_ERROR, classify_http,
                     classify_secret_error, classify_transport, key_shape)
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
# R4: OpenRouter answers 403 both for permissions and when a key, workspace or guardrail spending limit is spent; the
# latter's message names the limit. errors.classify_http tells them apart (detail `credits` vs `auth`).
# G.1 (G3-C3): after a PROVIDER_AUTH_ERROR the key is re-read from Secrets Manager before the NEXT call, at most once
# per KEY_REFRESH_S, so a key the owner puts in the secret is used without a redeploy. The failed call itself is never
# repeated (no extra model call).
KEY_REFRESH_S = 300.0


def load_openrouter_key(env: dict, environment: str, secrets_client: Any = None) -> str:
    """staging/pilot: AWS Secrets Manager only (SETUHAUL_OPENROUTER_SECRET_ID). dev/eval/ci: OPENROUTER_API_KEY, or the
    secret. The key is returned to the client and never logged or put in an error message; neither is the secret's
    ARN (it carries the account id). Failures are MISSING_SECRET / EMPTY_SECRET / SECRET_ACCESS_DENIED /
    SECRET_RETRIEVAL_ERROR (G.1)."""
    return load_openrouter_key_meta(env, environment, secrets_client)[0]


def load_openrouter_key_meta(env: dict, environment: str, secrets_client: Any = None) -> tuple[str, dict]:
    """The key and safe metadata about it: where it came from, the secret version id and the key's shape
    (`openrouter_format` / `unrecognized_format`), never the value, its length or a prefix."""
    secret_id = (env.get("SETUHAUL_OPENROUTER_SECRET_ID") or "").strip()
    if environment in KEY_FROM_ENV_ENVIRONMENTS:
        key = (env.get("OPENROUTER_API_KEY") or "").strip()
        if key:
            return key, {"source": "environment", "version_id": None, "shape": key_shape(key)}
        if not secret_id:
            raise ModelError("OPENROUTER_API_KEY or SETUHAUL_OPENROUTER_SECRET_ID is required", MISSING_SECRET, False,
                             detail="not_configured")
    else:
        if env.get("OPENROUTER_API_KEY"):
            raise ModelError(f"OPENROUTER_API_KEY is not accepted in {environment}; use Secrets Manager "
                             "(SETUHAUL_OPENROUTER_SECRET_ID)", INVALID_REQUEST, False, detail="key_in_environment")
        if not secret_id:
            raise ModelError(f"SETUHAUL_OPENROUTER_SECRET_ID is required in {environment}", MISSING_SECRET, False,
                             detail="not_configured")
    try:
        if secrets_client is None:
            import boto3
            secrets_client = boto3.client("secretsmanager",
                                          region_name=env.get("SETUHAUL_AWS_REGION") or "ap-southeast-2")
        resp = secrets_client.get_secret_value(SecretId=secret_id)
    except Exception as e:                                        # the message never includes the secret value or ARN
        cat = classify_secret_error(e)
        raise ModelError(f"could not read the OpenRouter secret ({type(e).__name__})", cat, False,
                         detail=type(e).__name__) from None
    raw = resp.get("SecretString") if isinstance(resp, dict) else None
    if not isinstance(raw, str):
        raise ModelError("the OpenRouter secret has no SecretString", SECRET_RETRIEVAL_ERROR, False, detail="binary")
    key = raw.strip()
    if key.startswith("{"):
        try:
            doc = json.loads(key)
            key = (doc.get("api_key") or doc.get("OPENROUTER_API_KEY") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            raise ModelError("the OpenRouter secret is not readable JSON", SECRET_RETRIEVAL_ERROR, False,
                             detail="bad_json") from None
    if not key:
        raise ModelError("the secret holds no OpenRouter key", EMPTY_SECRET, False, detail="empty")
    return key, {"source": "secrets_manager", "version_id": resp.get("VersionId"), "shape": key_shape(key)}


class OpenRouterModel:
    """OpenRouter chat completions with the same prompts, schema and forced tool call as BedrockModel."""

    def __init__(self, api_key: str, *, extract_model: str = HAIKU_OR, phrase_model: str | None = None,
                 provider: str = DEFAULT_PROVIDER, meter: ModelBudget | None = None, client: Any = None,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S, read_timeout_s: float = READ_TIMEOUT_S,
                 sleep: Callable[[float], None] = time.sleep, max_retry_wait_s: float = MAX_RETRY_WAIT_S,
                 key_loader: Callable[[], str] | None = None, key_refresh_s: float = KEY_REFRESH_S,
                 clock: Callable[[], float] = time.monotonic):
        if not api_key:
            raise ModelError("no OpenRouter API key", EMPTY_SECRET, False, detail="empty")
        if provider not in VERIFIED_ZDR_ROUTES:
            raise ModelError(f"provider {provider!r} is not a verified ZDR route {sorted(VERIFIED_ZDR_ROUTES)}",
                             INVALID_REQUEST, False, detail="config:provider")
        import httpx
        self.extract_model, self.phrase_model, self.provider = extract_model, phrase_model, provider
        self.provider_name = VERIFIED_ZDR_ROUTES[provider]
        self.meter, self.sleep, self.max_retry_wait_s = meter, sleep, max_retry_wait_s
        self.client = client or httpx.Client(timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s,
                                                                   write=5.0, pool=connect_timeout_s))
        self._set_key(api_key)
        self.key_loader, self.key_refresh_s, self.clock = key_loader, key_refresh_s, clock
        self._auth_failed_at: float | None = None
        self._key_loaded_at = clock()

    def _set_key(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "setuhaul"}

    def _maybe_refresh_key(self) -> None:
        """After a provider auth failure, re-read the key (bounded: once per key_refresh_s). A failed re-read raises
        the secret's category before any request is sent; a successful one only swaps the header."""
        if self.key_loader is None or self._auth_failed_at is None:
            return
        if self.clock() - self._key_loaded_at < self.key_refresh_s:
            return
        self._key_loaded_at = self.clock()
        self._set_key(self.key_loader())                 # ModelError(secret category) propagates: nothing was sent
        self._auth_failed_at = None
        log.info("model_call key re-read from Secrets Manager after an auth failure")

    # ---------------------------------------------------------------- the request
    def routing(self) -> dict:
        return {"order": [self.provider], "only": [self.provider], "allow_fallbacks": False,
                "require_parameters": True, "data_collection": "deny", "zdr": True}

    def _reserve_usd(self, model_id: str, body: dict) -> Decimal:
        p_in, p_out = PRICE.get(model_id, UNKNOWN_PRICE)
        est_in = len(json.dumps(body)) // 3 + 1                  # generous: a token is ~4 characters
        return max(MIN_RESERVATION, ((est_in * p_in) + (int(body.get("max_tokens", 600)) * p_out)) * Decimal("1.2"))

    def _call(self, purpose: str, model_id: str, body: dict,
              parse: Callable[[dict, Trace], Any]) -> tuple[Any, Trace]:
        """One metered model call. `parse` turns a well-formed HTTP answer into the result or raises ModelError; it
        runs before the attempt is settled, so an unusable answer is recorded as ERROR (with its real cost)."""
        import httpx
        body = {"model": model_id, "provider": self.routing(), **body}
        call_id = uuid.uuid4()
        tr = Trace(purpose, model_id, f"openrouter:{self.provider}",
                   schema_version=SCHEMA_VERSION if purpose == "extract" else None)
        tr.call_id, tr.requested_provider = str(call_id), self.provider
        self._maybe_refresh_key()                                 # G.1: before anything is reserved or sent
        for attempt in (1, 2):
            tr.attempts = attempt
            res = None
            if self.meter is not None:
                try:
                    res = self.meter.reserve(call_id, attempt, purpose, model_id, self.provider,
                                             self._reserve_usd(model_id, body))
                except psycopg.Error as e:                        # no ledger, no paid call
                    tr.outcome = "error:meter"
                    diag = getattr(e, "diag", None)
                    # what failed, never what was sent: class, SQLSTATE and the server's primary message only
                    log.warning("usage ledger unavailable: %s sqlstate=%s message=%s", type(e).__name__,
                                getattr(e, "sqlstate", None), str(getattr(diag, "message_primary", "") or "")[:200])
                    raise ModelError(f"usage ledger unavailable ({type(e).__name__})", BUDGET_CONFIGURATION_ERROR,
                                     False, detail="meter_error") from None
                if res.status != "RESERVED":
                    tr.outcome = f"error:{res.status.lower()}"
                    cat = BUDGET_EXCEEDED if res.status == "BUDGET_EXHAUSTED" else BUDGET_CONFIGURATION_ERROR
                    raise ModelError(f"model call not sent: {res.status}", cat, False, detail=res.status.lower())
            t0 = time.monotonic()
            retry, sent, category, detail, status, data, headers = False, True, None, None, None, None, {}
            try:
                r = self.client.post(OPENROUTER_URL, headers=self._headers, json=body)
                status, headers = r.status_code, r.headers
                try:
                    data = r.json()
                except ValueError:
                    data = None
            except httpx.TransportError as e:                     # connect/DNS/TLS/timeout/protocol (G.1 taxonomy)
                category, detail, sent, retry = classify_transport(e)
            latency = int((time.monotonic() - t0) * 1000)
            tr.latency_ms = latency
            wait = 0.0
            if category is None:
                if status == 200 and isinstance(data, dict) and "error" not in data:
                    category = None
                elif status == 200:
                    category, detail = UNKNOWN_PROVIDER_ERROR, "malformed"
                else:
                    category, detail = classify_http(status, _error_message(data))
                    if detail == "rate_limited":
                        wait = _retry_after(headers)
                        retry = wait <= self.max_retry_wait_s
                    elif detail == "server_error":
                        retry = True                              # 500/502 once; 503 (pinned route unavailable) never
            # R1: every metadata field is type-checked; a wrong type is treated as unknown, never trusted.
            meta = _metadata(data)
            cost = meta["cost"]                                   # None = unknown: the whole reservation is kept
            if category is not None and status in REJECTED_BEFORE_INFERENCE and cost is None:
                cost = 0                                           # rejected by OpenRouter before any inference
            if category is None and meta["provider"] is None:
                category, detail = UNKNOWN_PROVIDER_ERROR, "malformed"   # the serving provider cannot be verified
            elif category is None and meta["provider"] != self.provider_name:
                category, detail = UNKNOWN_PROVIDER_ERROR, "provider_mismatch"   # a route we did not pin: refuse it
            actual = meta["provider"]
            result, unusable = None, None
            if category is None:
                # The model answered: its usage is real whether or not the answer can be used. Reported usage only:
                # a missing count stays None (unknown), never a made-up zero (G.1).
                tr.input_tokens, tr.output_tokens = meta["input_tokens"], meta["output_tokens"]
                tr.cache_read_tokens = meta["cached_tokens"]
                tr.cost_usd, tr.region, tr.actual_provider = cost, f"openrouter:{actual}", actual
                try:
                    result = parse(data, tr)
                except ModelError as e:                            # unusable answer: settled as ERROR, then raised
                    unusable, category, detail = e, e.category, e.detail
            if res is not None:
                try:
                    self.meter.settle(res, ok=category is None, sent=sent, actual_provider=actual,
                                      input_tokens=meta["input_tokens"], output_tokens=meta["output_tokens"],
                                      cost_usd=cost, latency_ms=latency, error=category,
                                      generation_id=meta["generation_id"])
                except psycopg.Error as e:                        # the ledger keeps the reservation (conservative)
                    log.error("model_call settle failed call=%s attempt=%s (%s)", call_id, attempt, type(e).__name__)
            log.info("model_call call=%s attempt=%s purpose=%s model=%s requested=%s actual=%s status=%s category=%s "
                     "detail=%s latency_ms=%s in=%s out=%s cost=%s", call_id, attempt, purpose, model_id, self.provider,
                     actual, status, category, detail, latency, meta["input_tokens"], meta["output_tokens"], cost)
            if unusable is not None:
                raise unusable                                     # never retried: the model was paid for once
            if category is None:
                return result, tr
            if category == PROVIDER_AUTH_ERROR:
                self._auth_failed_at = self.clock()               # G.1: re-read the key before the next call
                log.error("ALERT model_call %s (%s): OpenRouter refused the key or its credit (status %s)",
                          category, detail, status)
            if retry and attempt == 1:
                if wait:
                    self.sleep(wait)
                continue
            tr.outcome = f"error:{category}"
            msg = _error_message(data)[:200]
            raise ModelError(f"OpenRouter {category} (status {status}) {msg}".strip(), category, False, detail=detail)
        raise ModelError("unreachable", UNKNOWN_PROVIDER_ERROR, False)     # pragma: no cover

    # ---------------------------------------------------------------- the two purposes
    def extract(self, user_block: str) -> tuple[dict, Trace]:
        spec = RECORD_DRIVER_REPORT
        return self._call("extract", self.extract_model, {
            "messages": [{"role": "system", "content": [{"type": "text", "text": EXTRACT_SYSTEM,
                                                         "cache_control": {"type": "ephemeral"}}]},
                         {"role": "user", "content": user_block}],
            "tools": [{"type": "function", "function": {"name": spec["name"], "description": spec["description"],
                                                        "parameters": spec["input_schema"]}}],
            "tool_choice": {"type": "function", "function": {"name": spec["name"]}},
            "temperature": 0, "max_tokens": 600}, _parse_extract)

    def phrase(self, facts_block: str, correction: str | None = None) -> tuple[str, Trace]:
        if not self.phrase_model:
            raise ModelError("no phrasing model configured (OD-3: production replies are templates)", "config", False)
        text = facts_block + (f"\n\nYour previous draft broke a rule: {correction}. Write it again." if correction else "")
        return self._call("phrase", self.phrase_model, {
            "messages": [{"role": "system", "content": [{"type": "text", "text": PHRASE_SYSTEM,
                                                         "cache_control": {"type": "ephemeral"}}]},
                         {"role": "user", "content": text}],
            "max_tokens": 300}, _parse_phrase)                      # Sonnet 5 rejects a non-default temperature


    def judge(self, system: str, user: str, tool: dict, max_tokens: int = 700) -> tuple[dict, Trace]:
        """EVALUATION ONLY (offline LLM judge, evals/langsmith/llm_judge.py): one metered call on the same pinned,
        zero-retention route and the same ledger (purpose 'judge', migration 0042), temperature 0, answered by a
        forced tool call. Returns {"args": the tool arguments, "served_model": the model OpenRouter reports}.
        Nothing in the agent or the API calls this; staging and pilot never build a judge client."""
        def parse(data: dict, tr: Trace) -> dict:
            args = _tool_args(data, tr, tool["name"])
            served = data.get("model") if isinstance(data.get("model"), str) else None
            return {"args": args, "served_model": served[:80] if served else None}
        return self._call("judge", self.extract_model, {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": [{"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                        "parameters": tool["input_schema"]}}],
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
            "temperature": 0, "max_tokens": max_tokens}, parse)


def _tool_args(data: dict, tr: Trace, name: str) -> dict:
    """The arguments of the forced tool call `name`, after checking the answer's structure; ModelError otherwise."""
    msg = _first_message(data, tr)
    calls = msg.get("tool_calls")
    if not isinstance(calls, list):
        raise _malformed(tr, "no tool_calls list")
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and fn.get("name") == name:
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    raise _malformed(tr, "tool arguments are not JSON") from None
            if not isinstance(args, dict):
                raise _malformed(tr, "tool arguments are not a JSON object")
            return args
    raise _malformed(tr, f"no {name} tool call")


def _parse_extract(data: dict, tr: Trace) -> dict:
    """The record_driver_report arguments, after checking the answer's structure; ModelError(malformed) otherwise."""
    spec = RECORD_DRIVER_REPORT
    msg = _first_message(data, tr)
    calls = msg.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise _malformed(tr, "tool_calls is not a list")
    for call in calls or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict):
            raise _malformed(tr, "a tool call has no function object")
        if fn.get("name") == spec["name"]:
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError as e:
                    tr.outcome = "error:bad_json"
                    raise ModelError(f"tool arguments are not JSON: {e}", "malformed", False) from None
            if not isinstance(args, dict):
                raise _malformed(tr, "tool arguments are not a JSON object")
            return args
    tr.outcome = "error:no_tool_call"
    raise ModelError("extraction returned no tool call", "malformed", False)


def _parse_phrase(data: dict, tr: Trace) -> str:
    content = _first_message(data, tr).get("content")
    if isinstance(content, list):
        content = "".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    if not isinstance(content, str) or not content.strip():
        tr.outcome = "error:empty"
        raise ModelError("empty reply", "malformed", False)
    return content.strip()


def _int(v) -> int | None:
    """A non-negative integer, or None (unknown). bool is not a number here."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _cost(v) -> float | None:
    """A finite, non-negative number of US dollars, or None (unknown: the reservation is kept)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) and v >= 0 else None


def _text(v, limit: int = 200) -> str | None:
    return v[:limit] if isinstance(v, str) and v else None


def _metadata(data) -> dict:
    """Usage, cost and provider metadata of an answer, each type-checked; anything malformed becomes None."""
    body = data if isinstance(data, dict) else {}
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    return {"provider": _text(body.get("provider")), "generation_id": _text(body.get("id")),
            "input_tokens": _int(usage.get("prompt_tokens")), "output_tokens": _int(usage.get("completion_tokens")),
            "cached_tokens": _int(details.get("cached_tokens")), "cost": _cost(usage.get("cost"))}


def _error_message(data) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    msg = err.get("message") if isinstance(err, dict) else None
    return msg if isinstance(msg, str) else ""


def _malformed(tr: Trace, why: str) -> ModelError:
    tr.outcome = "error:malformed"
    return ModelError(f"OpenRouter answer has an unexpected shape: {why}", "malformed", False)


def _first_message(data: dict, tr: Trace) -> dict:
    """choices[0].message, after checking every level's type."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _malformed(tr, "choices is not a non-empty list of objects")
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        raise _malformed(tr, "choices[0].message is not an object")
    return msg


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
        raise ModelError(f"CONFIG: model {model!r} is not approved for {environment} {sorted(PILOT_MODELS)}",
                         INVALID_REQUEST, False, detail="config:model_not_approved")
    if provider not in VERIFIED_ZDR_ROUTES:
        raise ModelError(f"CONFIG: provider {provider!r} is not a verified ZDR route {sorted(VERIFIED_ZDR_ROUTES)}",
                         INVALID_REQUEST, False, detail="config:provider")
    meter = None
    cap = (env.get("SETUHAUL_LLM_DAILY_BUDGET_USD") or "").strip()
    if pilot or cap:
        if db is None:
            raise ModelError(f"CONFIG: the usage ledger (database) is required in {environment}",
                             BUDGET_CONFIGURATION_ERROR, False, detail="no_ledger")
        try:
            cap_d = Decimal(cap)
        except Exception:
            raise ModelError("CONFIG: SETUHAUL_LLM_DAILY_BUDGET_USD must be set to a number of US dollars",
                             BUDGET_CONFIGURATION_ERROR, False, detail="cap_not_a_number") from None
        if not cap_d.is_finite() or cap_d <= 0:
            raise ModelError("CONFIG: SETUHAUL_LLM_DAILY_BUDGET_USD must be positive", BUDGET_CONFIGURATION_ERROR,
                             False, detail="cap_not_positive")
        meter = ModelBudget(db, environment, cap_d)
    key, meta = load_openrouter_key_meta(env, environment, secrets_client)
    # safe diagnosis (G.1): where the key came from, its secret version and shape; never the value
    log.info("openrouter key loaded source=%s version=%s shape=%s", meta["source"], meta["version_id"], meta["shape"])
    if meta["shape"] != "openrouter_format":
        log.warning("ALERT openrouter key shape=%s: the secret does not look like an OpenRouter key (OpenRouter keys "
                    "start with the public prefix sk-or-); calls will likely fail with PROVIDER_AUTH_ERROR", meta["shape"])
    phrase = None if pilot else (env.get("SETUHAUL_OR_PHRASE_MODEL") or SONNET_OR)
    loader = (lambda: load_openrouter_key(env, environment, secrets_client)) if meta["source"] == "secrets_manager" else None
    return OpenRouterModel(key, extract_model=model, phrase_model=phrase, provider=provider, meter=meter,
                           client=http_client, key_loader=loader)
