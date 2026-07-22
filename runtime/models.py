"""Model-role routing through the local LiteLLM gateway."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .state import StateStore


@dataclass(frozen=True)
class ModelRoute:
    """A logical role routed by LiteLLM."""

    alias: str
    max_tokens: int = 2048
    temperature: float = 0.0


class AuditedModel:
    """Delegate to a smolagents model while recording available usage metrics."""

    def __init__(
        self,
        model: Any,
        *,
        route: str,
        state: StateStore,
        run_id: str,
    ) -> None:
        self._model = model
        self._route = route
        self._state = state
        self._run_id = run_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        """Generate one response and persist known token counts."""
        started = time.perf_counter()
        metadata: dict[str, Any] = {}
        try:
            response = self._model.generate(*args, **kwargs)
            usage = getattr(response, "token_usage", None)
            prompt_tokens = getattr(usage, "input_tokens", None)
            completion_tokens = getattr(usage, "output_tokens", None)
            metadata["status"] = "success"
            return response
        except Exception as exc:
            prompt_tokens = None
            completion_tokens = None
            metadata.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            self._state.add_model_metrics(
                self._run_id,
                route=self._route,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=(time.perf_counter() - started) * 1000,
                metadata=metadata,
            )


class ModelRegistry:
    """Construct smolagents models for logical local routes."""

    def __init__(
        self,
        *,
        api_base: str = "http://127.0.0.1:4000/v1",
        api_key: str = "local",
    ) -> None:
        self.api_base = api_base
        self.api_key = api_key
        self.routes = {
            "local-fast": ModelRoute("local-fast", max_tokens=2048),
            "local-plan": ModelRoute("local-plan", max_tokens=3072),
            "local-review": ModelRoute("local-review", max_tokens=2048),
        }

    def build(self, alias: str) -> Any:
        """Build a smolagents LiteLLMModel lazily."""
        if alias not in self.routes:
            raise KeyError(f"Unknown model route: {alias}")
        try:
            from smolagents import LiteLLMModel
        except ImportError as exc:
            raise RuntimeError(
                "smolagents is not installed. Run `make agent-install`."
            ) from exc
        route = self.routes[alias]
        return LiteLLMModel(
            model_id=f"openai/{route.alias}",
            api_base=self.api_base,
            api_key=self.api_key,
            temperature=route.temperature,
            max_tokens=route.max_tokens,
        )

    def litellm_available(self, timeout: float = 2.0) -> bool:
        """Return whether the LiteLLM proxy port is accepting connections."""
        try:
            with socket.create_connection(("127.0.0.1", 4000), timeout=timeout):
                return True
        except OSError:
            return False

    def llama_available(self, timeout: float = 2.0) -> bool:
        """Return whether llama-server reports a healthy state."""
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8080/health", timeout=timeout
            ) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return False
        return response.status == 200 and payload.get("status") == "ok"
