from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

import pytest

from runtime.route_contract_diagnostic import (
    DiagnosticError,
    assess_contract,
    collect_report,
    compare_reports,
    load_protocol,
    validate_report,
)


class FakeClient:
    def __init__(self, subject_name: str) -> None:
        self.protocol = load_protocol()
        self.subject = self.protocol.subjects[subject_name]
        self.payloads: list[dict[str, Any]] = []

    def get_json(self, url: str, *, timeout: float = 10.0) -> dict[str, Any]:
        del timeout
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/props"):
            return {
                "model_path": f"/models/{self.subject.model_file}",
                "build_info": "b-test",
                "total_slots": 1,
                "default_generation_settings": {"n_ctx": 32768},
            }
        if url.endswith("/v1/models"):
            return {"data": [{"id": self.subject.llama_alias}]}
        raise AssertionError(url)

    def get_text(self, url: str, *, timeout: float = 10.0) -> str:
        del timeout
        assert url.endswith("/metrics")
        return "llamacpp:predicted_tokens_seconds 12.5\n"

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del timeout
        assert url.endswith("/chat/completions")
        self.payloads.append(payload)
        text = "\n".join(message["content"] for message in payload["messages"])
        if "instruction (non-empty string)" in text:
            content = json.dumps(
                {
                    "instruction": "Change the comparison to accept equality.",
                    "editable_files": ["calculator.py"],
                    "acceptance_criteria": ["The equal boundary passes."],
                    "depends_on": [],
                }
            )
            reasoning = "private planner trace"
        elif "verdict (pass, fail, or needs_attention)" in text:
            content = json.dumps(
                {
                    "verdict": "fail",
                    "summary": "The boundary defect remains.",
                    "issues": ["The equal case still fails."],
                    "unrelated_changes": [],
                }
            )
            reasoning = "private reviewer trace"
        else:
            content = "ROUTE_OK"
            reasoning = ""
        return {
            "model": payload["model"],
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
                "prompt_tokens": len(text.split()),
                "completion_tokens": 40 if reasoning else 3,
                "completion_tokens_details": {
                    "reasoning_tokens": 20 if reasoning else 0
                },
            },
        }


class FailingClient(FakeClient):
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del url, payload, timeout
        raise DiagnosticError("provider unavailable")


def rehash(report: dict[str, Any]) -> None:
    unhashed = dict(report)
    unhashed.pop("collection_sha256", None)
    canonical = json.dumps(unhashed, sort_keys=True, separators=(",", ":"))
    report["collection_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()


def test_contract_assessment_separates_schema_from_semantics() -> None:
    reviewer = assess_contract(
        "reviewer",
        json.dumps(
            {
                "verdict": "pass",
                "summary": "Structurally valid but wrong conclusion.",
                "issues": [],
                "unrelated_changes": [],
            }
        ),
    )
    assert reviewer == {
        "json_valid": True,
        "schema_valid": True,
        "task_semantics_valid": False,
        "contract_failures": ["task_semantics_mismatch"],
    }

    planner = assess_contract(
        "planner",
        json.dumps(
            {
                "instruction": "Change the boundary.",
                "editable_files": ["calculator.py", "test_calculator.py"],
                "acceptance_criteria": ["The equal boundary passes."],
                "depends_on": [],
            }
        ),
    )
    assert planner["json_valid"] is True
    assert planner["schema_valid"] is True
    assert planner["task_semantics_valid"] is False


def test_contract_assessment_classifies_non_json_and_schema_mismatch() -> None:
    non_json = assess_contract("planner", "```json\n{}\n```")
    assert non_json["json_valid"] is False
    assert non_json["contract_failures"] == ["non_json_final"]

    schema = assess_contract("reviewer", json.dumps({"verdict": "fail"}))
    assert schema["json_valid"] is True
    assert schema["schema_valid"] is False
    assert schema["contract_failures"] == ["schema_mismatch"]


def test_collect_report_uses_subject_routes_and_does_not_store_text() -> None:
    protocol = load_protocol()
    client = FakeClient("candidate")
    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="amelia-gtx1660-v1",
        implementation_commit="a" * 40,
        client=client,
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )

    assert report["subject"] == "candidate"
    assert len(report["attempts"]) == 15
    assert report["summary"]["planner"]["json_rate"] == 1.0
    assert report["summary"]["planner"]["schema_rate"] == 1.0
    assert report["summary"]["planner"]["task_semantics_rate"] == 1.0
    assert all(payload["model"] == "local-reason" for payload in client.payloads)
    serialized = json.dumps(report)
    assert "private planner trace" not in serialized
    assert "private reviewer trace" not in serialized
    assert "Change the comparison to accept equality" not in serialized
    validate_report(report, protocol)


def test_provider_failures_remain_valid_bounded_evidence() -> None:
    protocol = load_protocol()
    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="test-machine",
        implementation_commit="a" * 40,
        client=FailingClient("candidate"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )

    assert report["summary"]["exact"]["failure_counts"] == {"response_failure": 5}
    assert report["summary"]["planner"]["json_rate"] == 0.0
    validate_report(report, protocol)


def test_baseline_uses_existing_operational_profiles() -> None:
    protocol = load_protocol()
    client = FakeClient("baseline")
    report = collect_report(
        protocol=protocol,
        subject_name="baseline",
        environment_id="amelia-gtx1660-v1",
        implementation_commit="b" * 40,
        client=client,
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )

    planner = next(
        payload
        for payload in client.payloads
        if payload["model"] == "local-plan" and payload["max_tokens"] == 3072
    )
    reviewer = next(
        payload for payload in client.payloads if payload["model"] == "local-review"
    )
    assert planner["temperature"] == 0.0
    assert planner["extra_body"]["thinking_budget_tokens"] == 0
    assert reviewer["max_tokens"] == 2048
    assert report["summary"]["reviewer"]["schema_rate"] == 1.0


def test_compare_reports_requires_same_protocol_and_environment() -> None:
    protocol = load_protocol()
    baseline = collect_report(
        protocol=protocol,
        subject_name="baseline",
        environment_id="amelia-gtx1660-v1",
        implementation_commit="c" * 40,
        client=FakeClient("baseline"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )
    candidate = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="amelia-gtx1660-v1",
        implementation_commit="c" * 40,
        client=FakeClient("candidate"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )

    comparison = compare_reports(
        protocol=protocol,
        baseline_value=baseline,
        candidate_value=candidate,
    )
    assert comparison["qualification_claim"] is None
    assert (
        comparison["comparison"]["planner"]["candidate_minus_baseline"]["schema_rate"]
        == 0.0
    )

    candidate["environment_id"] = "different-machine"
    rehash(candidate)
    with pytest.raises(DiagnosticError, match="different environment"):
        compare_reports(
            protocol=protocol,
            baseline_value=baseline,
            candidate_value=candidate,
        )


def test_validate_report_rejects_tampering() -> None:
    protocol = load_protocol()
    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="test-machine",
        implementation_commit="d" * 40,
        client=FakeClient("candidate"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )
    report["summary"]["planner"]["schema_rate"] = 0.0
    with pytest.raises(DiagnosticError, match="collection_sha256"):
        validate_report(report, protocol)


def test_validate_report_rejects_rehashed_summary_drift() -> None:
    protocol = load_protocol()
    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="test-machine",
        implementation_commit="d" * 40,
        client=FakeClient("candidate"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )
    report["summary"]["planner"]["schema_rate"] = 0.0
    rehash(report)
    with pytest.raises(DiagnosticError, match="summary does not match"):
        validate_report(report, protocol)


def test_validate_report_rejects_rehashed_attempt_inconsistency() -> None:
    protocol = load_protocol()
    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="test-machine",
        implementation_commit="e" * 40,
        client=FakeClient("candidate"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )
    planner = report["attempts"][5]
    planner["schema_valid"] = False
    planner["task_semantics_valid"] = False
    planner["contract_failures"] = []
    rehash(report)
    with pytest.raises(DiagnosticError, match="invalid schema requires"):
        validate_report(report, protocol)


def test_validate_report_rejects_rehashed_profile_drift() -> None:
    protocol = load_protocol()
    report = collect_report(
        protocol=protocol,
        subject_name="candidate",
        environment_id="test-machine",
        implementation_commit="f" * 40,
        client=FakeClient("candidate"),
        litellm_base_url="http://litellm/v1",
        llama_base_url="http://llama",
    )
    report["profiles"]["planner"]["temperature"] = 0.0
    rehash(report)
    with pytest.raises(DiagnosticError, match="profiles do not match"):
        validate_report(report, protocol)


def test_service_identity_rejects_wrong_subject_model() -> None:
    protocol = load_protocol()
    client = FakeClient("candidate")
    client.subject = protocol.subjects["baseline"]
    with pytest.raises(DiagnosticError, match="Active model does not match"):
        collect_report(
            protocol=protocol,
            subject_name="candidate",
            environment_id="test-machine",
            implementation_commit="e" * 40,
            client=client,
            litellm_base_url="http://litellm/v1",
            llama_base_url="http://llama",
        )
