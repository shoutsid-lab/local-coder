from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import runtime.route_qualification_collect as collector
from runtime.route_qualification import load_policy
from runtime.route_qualification_collect import (
    CollectionError,
    ServiceIdentity,
    build_context_messages,
    collect_focused_evidence,
    git_commit,
    parse_generation_throughput,
    schema_valid,
    write_report,
)


class FakeClient:
    def __init__(self, *, model_file: str | None = None) -> None:
        policy = load_policy()
        self.model_file = model_file or policy.candidate_model
        self.chat_payloads: list[dict[str, Any]] = []

    def get_json(self, url: str, *, timeout: float = 10.0) -> dict[str, Any]:
        del timeout
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/props"):
            return {
                "model_path": f"/models/{self.model_file}",
                "build_info": "b999-test",
                "total_slots": 1,
                "default_generation_settings": {"n_ctx": 16384},
            }
        if url.endswith("/v1/models"):
            return {"data": [{"id": "local-reason"}]}
        raise AssertionError(url)

    def get_text(self, url: str, *, timeout: float = 10.0) -> str:
        del timeout
        assert url.endswith("/metrics")
        return "llamacpp:predicted_tokens_seconds 9.5\n"

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del timeout
        if url.endswith("/apply-template"):
            prompt = "\n".join(message["content"] for message in payload["messages"])
            return {"prompt": prompt}
        if url.endswith("/tokenize"):
            content = payload["content"]
            return {"tokens": list(range(len(content.split())))}
        if not url.endswith("/chat/completions"):
            raise AssertionError(url)

        self.chat_payloads.append(payload)
        messages = payload["messages"]
        text = "\n".join(message["content"] for message in messages)
        if "instruction (non-empty string)" in text:
            content = json.dumps(
                {
                    "instruction": "Change the boundary comparison only.",
                    "editable_files": ["calculator.py"],
                    "acceptance_criteria": ["The equal-boundary test passes."],
                    "depends_on": [],
                }
            )
            reasoning = "bounded planner reasoning"
            completion_tokens = 120
        elif "verdict (pass, fail, or needs_attention)" in text:
            content = json.dumps(
                {
                    "verdict": "fail",
                    "summary": "calculator.py still excludes the equal case.",
                    "issues": ["calculator.py: the required boundary remains wrong."],
                    "unrelated_changes": [],
                }
            )
            reasoning = "bounded reviewer reasoning"
            completion_tokens = 90
        else:
            content = "ROUTE_OK"
            reasoning = ""
            completion_tokens = 4
        prompt_tokens = len(text.split())
        return {
            "model": "local-reason",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": content,
                        "reasoning_content": reasoning,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "completion_tokens_details": {
                    "reasoning_tokens": completion_tokens // 2 if reasoning else 0
                },
            },
        }


class FakeSampler:
    def __init__(self, pid: int, peak_vram_mib: float | None) -> None:
        self.pid = pid
        self.peak_vram_mib = peak_vram_mib or 5100.0
        self.peak_system_memory_mib = 6200.0
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def fake_identity() -> ServiceIdentity:
    policy = load_policy()
    return ServiceIdentity(
        model_file=policy.candidate_model,
        model_alias=policy.candidate_route,
        build_info="b999-test",
        configured_context_tokens=16384,
        total_slots=1,
        server_pid=os.getpid(),
    )


def test_focused_collection_uses_frozen_contract_counts_and_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_policy()
    client = FakeClient()
    monkeypatch.setattr(collector, "inspect_service", lambda **kwargs: fake_identity())

    report = collect_focused_evidence(
        policy=policy,
        environment_id="amelia-gtx1660-v1",
        implementation_commit="a" * 40,
        client=client,
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
        server_pid=None,
        startup_seconds=None,
        model_switch_seconds=None,
        peak_vram_mib=None,
        sampler_factory=FakeSampler,
    )

    runs = report["contract_runs"]
    assert len(runs) == 16
    assert sum(run["suite"] == "exact" for run in runs) == 6
    assert sum(run["suite"] == "planner" for run in runs) == 5
    assert sum(run["suite"] == "reviewer" for run in runs) == 5
    assert report["resources"]["context_tokens_tested"] >= 8192
    assert report["pending"] == [
        "track_g_role_cases",
        "startup_seconds",
        "model_switch_seconds",
    ]

    planner_payload = next(
        payload
        for payload in client.chat_payloads
        if "instruction (non-empty string)"
        in "\n".join(message["content"] for message in payload["messages"])
    )
    reviewer_payload = next(
        payload
        for payload in client.chat_payloads
        if "verdict (pass, fail, or needs_attention)"
        in "\n".join(message["content"] for message in payload["messages"])
    )
    assert planner_payload["max_tokens"] == 2048
    assert planner_payload["temperature"] == 0.6
    assert planner_payload["top_p"] == 0.95
    assert planner_payload["extra_body"]["top_k"] == 20
    assert planner_payload["extra_body"]["repeat_penalty"] == 1.05
    assert planner_payload["extra_body"]["thinking_budget_tokens"] == 1024
    assert reviewer_payload["max_tokens"] == 1536
    assert reviewer_payload["extra_body"]["thinking_budget_tokens"] == 768


def test_collection_rechecks_service_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = [
        fake_identity(),
        ServiceIdentity(
            model_file=load_policy().candidate_model,
            model_alias="changed-route",
            build_info="b999-test",
            configured_context_tokens=16384,
            total_slots=1,
            server_pid=os.getpid(),
        ),
    ]

    def inspect(**kwargs: Any) -> ServiceIdentity:
        del kwargs
        return identities.pop(0)

    monkeypatch.setattr(collector, "inspect_service", inspect)
    with pytest.raises(CollectionError, match="identity changed"):
        collect_focused_evidence(
            policy=load_policy(),
            environment_id="test-machine",
            implementation_commit="c" * 40,
            client=FakeClient(),
            litellm_base_url="http://litellm/v1",
            llama_base_url="http://llama",
            server_pid=None,
            startup_seconds=None,
            model_switch_seconds=None,
            peak_vram_mib=None,
            sampler_factory=FakeSampler,
        )


def test_collection_does_not_persist_final_or_reasoning_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "inspect_service", lambda **kwargs: fake_identity())
    report = collect_focused_evidence(
        policy=load_policy(),
        environment_id="test-machine",
        implementation_commit="b" * 40,
        client=FakeClient(),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
        server_pid=None,
        startup_seconds=12.0,
        model_switch_seconds=20.0,
        peak_vram_mib=5000.0,
        sampler_factory=FakeSampler,
    )

    serialized = json.dumps(report)
    assert "bounded planner reasoning" not in serialized
    assert "bounded reviewer reasoning" not in serialized
    assert "Change the boundary comparison only" not in serialized
    assert report["pending"] == ["track_g_role_cases"]
    assert all(
        set(run)
        == {
            "attempt_id",
            "suite",
            "thinking_enabled",
            "response_outcome",
            "final_answer_present",
            "schema_valid",
            "reasoning_present",
            "malformed_tool_call",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "latency_seconds",
            "generated_tokens_per_second",
        }
        for run in report["contract_runs"]
    )


def test_schema_validation_matches_role_contracts() -> None:
    assert schema_valid("exact", "ROUTE_OK") is True
    assert schema_valid("exact", "ROUTE_OK extra") is False
    assert (
        schema_valid(
            "planner",
            json.dumps(
                {
                    "instruction": "Change one comparison.",
                    "editable_files": ["calculator.py"],
                    "acceptance_criteria": ["The boundary test passes."],
                    "depends_on": [],
                }
            ),
        )
        is True
    )
    assert (
        schema_valid(
            "reviewer",
            json.dumps(
                {
                    "verdict": "pass",
                    "summary": "Incorrect verdict.",
                    "issues": [],
                    "unrelated_changes": [],
                }
            ),
        )
        is False
    )


def test_generation_throughput_parser_accepts_labelled_metrics() -> None:
    metrics = "\n".join(
        [
            "# HELP llamacpp:predicted_tokens_seconds Average generation throughput",
            'llamacpp:predicted_tokens_seconds{model="local-reason"} 7.25',
        ]
    )
    assert parse_generation_throughput(metrics) == 7.25
    assert parse_generation_throughput("unrelated_metric 1") is None


def test_context_builder_reaches_target_without_exceeding_bound() -> None:
    messages, tokens = build_context_messages(
        client=FakeClient(),
        llama_base_url="http://llama",
        target_tokens=200,
        maximum_tokens=240,
    )
    assert tokens >= 200
    assert tokens <= 240
    assert messages[-1]["content"].endswith("Reply with exactly ROUTE_OK.")


def test_service_identity_rejects_wrong_model_before_process_lookup() -> None:
    policy = load_policy()
    with pytest.raises(CollectionError, match="does not match the frozen policy"):
        collector.inspect_service(
            client=FakeClient(model_file="wrong-model.gguf"),
            llama_base_url="http://llama",
            policy=policy,
            server_pid=None,
        )


def test_vram_override_avoids_unavailable_wsl_process_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("nvidia-smi must not be called")

    monkeypatch.setattr(subprocess, "run", unavailable)
    sampler = collector.ResourceSampler(os.getpid(), 4999.0)
    sampler.sample_once()

    assert sampler.peak_vram_mib == 4999.0
    assert sampler.peak_system_memory_mib > 0


def test_vram_override_rejects_zero() -> None:
    with pytest.raises(CollectionError, match="must be greater than zero"):
        collector.ResourceSampler(os.getpid(), 0.0)


def test_write_report_is_atomic_and_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    write_report(path, {"value": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}
    with pytest.raises(CollectionError, match="Refusing to overwrite"):
        write_report(path, {"value": 2})


def test_write_report_race_creates_only_one_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "report.json"
    barrier = threading.Barrier(2)
    original_exists = Path.exists

    def synchronized_exists(candidate: Path) -> bool:
        if candidate == path:
            barrier.wait(timeout=5)
            return False
        return original_exists(candidate)

    monkeypatch.setattr(Path, "exists", synchronized_exists)

    def attempt(value: int) -> str:
        try:
            write_report(path, {"value": value})
        except CollectionError:
            return "blocked"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (1, 2)))

    assert sorted(outcomes) == ["blocked", "written"]
    assert json.loads(path.read_text(encoding="utf-8"))["value"] in {1, 2}


def test_provider_failure_is_recorded_without_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_policy()
    client = FakeClient()

    def fail_post(
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del url, payload, timeout
        raise CollectionError("secret provider detail")

    monkeypatch.setattr(client, "post_json", fail_post)
    record, source = collector.run_contract_attempt(
        client=client,
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
        policy=policy,
        suite="planner",
        index=1,
    )

    assert record["response_outcome"] == "provider_error"
    assert record["prompt_tokens"] == 0
    assert record["completion_tokens"] == 0
    assert source == "request_lower_bound"
    assert "secret provider detail" not in json.dumps(record)


def test_unexpected_request_failure_is_not_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()

    def fail_post(
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del url, payload, timeout
        raise RuntimeError("collector defect")

    monkeypatch.setattr(client, "post_json", fail_post)
    with pytest.raises(RuntimeError, match="collector defect"):
        collector.run_contract_attempt(
            client=client,
            litellm_base_url="http://litellm/v1",
            llama_base_url="http://llama",
            policy=load_policy(),
            suite="planner",
            index=1,
        )


def test_git_commit_requires_clean_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    assert len(git_commit(tmp_path)) == 40
    tracked.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(CollectionError, match="committed, clean working tree"):
        git_commit(tmp_path)
