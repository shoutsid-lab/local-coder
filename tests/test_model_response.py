import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.editor import EditorError, request_edits
from runtime.live_e2e import (
    EXACT_PROBE_MAX_TOKENS,
    EXACT_PROBE_THINKING_BUDGET,
    REASONING_PROBE_MAX_TOKENS,
    REASONING_PROBE_THINKING_BUDGET,
    reasoning_capability_probe,
    route_probe,
)
from runtime.model_response import (
    EMPTY_COMPLETION,
    MALFORMED_FINAL,
    PROVIDER_ERROR,
    REASONING_ONLY_TRUNCATED,
    ROUTE_OK,
    TOOL_CALL_OK,
    normalize_model_response,
    normalize_provider_error,
)
from runtime.models import AuditedModel
from runtime.state import StateStore

OBSERVED_REASONING_ONLY_RESPONSE = {
    "id": "chatcmpl-probe",
    "model": "local-fast",
    "system_fingerprint": "build-1",
    "choices": [
        {
            "finish_reason": "length",
            "message": {
                "content": "",
                "role": "assistant",
                "reasoning_content": (
                    "The user wants me to reply with exactly ROUTE_OK."
                ),
                "provider_specific_fields": {"refusal": None},
            },
        }
    ],
    "usage": {
        "completion_tokens": 16,
        "prompt_tokens": 47,
        "prompt_tokens_details": {"cached_tokens": 43},
    },
}


def test_observed_reasoning_only_response_is_classified_without_substitution() -> None:
    normalized = normalize_model_response(
        OBSERVED_REASONING_ONLY_RESPONSE,
        expected_content="ROUTE_OK",
        accept_tool_calls=False,
    )

    assert normalized.outcome == REASONING_ONLY_TRUNCATED
    assert normalized.content == ""
    assert normalized.reasoning_present is True
    assert normalized.finish_reason == "length"
    assert normalized.prompt_tokens == 47
    assert normalized.completion_tokens == 16
    assert normalized.cached_tokens == 43
    assert normalized.model == "local-fast"


def test_provider_specific_reasoning_is_normalized() -> None:
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "final",
                    "provider_specific_fields": {
                        "reasoning_content": "private reasoning"
                    },
                },
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 7,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }

    normalized = normalize_model_response(response)

    assert normalized.outcome == ROUTE_OK
    assert normalized.content == "final"
    assert normalized.reasoning_content == "private reasoning"
    assert normalized.reasoning_tokens == 5


def test_bounded_metadata_hashes_but_does_not_retain_reasoning() -> None:
    normalized = normalize_model_response(OBSERVED_REASONING_ONLY_RESPONSE)

    metadata = normalized.bounded_metadata()

    assert metadata["response_outcome"] == REASONING_ONLY_TRUNCATED
    assert metadata["reasoning_present"] is True
    assert metadata["reasoning_chars"] == len(normalized.reasoning_content)
    assert (
        metadata["reasoning_sha256"]
        == hashlib.sha256(normalized.reasoning_content.encode("utf-8")).hexdigest()
    )
    assert normalized.reasoning_content not in json.dumps(metadata)


def test_ordinary_non_reasoning_success_keeps_metadata_unchanged() -> None:
    normalized = normalize_model_response(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )

    assert normalized.outcome == ROUTE_OK
    assert normalized.bounded_metadata() == {}


def test_exact_content_mismatch_is_malformed_final() -> None:
    normalized = normalize_model_response(
        {"choices": [{"message": {"content": "NOT_OK"}}]},
        expected_content="ROUTE_OK",
        accept_tool_calls=False,
    )

    assert normalized.outcome == MALFORMED_FINAL


def test_tool_call_is_only_accepted_when_the_contract_allows_it() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "call-1", "type": "function"}],
                }
            }
        ]
    }

    assert normalize_model_response(response).outcome == TOOL_CALL_OK
    assert (
        normalize_model_response(response, accept_tool_calls=False).outcome
        == MALFORMED_FINAL
    )


def test_empty_completion_is_distinct_from_reasoning_truncation() -> None:
    normalized = normalize_model_response(
        {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
    )

    assert normalized.outcome == EMPTY_COMPLETION
    assert normalized.reasoning_present is False


def test_provider_error_does_not_retain_exception_message() -> None:
    normalized = normalize_provider_error(
        RuntimeError("secret provider body"),
        model="local-plan",
        provider="litellm",
    )

    metadata = normalized.bounded_metadata()
    assert normalized.outcome == PROVIDER_ERROR
    assert metadata["error_type"] == "RuntimeError"
    assert "secret provider body" not in json.dumps(metadata)


def test_route_probe_reports_reasoning_budget_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runtime.live_e2e.post_json",
        lambda *_args, **_kwargs: OBSERVED_REASONING_ONLY_RESPONSE,
    )

    with pytest.raises(RuntimeError) as error:
        route_probe("local-plan")

    message = str(error.value)
    assert "reasoning_only_truncated" in message
    assert "disable thinking for exact probes or raise the bound" in message
    assert "The user wants" not in message


def test_route_probe_returns_bounded_success_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runtime.live_e2e.post_json",
        lambda *_args, **_kwargs: {
            "model": "local-fast",
            "choices": [{"finish_reason": "stop", "message": {"content": "ROUTE_OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    )

    metadata = route_probe("local-fast")

    assert metadata == {
        "response_outcome": ROUTE_OK,
        "final_content_present": True,
        "final_content_chars": 8,
        "reasoning_present": False,
        "reasoning_chars": 0,
        "tool_call_count": 0,
        "finish_reason": "stop",
        "model": "local-fast",
        "probe_kind": "exact",
        "probe_max_tokens": EXACT_PROBE_MAX_TOKENS,
        "probe_reasoning_enabled": False,
        "probe_thinking_budget_tokens": EXACT_PROBE_THINKING_BUDGET,
    }


def test_native_editor_rejects_reasoning_only_response_and_audits_hash() -> None:
    response = io.BytesIO(json.dumps(OBSERVED_REASONING_ONLY_RESPONSE).encode("utf-8"))
    metrics: list[dict[str, object]] = []

    with (
        patch("runtime.editor.urllib.request.urlopen", return_value=response),
        pytest.raises(EditorError, match="reasoning_only_truncated"),
    ):
        request_edits(
            instruction="Replace one exact word",
            contents={"README.md": "before\n"},
            task="Change before to after",
            metrics_callback=lambda **values: metrics.append(values),
        )

    metadata = metrics[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["response_outcome"] == REASONING_ONLY_TRUNCATED
    assert metadata["reasoning_present"] is True
    assert "reasoning_content" not in metadata
    assert "response_excerpt" not in metadata


def test_audited_model_records_reasoning_metadata_without_trace(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "agent.db")
    run_id = store.create_run(
        task="reasoning",
        mode="agentic",
        repository=tmp_path,
        base_branch="main",
    )
    response = SimpleNamespace(
        content="final",
        reasoning_content="private reasoning",
        finish_reason="stop",
        token_usage=SimpleNamespace(input_tokens=4, output_tokens=6),
    )
    model = AuditedModel(
        SimpleNamespace(generate=lambda *_args, **_kwargs: response),
        route="local-plan",
        state=store,
        run_id=run_id,
    )

    assert model.generate([]) is response

    metric = store.run_details(run_id)["model_metrics"][0]
    metadata = json.loads(metric["metadata"])
    assert metadata["response_outcome"] == ROUTE_OK
    assert metadata["reasoning_present"] is True
    assert metadata["reasoning_chars"] == len("private reasoning")
    assert "private reasoning" not in metric["metadata"]


def test_exact_route_probe_disables_reasoning_in_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_post(_url: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "model": "local-plan",
            "choices": [{"finish_reason": "stop", "message": {"content": "ROUTE_OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }

    monkeypatch.setattr("runtime.live_e2e.post_json", fake_post)

    route_probe("local-plan")

    assert captured["max_tokens"] == EXACT_PROBE_MAX_TOKENS
    assert captured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "thinking_budget_tokens": EXACT_PROBE_THINKING_BUDGET,
    }


def test_exact_route_probe_reports_ignored_reasoning_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runtime.live_e2e.post_json",
        lambda *_args, **_kwargs: OBSERVED_REASONING_ONLY_RESPONSE,
    )

    with pytest.raises(RuntimeError) as error:
        route_probe("local-plan")

    message = str(error.value)
    assert "exact_probe_reasoning_disabled=true" in message
    assert "verify template control support before raising the bound" in message


def test_reasoning_capability_probe_uses_bounded_thinking_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_post(_url: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {
            "model": "local-reason",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "REASON_OK",
                        "reasoning_content": "One plus one combines two units.",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 18,
                "completion_tokens_details": {"reasoning_tokens": 11},
            },
        }

    monkeypatch.setattr("runtime.live_e2e.post_json", fake_post)

    metadata = reasoning_capability_probe("local-reason")

    assert captured["max_tokens"] == REASONING_PROBE_MAX_TOKENS
    assert captured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_budget_tokens": REASONING_PROBE_THINKING_BUDGET,
    }
    assert metadata["probe_kind"] == "reasoning"
    assert metadata["reasoning_present"] is True
    assert metadata["reasoning_chars"] == 32
    assert metadata["reasoning_tokens"] == 11
    assert "reasoning_content" not in metadata


def test_reasoning_capability_probe_requires_observable_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runtime.live_e2e.post_json",
        lambda *_args, **_kwargs: {
            "model": "local-reason",
            "choices": [{"finish_reason": "stop", "message": {"content": "REASON_OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    )

    with pytest.raises(RuntimeError, match="without observable reasoning_content"):
        reasoning_capability_probe("local-reason")


def test_reasoning_capability_probe_rejects_reasoning_only_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runtime.live_e2e.post_json",
        lambda *_args, **_kwargs: OBSERVED_REASONING_ONLY_RESPONSE,
    )

    with pytest.raises(RuntimeError, match="reasoning_only_truncated"):
        reasoning_capability_probe("local-reason")
