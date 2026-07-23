"""Route-specific generation policy for local model aliases."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

ReasoningMode = Literal["off", "auto", "on"]


@dataclass(frozen=True)
class RouteProfile:
    """One bounded generation policy for a logical LiteLLM route."""

    alias: str
    model_alias: str
    reasoning_mode: ReasoningMode
    max_tokens: int
    reasoning_tokens: int
    final_answer_tokens: int
    temperature: float
    top_p: float
    top_k: int | None
    repetition_penalty: float | None
    timeout_seconds: int
    retries: int
    preserve_reasoning: bool
    requires_model_switch: bool

    def __post_init__(self) -> None:
        if self.reasoning_mode not in {"off", "auto", "on"}:
            raise ValueError(f"Unsupported reasoning mode: {self.reasoning_mode}")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.reasoning_tokens < 0:
            raise ValueError("reasoning_tokens must be non-negative")
        if self.final_answer_tokens <= 0:
            raise ValueError("final_answer_tokens must be positive")
        if self.reasoning_tokens + self.final_answer_tokens > self.max_tokens:
            raise ValueError("reasoning and final-answer budgets exceed max_tokens")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if not 0 < self.top_p <= 1:
            raise ValueError("top_p must be in (0, 1]")
        if self.top_k is not None and self.top_k <= 0:
            raise ValueError("top_k must be positive")
        if self.repetition_penalty is not None and self.repetition_penalty <= 0:
            raise ValueError("repetition_penalty must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.retries < 0:
            raise ValueError("retries must be non-negative")
        if self.reasoning_mode == "off" and self.reasoning_tokens != 0:
            raise ValueError(
                "reasoning-disabled profiles require a zero reasoning budget"
            )

    def request_kwargs(self) -> dict[str, Any]:
        """Return provider kwargs without exposing an unbounded generation path."""
        result: dict[str, Any] = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.top_p != 1.0:
            result["top_p"] = self.top_p
        if self.timeout_seconds != 180:
            result["timeout"] = self.timeout_seconds
        if self.retries != 0:
            result["num_retries"] = self.retries

        extra_body: dict[str, Any] = {}
        chat_template_kwargs: dict[str, Any] = {}
        if self.reasoning_mode != "auto":
            chat_template_kwargs["enable_thinking"] = self.reasoning_mode == "on"
        if self.preserve_reasoning:
            chat_template_kwargs["reasoning_preserve"] = True
        if chat_template_kwargs:
            extra_body["chat_template_kwargs"] = chat_template_kwargs
        if self.reasoning_mode != "auto":
            extra_body["thinking_budget_tokens"] = self.reasoning_tokens
        if self.top_k is not None:
            extra_body["top_k"] = self.top_k
        if self.repetition_penalty is not None:
            extra_body["repeat_penalty"] = self.repetition_penalty
        if extra_body:
            result["extra_body"] = extra_body
        return result

    def with_budgets(
        self,
        *,
        max_tokens: int,
        reasoning_tokens: int,
        final_answer_tokens: int,
    ) -> "RouteProfile":
        """Return a validated budget override for a bounded task purpose."""
        return replace(
            self,
            max_tokens=max_tokens,
            reasoning_tokens=reasoning_tokens,
            final_answer_tokens=final_answer_tokens,
        )


DEFAULT_ROUTE_PROFILES = {
    "local-fast": RouteProfile(
        alias="local-fast",
        model_alias="local-coder",
        reasoning_mode="off",
        max_tokens=2048,
        reasoning_tokens=0,
        final_answer_tokens=2048,
        temperature=0.0,
        top_p=1.0,
        top_k=None,
        repetition_penalty=None,
        timeout_seconds=180,
        retries=0,
        preserve_reasoning=False,
        requires_model_switch=False,
    ),
    "local-plan": RouteProfile(
        alias="local-plan",
        model_alias="local-coder",
        reasoning_mode="off",
        max_tokens=3072,
        reasoning_tokens=0,
        final_answer_tokens=3072,
        temperature=0.0,
        top_p=1.0,
        top_k=None,
        repetition_penalty=None,
        timeout_seconds=180,
        retries=0,
        preserve_reasoning=False,
        requires_model_switch=False,
    ),
    "local-review": RouteProfile(
        alias="local-review",
        model_alias="local-coder",
        reasoning_mode="off",
        max_tokens=2048,
        reasoning_tokens=0,
        final_answer_tokens=2048,
        temperature=0.0,
        top_p=1.0,
        top_k=None,
        repetition_penalty=None,
        timeout_seconds=180,
        retries=0,
        preserve_reasoning=False,
        requires_model_switch=False,
    ),
    "local-reason": RouteProfile(
        alias="local-reason",
        model_alias="local-reason",
        reasoning_mode="on",
        max_tokens=2048,
        reasoning_tokens=1024,
        final_answer_tokens=1024,
        temperature=0.6,
        top_p=0.95,
        top_k=20,
        repetition_penalty=1.05,
        timeout_seconds=300,
        retries=0,
        preserve_reasoning=False,
        requires_model_switch=True,
    ),
}

REASONING_PROFILE_EXAMPLES = {
    "exact-probe": replace(
        DEFAULT_ROUTE_PROFILES["local-reason"],
        reasoning_mode="off",
        max_tokens=64,
        reasoning_tokens=0,
        final_answer_tokens=64,
        temperature=0.0,
        top_p=1.0,
        top_k=None,
        repetition_penalty=None,
        timeout_seconds=120,
    ),
    "planner": DEFAULT_ROUTE_PROFILES["local-reason"],
    "reviewer": DEFAULT_ROUTE_PROFILES["local-reason"].with_budgets(
        max_tokens=1536,
        reasoning_tokens=768,
        final_answer_tokens=768,
    ),
    "diagnostic": DEFAULT_ROUTE_PROFILES["local-reason"].with_budgets(
        max_tokens=4096,
        reasoning_tokens=2048,
        final_answer_tokens=2048,
    ),
}


def get_route_profile(alias: str) -> RouteProfile:
    """Return one fixed route profile or reject an unknown alias."""
    try:
        return DEFAULT_ROUTE_PROFILES[alias]
    except KeyError as exc:
        raise KeyError(f"Unknown model route: {alias}") from exc
