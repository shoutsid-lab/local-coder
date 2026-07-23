"""DSPy language-model wiring for the existing LiteLLM role routes."""

from __future__ import annotations

from typing import Any

LITELLM_API_BASE = "http://127.0.0.1:4000/v1"
LITELLM_API_KEY = "local"
DSPY_ROUTES = frozenset({"local-fast", "local-plan", "local-review"})


def build_dspy_lm(
    route: str,
    *,
    api_base: str = LITELLM_API_BASE,
    api_key: str = LITELLM_API_KEY,
    max_tokens: int = 2048,
    timeout: int | None = None,
    dspy_module: Any | None = None,
) -> Any:
    """Return a deterministic DSPy LM for one trusted LiteLLM alias."""
    if route not in DSPY_ROUTES:
        raise ValueError(f"Unsupported DSPy route: {route}")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    if dspy_module is None:
        try:
            import dspy as dspy_module
        except ImportError as exc:
            raise RuntimeError(
                "DSPy is not installed. Run `make agent-install`."
            ) from exc

    options = {
        "model_type": "chat",
        "api_base": api_base,
        "api_key": api_key,
        "temperature": 0,
        "max_tokens": max_tokens,
        "cache": False,
        "num_retries": 0,
    }
    if timeout is not None:
        options["timeout"] = timeout
    return dspy_module.LM(f"openai/{route}", **options)
