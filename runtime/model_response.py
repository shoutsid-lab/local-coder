"""Normalize final, reasoning, tool, and usage fields from model responses."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ROUTE_OK = "route_ok"
TOOL_CALL_OK = "tool_call_ok"
REASONING_ONLY_TRUNCATED = "reasoning_only_truncated"
EMPTY_COMPLETION = "empty_completion"
MALFORMED_FINAL = "malformed_final"
PROVIDER_ERROR = "provider_error"

_TERMINAL_OUTCOMES = frozenset(
    {
        ROUTE_OK,
        TOOL_CALL_OK,
        REASONING_ONLY_TRUNCATED,
        EMPTY_COMPLETION,
        MALFORMED_FINAL,
        PROVIDER_ERROR,
    }
)


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _details_value(container: Any, details_name: str, field_name: str) -> Any:
    details = _read(container, details_name)
    return _read(details, field_name)


def _integer(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _tool_calls(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    return (value,)


def _provider_field(value: Any, name: str) -> Any:
    return _read(_read(value, "provider_specific_fields"), name)


def _envelope(response: Any) -> Any:
    raw = _read(response, "raw")
    if _read(raw, "choices") is not None:
        return raw
    return response


def _choice_and_message(response: Any) -> tuple[Any, Any, Any]:
    envelope = _envelope(response)
    choices = _read(envelope, "choices")
    if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)):
        choice = choices[0] if choices else None
        message = _read(choice, "message")
        return envelope, choice, message
    return envelope, None, response


def _classify(
    *,
    content: str,
    reasoning_content: str,
    tool_calls: tuple[Any, ...],
    finish_reason: str | None,
    expected_content: str | None,
    accept_tool_calls: bool,
) -> str:
    final = content.strip()
    reasoning = reasoning_content.strip()
    if final:
        if expected_content is None or final == expected_content:
            return ROUTE_OK
        return MALFORMED_FINAL
    if tool_calls:
        return TOOL_CALL_OK if accept_tool_calls else MALFORMED_FINAL
    if reasoning and finish_reason == "length":
        return REASONING_ONLY_TRUNCATED
    if reasoning:
        return MALFORMED_FINAL
    return EMPTY_COMPLETION


@dataclass(frozen=True)
class NormalizedModelResponse:
    """One provider-neutral response view without retaining reasoning text in audit."""

    content: str
    reasoning_content: str
    tool_calls: tuple[Any, ...]
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    model: str | None
    provider: str | None
    system_fingerprint: str | None
    outcome: str
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _TERMINAL_OUTCOMES:
            raise ValueError(f"Unsupported model response outcome: {self.outcome}")

    @property
    def reasoning_present(self) -> bool:
        return bool(self.reasoning_content.strip())

    @property
    def reasoning_sha256(self) -> str | None:
        if not self.reasoning_present:
            return None
        return hashlib.sha256(self.reasoning_content.encode("utf-8")).hexdigest()

    def bounded_metadata(self, *, include_success: bool = False) -> dict[str, Any]:
        """Return audit-safe metadata without the reasoning trace itself."""
        ordinary_success = self.outcome in {ROUTE_OK, TOOL_CALL_OK}
        if ordinary_success and not self.reasoning_present and not include_success:
            return {}
        metadata: dict[str, Any] = {
            "response_outcome": self.outcome,
            "final_content_present": bool(self.content.strip()),
            "final_content_chars": len(self.content),
            "reasoning_present": self.reasoning_present,
            "reasoning_chars": len(self.reasoning_content),
            "tool_call_count": len(self.tool_calls),
        }
        optional = {
            "finish_reason": self.finish_reason,
            "reasoning_sha256": self.reasoning_sha256,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "model": self.model,
            "provider": self.provider,
            "system_fingerprint": self.system_fingerprint,
            "error_type": self.error_type,
        }
        metadata.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        return metadata

    def diagnostic(self, *, route: str | None = None) -> str:
        """Return one actionable summary without exposing reasoning content."""
        prefix = f"{route} " if route else ""
        fields = [
            f"outcome={self.outcome}",
            f"finish_reason={self.finish_reason or 'unknown'}",
            f"final_content_present={bool(self.content.strip())}",
            f"reasoning_present={self.reasoning_present}",
            f"tool_calls={len(self.tool_calls)}",
        ]
        if self.completion_tokens is not None:
            fields.append(f"completion_tokens={self.completion_tokens}")
        if self.outcome == REASONING_ONLY_TRUNCATED:
            fields.append("action=disable thinking for exact probes or raise the bound")
        return prefix + "model response " + "; ".join(fields)


def normalize_model_response(
    response: Any,
    *,
    expected_content: str | None = None,
    accept_tool_calls: bool = True,
) -> NormalizedModelResponse:
    """Normalize OpenAI, LiteLLM, DSPy-adjacent, or smolagents response shapes."""
    envelope, choice, message = _choice_and_message(response)
    message_provider = _read(message, "provider_specific_fields")
    choice_provider = _read(choice, "provider_specific_fields")
    envelope_provider = _read(envelope, "provider_specific_fields")

    content = _text(_read(message, "content"))
    reasoning_content = _first_text(
        _read(message, "reasoning_content"),
        _read(message_provider, "reasoning_content"),
        _read(choice, "reasoning_content"),
        _read(choice_provider, "reasoning_content"),
        _read(envelope, "reasoning_content"),
        _read(envelope_provider, "reasoning_content"),
    )
    calls = _tool_calls(_read(message, "tool_calls"))
    if not calls:
        calls = _tool_calls(_read(message, "function_call"))
    if not calls:
        calls = _tool_calls(_read(response, "tool_calls"))

    finish_reason_value = _read(choice, "finish_reason")
    if finish_reason_value is None:
        finish_reason_value = _read(response, "finish_reason")
    finish_reason = (
        finish_reason_value if isinstance(finish_reason_value, str) else None
    )

    usage = _read(envelope, "usage")
    if usage is None:
        usage = _read(response, "token_usage")
    prompt_tokens = _integer(
        _read(usage, "prompt_tokens"),
        _read(usage, "input_tokens"),
    )
    completion_tokens = _integer(
        _read(usage, "completion_tokens"),
        _read(usage, "output_tokens"),
    )
    cached_tokens = _integer(
        _details_value(usage, "prompt_tokens_details", "cached_tokens"),
        _details_value(usage, "input_tokens_details", "cached_tokens"),
        _read(usage, "cached_tokens"),
    )
    reasoning_tokens = _integer(
        _details_value(usage, "completion_tokens_details", "reasoning_tokens"),
        _details_value(usage, "output_tokens_details", "reasoning_tokens"),
        _read(usage, "reasoning_tokens"),
    )

    model_value = _read(envelope, "model") or _read(response, "model_id")
    provider_value = _read(envelope, "provider") or _provider_field(
        envelope, "provider"
    )
    fingerprint_value = _read(envelope, "system_fingerprint")
    model = model_value if isinstance(model_value, str) else None
    provider = provider_value if isinstance(provider_value, str) else None
    fingerprint = fingerprint_value if isinstance(fingerprint_value, str) else None
    outcome = _classify(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=calls,
        finish_reason=finish_reason,
        expected_content=expected_content,
        accept_tool_calls=accept_tool_calls,
    )
    return NormalizedModelResponse(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=calls,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        model=model,
        provider=provider,
        system_fingerprint=fingerprint,
        outcome=outcome,
    )


def normalize_provider_error(
    error: BaseException,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> NormalizedModelResponse:
    """Represent a transport/provider exception without persisting its message."""
    return NormalizedModelResponse(
        content="",
        reasoning_content="",
        tool_calls=(),
        finish_reason=None,
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
        reasoning_tokens=None,
        model=model,
        provider=provider,
        system_fingerprint=None,
        outcome=PROVIDER_ERROR,
        error_type=type(error).__name__,
    )
