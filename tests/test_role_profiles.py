from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.role_profiles import (
    RoleProfileError,
    activation_summary,
    apply_qualified_instructions,
    load_role_activation,
    role_generation_profile,
    role_route,
)

ROOT = Path(__file__).resolve().parents[1]


ACTIVATION_FILES = (
    "profiles/qwythos-role-activation-v1.json",
    "profiles/track-g-holdout-qualification-v1.json",
    "profiles/track-g-qwythos-prompt-tuning-v1.json",
    "profiles/model-services-v1.json",
    "evidence/track-g/baseline-track-g-holdout-v1-20260724T031908Z.json",
    "evidence/track-g/candidate-track-g-holdout-v1-20260724T032051Z.json",
    "evidence/track-g/qwythos-holdout-qualification-v1.json",
)


def _copy_activation_fixture(destination_root: Path) -> None:
    for relative in ACTIVATION_FILES:
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)


def test_qualified_roles_are_bound_to_frozen_holdout_evidence() -> None:
    activation = load_role_activation(ROOT)

    assert activation.qualification_sha256 == (
        "6ce7a71b5a36917f3f3d56051f73e1ea95e2286bfb9fa1f4ab999d5a505c27c5"
    )
    assert activation.enabled is True
    assert activation.automatic_switching is True
    assert activation.model_service_config_sha256 == (
        "8465c5b65a6217cc85eba08096d62eecc074f305924166fb29a490d8e53dbb77"
    )
    assert role_route("planner", repository_root=ROOT) == "local-reason"
    assert role_route("reviewer", repository_root=ROOT) == "local-reason"
    assert role_route("explorer", repository_root=ROOT) == "local-plan"
    assert role_route("implementer", repository_root=ROOT) == "local-fast"
    assert role_route("repairer", repository_root=ROOT) == "local-fast"

    planner = role_generation_profile("planner", repository_root=ROOT)
    reviewer = role_generation_profile("reviewer", repository_root=ROOT)
    assert (planner.reasoning_tokens, planner.final_answer_tokens) == (1024, 1024)
    assert planner.temperature == 0.6
    assert planner.timeout_seconds == 300
    assert (reviewer.reasoning_tokens, reviewer.final_answer_tokens) == (1536, 1536)
    assert reviewer.temperature == 0.0
    assert reviewer.timeout_seconds == 480


def test_qualified_prompt_is_applied_to_the_runtime_predictor() -> None:
    captured: list[str] = []
    replacement = object()

    class Signature:
        def with_instructions(self, instructions: str) -> object:
            captured.append(instructions)
            return replacement

    predictor = SimpleNamespace(signature=Signature())
    program = SimpleNamespace(named_predictors=lambda: [("predict", predictor)])

    assert apply_qualified_instructions(program, "planner", repository_root=ROOT)
    assert predictor.signature is replacement
    assert "acceptance_criteria" in captured[0]
    assert "out-of-scope path" in captured[0]


def test_activation_summary_exposes_only_bounded_promotion_state() -> None:
    summary = activation_summary(ROOT)

    assert summary["activation_id"] == "qwythos-planner-reviewer-v1"
    assert summary["enabled"] is True
    assert summary["model_service_config_sha256"] == (
        "8465c5b65a6217cc85eba08096d62eecc074f305924166fb29a490d8e53dbb77"
    )
    assert summary["qualified_roles"] == {
        "planner": {
            "route": "local-reason",
            "prompt_profile": "evidence-completeness",
            "active": True,
        },
        "reviewer": {
            "route": "local-reason",
            "prompt_profile": "field-checklist",
            "active": True,
        },
    }
    assert "instructions" not in json.dumps(summary)


def test_tampered_qualification_report_fails_closed(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    report_path = tmp_path / "evidence/track-g/qwythos-holdout-qualification-v1.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["combined_qualified"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="canonical hash"):
        load_role_activation(tmp_path)


def test_activation_without_automatic_switching_fails_closed(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    manifest_path = tmp_path / "profiles/qwythos-role-activation-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["automatic_switching"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="automatic switching"):
        load_role_activation(tmp_path)


def test_tampered_supporting_report_fails_closed(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    report_path = (
        tmp_path / "evidence/track-g/baseline-track-g-holdout-v1-20260724T031908Z.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["environment_id"] = "tampered-environment"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="canonical hash"):
        load_role_activation(tmp_path)


def test_disabled_activation_restores_qwen_routes_and_prompts(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    manifest_path = tmp_path / "profiles/qwythos-role-activation-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["enabled"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    activation = load_role_activation(tmp_path)
    assert activation.enabled is False
    assert role_route("planner", repository_root=tmp_path) == "local-plan"
    assert role_route("reviewer", repository_root=tmp_path) == "local-review"
    assert role_generation_profile("planner", repository_root=tmp_path) == (
        role_generation_profile("explorer", repository_root=tmp_path)
    )

    program = SimpleNamespace(named_predictors=lambda: [])
    assert not apply_qualified_instructions(
        program, "planner", repository_root=tmp_path
    )
    summary = activation_summary(tmp_path)
    assert summary["enabled"] is False
    assert summary["qualified_roles"]["planner"]["active"] is False
    assert summary["qualified_roles"]["reviewer"]["active"] is False


def test_tampered_model_service_policy_fails_closed(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    config_path = tmp_path / "profiles/model-services-v1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["profiles"]["fast-qwen"]["routes"].append("local-reason")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="service config hash"):
        load_role_activation(tmp_path)


def test_rehashed_model_service_policy_edit_still_fails_closed(
    tmp_path: Path,
) -> None:
    _copy_activation_fixture(tmp_path)

    config_path = tmp_path / "profiles/model-services-v1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    arguments = config["profiles"]["reason-qwythos"]["arguments"]
    budget_index = arguments.index("--reasoning-budget") + 1
    arguments[budget_index] = "4096"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    canonical = json.dumps(
        config,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest_path = tmp_path / "profiles/qwythos-role-activation-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_service_config_sha256"] = hashlib.sha256(canonical).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="reason-qwythos launch arguments"):
        load_role_activation(tmp_path)


def test_unqualified_role_cannot_be_promoted_by_manifest_edit(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    manifest_path = tmp_path / "profiles/qwythos-role-activation-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["role_routes"]["implementer"] = "local-reason"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="implementer route"):
        load_role_activation(tmp_path)


def test_fallback_routes_are_bound_to_the_qwen_baseline(tmp_path: Path) -> None:
    _copy_activation_fixture(tmp_path)

    manifest_path = tmp_path / "profiles/qwythos-role-activation-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fallback_role_routes"]["reviewer"] = "local-reason"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RoleProfileError, match="Fallback reviewer route"):
        load_role_activation(tmp_path)
