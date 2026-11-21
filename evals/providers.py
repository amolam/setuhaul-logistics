"""Model providers for development and evaluation scripts (re-exports; decision T52).

The OpenRouter client now lives in the application package (`setuhaul/agent/openrouter.py`) and is built only by
`setuhaul.agent.models.model_for()`, which fails closed: an explicit environment (dev, eval, ci, staging, pilot),
SETUHAUL_DATA_CLASS=synthetic, an explicit SETUHAUL_MODEL_PROVIDER, a pinned zero-data-retention route, and Secrets
Manager credentials plus the usage ledger outside dev/eval/ci. Evaluation scripts call `model_from_env()`, which is
`model_for()`; there is no second implementation.

    SETUHAUL_ENV=eval SETUHAUL_DATA_CLASS=synthetic SETUHAUL_MODEL_PROVIDER=openrouter OPENROUTER_API_KEY=... \
        python evals/run_golden.py --extractor model ...
"""
from __future__ import annotations

from setuhaul.agent.models import ModelClient, ModelError, model_for
from setuhaul.agent.openrouter import (HAIKU_OR, OPENROUTER_URL, SONNET_OR, VERIFIED_ZDR_ROUTES,  # noqa: F401
                                       OpenRouterModel)


def require_synthetic_eval(env: dict | None = None) -> str:
    """For evaluation tools that call OpenRouter directly (held-out generation): dev/eval/ci and synthetic data only."""
    import os
    from setuhaul.agent.models import KEY_FROM_ENV_ENVIRONMENTS
    env = os.environ if env is None else env
    environment = (env.get("SETUHAUL_ENV") or "").lower()
    if environment not in KEY_FROM_ENV_ENVIRONMENTS or (env.get("SETUHAUL_DATA_CLASS") or "").lower() != "synthetic":
        raise ModelError("CONFIG: evaluation tools need SETUHAUL_ENV in dev/eval/ci and SETUHAUL_DATA_CLASS=synthetic",
                         "config", False)
    return environment


def model_from_env(env: dict | None = None) -> ModelClient:
    """The evaluation entry point: the same fail-closed selection deployed code uses."""
    return model_for(env)


__all__ = ["HAIKU_OR", "OPENROUTER_URL", "SONNET_OR", "VERIFIED_ZDR_ROUTES", "ModelError", "OpenRouterModel",
           "model_from_env", "require_synthetic_eval"]
