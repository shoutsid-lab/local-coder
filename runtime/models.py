"""Model-role routing through the local LiteLLM gateway."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from .model_response import normalize_model_response, normalize_provider_error
from .route_profiles import DEFAULT_ROUTE_PROFILES, RouteProfile
from .state import StateStore

ModelRoute = RouteProfile


class ModelBudgetExceeded(RuntimeError):
    """Raised when one candidate build exhausts its shared model budget."""


@dataclass
class ModelUsageBudget:
    """Shared hard ceiling across every model route in one candidate build."""

    max_calls: int
    max_prompt_tokens: int
    max_completion_tokens: int
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def reserve_call(self) -> None:
        """Reserve one request before sending it to the model service."""
        if self.calls >= self.max_calls:
            raise ModelBudgetExceeded("Candidate model-call budget exhausted.")
        self.calls += 1

    def record_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Record returned usage and stop the run on missing or excess metrics."""
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            raise ModelBudgetExceeded("Candidate model usage was not reported.")
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        if self.prompt_tokens > self.max_prompt_tokens:
            raise ModelBudgetExceeded("Candidate prompt-token budget exhausted.")
        if self.completion_tokens > self.max_completion_tokens:
            raise ModelBudgetExceeded("Candidate completion-token budget exhausted.")


class AuditedModel:
    """Delegate to a smolagents model while recording available usage metrics."""

    def __init__(
        self,
        model: Any,
        *,
        route: str,
        state: StateStore,
        run_id: str,
        usage_budget: ModelUsageBudget | None = None,
        route_session: Callable[[str], Any] | None = None,
    ) -> None:
        self._model = model
        self._route = route
        self._state = state
        self._run_id = run_id
        self._usage_budget = usage_budget
        self._route_session = route_session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        """Generate one response and persist known token counts."""
        started = time.perf_counter()
        metadata: dict[str, Any] = {}
        if self._usage_budget is not None:
            self._usage_budget.reserve_call()
        try:
            session = (
                self._route_session(self._route)
                if self._route_session is not None
                else nullcontext({})
            )
            with session as service:
                service_metadata = dict(service or {})
                if service_metadata:
                    metadata["model_service"] = service_metadata
                response = self._model.generate(*args, **kwargs)
            normalized = normalize_model_response(response)
            prompt_tokens = normalized.prompt_tokens
            completion_tokens = normalized.completion_tokens
            metadata["status"] = "success"
            metadata.update(normalized.bounded_metadata())
        except Exception as exc:
            prompt_tokens = None
            completion_tokens = None
            normalized = normalize_provider_error(exc, model=self._route)
            metadata.update(status="error", error_type=type(exc).__name__)
            metadata.update(normalized.bounded_metadata())
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
        if self._usage_budget is not None:
            self._usage_budget.record_usage(prompt_tokens, completion_tokens)
        return response


class ModelRegistry:
    """Construct smolagents models for logical local routes."""

    def __init__(
        self,
        *,
        api_base: str = "http://127.0.0.1:4000/v1",
        api_key: str = "local",
        service_manager: Any | None = None,
    ) -> None:
        self.api_base = api_base
        self.api_key = api_key
        self.routes = dict(DEFAULT_ROUTE_PROFILES)
        self.service_manager = service_manager

    def prepare_route(self, alias: str) -> Mapping[str, Any]:
        """Ensure the physical model for one trusted logical route is live."""
        if alias not in self.routes:
            raise KeyError(f"Unknown model route: {alias}")
        if self.service_manager is None:
            return {}
        return self.service_manager.ensure_route(alias)

    @contextmanager
    def route_session(self, alias: str) -> Iterator[Mapping[str, Any]]:
        """Serialize one complete model call on its required physical profile."""
        if alias not in self.routes:
            raise KeyError(f"Unknown model route: {alias}")
        if self.service_manager is None:
            yield {}
            return
        session = getattr(self.service_manager, "route_session", None)
        if callable(session):
            with session(alias) as evidence:
                yield evidence
            return
        yield self.service_manager.ensure_route(alias)

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
            **route.request_kwargs(),
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
