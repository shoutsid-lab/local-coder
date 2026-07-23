from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from runtime.dspy_lm import build_dspy_lm
from runtime.models import ModelRegistry
from runtime.route_profiles import (
    DEFAULT_ROUTE_PROFILES,
    REASONING_PROFILE_EXAMPLES,
    RouteProfile,
    get_route_profile,
)


def test_default_routes_preserve_existing_role_policies() -> None:
    assert set(DEFAULT_ROUTE_PROFILES) == {
        "local-fast",
        "local-plan",
        "local-review",
        "local-reason",
    }
    assert DEFAULT_ROUTE_PROFILES["local-fast"].max_tokens == 2048
    assert DEFAULT_ROUTE_PROFILES["local-plan"].max_tokens == 3072
    assert DEFAULT_ROUTE_PROFILES["local-review"].max_tokens == 2048
    for alias in ("local-fast", "local-plan", "local-review"):
        profile = DEFAULT_ROUTE_PROFILES[alias]
        assert profile.reasoning_mode == "off"
        assert profile.reasoning_tokens == 0
        assert not profile.requires_model_switch


def test_local_reason_is_additive_and_operator_managed() -> None:
    profile = get_route_profile("local-reason")

    assert profile.model_alias == "local-reason"
    assert profile.reasoning_mode == "on"
    assert profile.max_tokens == 2048
    assert profile.reasoning_tokens == 1024
    assert profile.final_answer_tokens == 1024
    assert profile.temperature == 0.6
    assert profile.top_p == 0.95
    assert profile.top_k == 20
    assert profile.repetition_penalty == 1.05
    assert profile.timeout_seconds == 300
    assert profile.retries == 0
    assert not profile.preserve_reasoning
    assert profile.requires_model_switch


def test_reasoning_profile_examples_are_bounded() -> None:
    exact = REASONING_PROFILE_EXAMPLES["exact-probe"]
    planner = REASONING_PROFILE_EXAMPLES["planner"]
    reviewer = REASONING_PROFILE_EXAMPLES["reviewer"]
    diagnostic = REASONING_PROFILE_EXAMPLES["diagnostic"]

    assert exact.reasoning_mode == "off"
    assert (exact.reasoning_tokens, exact.final_answer_tokens) == (0, 64)
    assert (planner.reasoning_tokens, planner.final_answer_tokens) == (1024, 1024)
    assert (reviewer.reasoning_tokens, reviewer.final_answer_tokens) == (768, 768)
    assert (diagnostic.reasoning_tokens, diagnostic.final_answer_tokens) == (
        2048,
        2048,
    )
    for profile in REASONING_PROFILE_EXAMPLES.values():
        total = profile.reasoning_tokens + profile.final_answer_tokens
        assert total <= profile.max_tokens


def test_reasoning_request_kwargs_are_explicit_and_bounded() -> None:
    kwargs = get_route_profile("local-reason").request_kwargs()

    assert kwargs == {
        "temperature": 0.6,
        "max_tokens": 2048,
        "top_p": 0.95,
        "timeout": 300,
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": True},
            "thinking_budget_tokens": 1024,
            "top_k": 20,
            "repeat_penalty": 1.05,
        },
    }


def test_existing_route_request_kwargs_remain_minimal() -> None:
    assert get_route_profile("local-fast").request_kwargs() == {
        "temperature": 0.0,
        "max_tokens": 2048,
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking_budget_tokens": 0,
        },
    }


def test_profile_rejects_invalid_combined_budget() -> None:
    base = get_route_profile("local-reason")

    with pytest.raises(ValueError, match="budgets exceed"):
        replace(base, max_tokens=100, reasoning_tokens=80, final_answer_tokens=80)


def test_profile_rejects_reasoning_budget_when_thinking_is_off() -> None:
    base = get_route_profile("local-fast")

    with pytest.raises(ValueError, match="zero reasoning budget"):
        replace(base, reasoning_tokens=1, final_answer_tokens=2047)


def test_model_registry_builds_optional_reasoning_route(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules,
        "smolagents",
        SimpleNamespace(LiteLLMModel=FakeModel),
    )

    ModelRegistry().build("local-reason")

    assert captured["model_id"] == "openai/local-reason"
    assert captured["max_tokens"] == 2048
    assert captured["temperature"] == 0.6
    assert captured["top_p"] == 0.95
    assert captured["timeout"] == 300
    assert captured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_budget_tokens": 1024,
        "top_k": 20,
        "repeat_penalty": 1.05,
    }


def test_dspy_factory_uses_reasoning_route_profile() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def lm(model: str, **kwargs: object) -> SimpleNamespace:
        calls.append((model, kwargs))
        return SimpleNamespace(model=model)

    build_dspy_lm("local-reason", dspy_module=SimpleNamespace(LM=lm))

    model, kwargs = calls[0]
    assert model == "openai/local-reason"
    assert kwargs["temperature"] == 0.6
    assert kwargs["max_tokens"] == 2048
    assert kwargs["top_p"] == 0.95
    assert kwargs["timeout"] == 300
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_budget_tokens": 1024,
        "top_k": 20,
        "repeat_penalty": 1.05,
    }


def test_unknown_route_is_rejected() -> None:
    with pytest.raises(KeyError, match="Unknown model route"):
        get_route_profile("cloud-reason")


def test_route_profile_type_remains_immutable() -> None:
    profile = get_route_profile("local-reason")
    assert isinstance(profile, RouteProfile)
    with pytest.raises(Exception):
        profile.max_tokens = 999  # type: ignore[misc]
