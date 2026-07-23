"""Authorized activation and rollback for promoted DSPy prompt states."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from runtime.state import scorecard_allows_promotion, utc_now

if TYPE_CHECKING:
    from evaluation.prompt_campaign import PromptCampaignSpec
    from runtime.state import StateStore

PROMPT_ACTIVATION_SCHEMA_VERSION = 1
PROMPT_STORE_DIRECTORY = Path(".local-coder") / "prompt-programs"
SUPPORTED_PROMPT_ROLES = {
    "explorer",
    "planner",
    "implementer",
    "repairer",
    "reviewer",
}


class PromptActivationError(ValueError):
    """Raised when prompt deployment lineage or active state is invalid."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def prompt_store_root(
    repository_root: Path | None = None,
    *,
    store_root: Path | None = None,
) -> Path:
    """Return the operator-owned prompt program store."""
    if store_root is not None:
        return store_root.resolve()
    configured = os.environ.get("LOCAL_CODER_PROMPT_STORE")
    if configured:
        return Path(configured).expanduser().resolve()
    root = (
        repository_root.resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[1]
    )
    return (root / PROMPT_STORE_DIRECTORY).resolve()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptActivationError(f"Could not load {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise PromptActivationError(f"{label} must be a JSON object.")
    return payload


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _active_pointer_path(store: Path, role: str) -> Path:
    if role not in SUPPORTED_PROMPT_ROLES:
        raise PromptActivationError(f"Unsupported prompt role: {role}")
    return store / "active" / f"{role}.json"


def _history_directory(store: Path, activation_id: str) -> Path:
    invalid_characters = any(
        character not in "0123456789abcdef" for character in activation_id
    )
    if not activation_id or invalid_characters:
        raise PromptActivationError(
            "Prompt activation ID must be lowercase hexadecimal."
        )
    return store / "history" / activation_id


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _approved_prompt_spec(campaign: Mapping[str, Any]) -> PromptCampaignSpec:
    from evaluation.prompt_campaign import PromptCampaignError, PromptCampaignSpec

    approved = [
        brief
        for brief in campaign.get("briefs", [])
        if brief.get("status") == "approved"
    ]
    if len(approved) != 1:
        raise PromptActivationError("Prompt activation requires one approved brief.")
    metadata_text = approved[0].get("metadata")
    if not isinstance(metadata_text, str):
        raise PromptActivationError("Approved prompt brief has no typed metadata.")
    try:
        metadata = json.loads(metadata_text)
        return PromptCampaignSpec.from_metadata(metadata)
    except (json.JSONDecodeError, PromptCampaignError) as exc:
        raise PromptActivationError(
            "Approved prompt brief metadata is invalid."
        ) from exc


def _candidate_state(
    build: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, str]:
    from evaluation.prompt_campaign import (
        PromptCampaignError,
        load_prompt_candidate_artifact,
    )

    try:
        artifact = load_prompt_candidate_artifact(dict(build))
    except PromptCampaignError as exc:
        raise PromptActivationError(str(exc)) from exc
    output = artifact.get("output")
    expected_hash = artifact.get("gepa_candidate_hash")
    if not isinstance(output, str) or not output:
        raise PromptActivationError("Prompt candidate output path is missing.")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise PromptActivationError("Prompt candidate state hash is missing.")
    candidate_path = Path(output).resolve() / "candidate.json"
    if not candidate_path.is_file():
        raise PromptActivationError("Prompt candidate state file is missing.")
    content = candidate_path.read_bytes()
    actual_hash = _sha256_bytes(content)
    if actual_hash != expected_hash:
        raise PromptActivationError("Prompt candidate state changed after evaluation.")
    try:
        program_state = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromptActivationError("Prompt candidate state is invalid JSON.") from exc
    if not isinstance(program_state, dict):
        raise PromptActivationError("Prompt candidate state must be a JSON object.")
    return artifact, content, actual_hash


def _activation_pointer(metadata: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "schema_version",
        "activation_id",
        "role",
        "campaign_id",
        "build_id",
        "evaluation_id",
        "candidate_instruction_hash",
        "program_state_path",
        "program_hash",
        "previous_activation_id",
        "activated_at",
    )
    pointer = {key: metadata.get(key) for key in keys}
    required = tuple(key for key in keys if key != "previous_activation_id")
    if any(pointer[key] is None for key in required):
        raise PromptActivationError("Prompt activation metadata is incomplete.")
    return pointer


def _validate_pointer_state(
    store: Path,
    role: str,
    pointer: Mapping[str, Any],
) -> dict[str, Any]:
    if pointer.get("schema_version") != PROMPT_ACTIVATION_SCHEMA_VERSION:
        raise PromptActivationError("Active prompt schema version is unsupported.")
    if pointer.get("role") != role:
        raise PromptActivationError("Active prompt pointer role is inconsistent.")
    relative = pointer.get("program_state_path")
    expected_hash = pointer.get("program_hash")
    if not isinstance(relative, str) or not relative:
        raise PromptActivationError("Active prompt state path is missing.")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise PromptActivationError("Active prompt state hash is missing.")
    state_path = (store / relative).resolve()
    history_root = (store / "history").resolve()
    if not _path_within(state_path, history_root):
        raise PromptActivationError("Active prompt state escapes the history store.")
    if not state_path.is_file():
        raise PromptActivationError("Active prompt state file is missing.")
    actual_hash = _sha256_bytes(state_path.read_bytes())
    if actual_hash != expected_hash:
        raise PromptActivationError("Active prompt state hash does not match.")
    result = dict(pointer)
    result["resolved_program_state_path"] = str(state_path)
    return result


def read_active_prompt(
    role: str,
    *,
    repository_root: Path | None = None,
    store_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return and validate one active prompt pointer without loading DSPy."""
    store = prompt_store_root(repository_root, store_root=store_root)
    pointer_path = _active_pointer_path(store, role)
    if not pointer_path.is_file():
        return None
    pointer = _read_object(pointer_path, label="active prompt pointer")
    return _validate_pointer_state(store, role, pointer)


def load_active_prompt_state(
    program: Any,
    role: str,
    *,
    repository_root: Path | None = None,
    store_root: Path | None = None,
) -> dict[str, Any] | None:
    """Load an authorized active state into one DSPy role program, if present."""
    pointer = read_active_prompt(
        role,
        repository_root=repository_root,
        store_root=store_root,
    )
    if pointer is None:
        return None
    loader = getattr(program, "load", None)
    if not callable(loader):
        raise PromptActivationError("DSPy role program cannot load active state.")
    loader(pointer["resolved_program_state_path"])
    return pointer


def activate_prompt_candidate(
    store: StateStore,
    evaluation_id: str,
    *,
    actor: str,
    rationale: str,
    repository_root: Path,
    store_root: Path | None = None,
) -> dict[str, Any]:
    """Atomically activate one explicitly promoted, clean prompt candidate."""
    from evaluation.outcomes import stable_hash

    if not actor.strip() or not rationale.strip():
        raise PromptActivationError("Activation requires an actor and rationale.")
    evaluation = store.evaluation_details(evaluation_id)
    if evaluation is None:
        raise PromptActivationError("Prompt evaluation is missing.")
    campaign_id = evaluation.get("campaign_id")
    build_id = evaluation.get("build_id")
    if not isinstance(campaign_id, str) or not isinstance(build_id, str):
        raise PromptActivationError("Prompt evaluation has incomplete lineage.")
    campaign = store.campaign_details(campaign_id)
    build = store.candidate_build_details(build_id)
    if campaign is None or build is None:
        raise PromptActivationError("Prompt campaign or candidate build is missing.")
    if campaign.get("kind") != "prompt-optimization":
        raise PromptActivationError("Only prompt campaigns may activate program state.")
    if campaign.get("status") != "completed_clean":
        raise PromptActivationError(
            "Prompt campaign must close cleanly before activation."
        )
    if evaluation.get("status") != "completed":
        raise PromptActivationError("Prompt evaluation is not completed.")
    decision = evaluation.get("decision")
    if not isinstance(decision, dict) or decision.get("decision") != "promote":
        raise PromptActivationError("Prompt activation requires a promote decision.")
    try:
        scorecard = json.loads(str(evaluation.get("scorecard")))
    except (json.JSONDecodeError, TypeError) as exc:
        raise PromptActivationError("Prompt evaluation scorecard is invalid.") from exc
    if not scorecard_allows_promotion(scorecard):
        raise PromptActivationError("Prompt scorecard does not permit activation.")
    if build.get("status") != "candidate_ready":
        raise PromptActivationError("Prompt candidate build is not activation-ready.")

    spec = _approved_prompt_spec(campaign)
    artifact, candidate_content, candidate_hash = _candidate_state(build)
    if artifact.get("role") != spec.role:
        raise PromptActivationError("Prompt candidate role differs from its brief.")
    instruction_hash = artifact.get("candidate_instruction_hash")
    if not isinstance(instruction_hash, str) or not instruction_hash:
        raise PromptActivationError("Prompt candidate instruction hash is missing.")

    prompt_root = prompt_store_root(repository_root, store_root=store_root)
    previous = read_active_prompt(spec.role, store_root=prompt_root)
    if (
        previous is not None
        and previous.get("evaluation_id") == evaluation_id
        and previous.get("program_hash") == candidate_hash
    ):
        result = dict(previous)
        result["idempotent"] = True
        return result

    activation_id = uuid.uuid4().hex[:12]
    history = _history_directory(prompt_root, activation_id)
    program_path = history / "program.json"
    metadata_path = history / "metadata.json"
    relative_program_path = str(program_path.relative_to(prompt_root))
    metadata = {
        "schema_version": PROMPT_ACTIVATION_SCHEMA_VERSION,
        "artifact_kind": "prompt_activation",
        "activation_id": activation_id,
        "role": spec.role,
        "campaign_id": campaign_id,
        "build_id": build_id,
        "evaluation_id": evaluation_id,
        "decision_id": decision.get("id"),
        "candidate_instruction_hash": instruction_hash,
        "program_state_path": relative_program_path,
        "program_hash": candidate_hash,
        "previous_activation_id": (
            previous.get("activation_id") if previous is not None else None
        ),
        "actor": actor.strip(),
        "rationale": rationale.strip(),
        "activated_at": utc_now(),
    }
    pointer = _activation_pointer(metadata)
    pointer_path = _active_pointer_path(prompt_root, spec.role)
    previous_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
    history.mkdir(parents=True, exist_ok=False)
    try:
        _write_atomic(program_path, candidate_content)
        _write_atomic(metadata_path, _json_bytes(metadata))
        _write_atomic(pointer_path, _json_bytes(pointer))
        artifact_content = _json_bytes(metadata).decode("utf-8")
        store.add_evaluation_artifact(
            evaluation_id,
            kind="prompt_activation",
            content_hash=stable_hash(metadata),
            content=artifact_content,
        )
    except Exception:
        if previous_pointer is None:
            pointer_path.unlink(missing_ok=True)
        else:
            _write_atomic(pointer_path, previous_pointer)
        shutil.rmtree(history, ignore_errors=True)
        raise
    result = dict(pointer)
    result["idempotent"] = False
    result["metadata_path"] = str(metadata_path)
    return result


def rollback_active_prompt(
    store: StateStore,
    role: str,
    *,
    actor: str,
    rationale: str,
    repository_root: Path,
    store_root: Path | None = None,
) -> dict[str, Any]:
    """Atomically restore the previous authorized state, or the code baseline."""
    from evaluation.outcomes import stable_hash

    if not actor.strip() or not rationale.strip():
        raise PromptActivationError("Rollback requires an actor and rationale.")
    prompt_root = prompt_store_root(repository_root, store_root=store_root)
    current = read_active_prompt(role, store_root=prompt_root)
    if current is None:
        raise PromptActivationError(f"No active prompt exists for role: {role}")
    current_id = current.get("activation_id")
    previous_id = current.get("previous_activation_id")
    if not isinstance(current_id, str):
        raise PromptActivationError("Active prompt activation ID is missing.")
    pointer_path = _active_pointer_path(prompt_root, role)
    old_pointer = pointer_path.read_bytes()
    restored_pointer: dict[str, Any] | None = None
    if previous_id is not None:
        if not isinstance(previous_id, str):
            raise PromptActivationError("Previous activation ID is invalid.")
        metadata_path = _history_directory(prompt_root, previous_id) / "metadata.json"
        previous_metadata = _read_object(
            metadata_path,
            label="previous prompt activation metadata",
        )
        restored_pointer = _activation_pointer(previous_metadata)
        _validate_pointer_state(prompt_root, role, restored_pointer)

    event = {
        "schema_version": PROMPT_ACTIVATION_SCHEMA_VERSION,
        "artifact_kind": "prompt_rollback",
        "role": role,
        "evaluation_id": current.get("evaluation_id"),
        "from_activation_id": current_id,
        "to_activation_id": previous_id,
        "restored": "previous_activation" if previous_id else "code_baseline",
        "actor": actor.strip(),
        "rationale": rationale.strip(),
        "rolled_back_at": utc_now(),
    }
    evaluation_id = current.get("evaluation_id")
    if not isinstance(evaluation_id, str) or not evaluation_id:
        raise PromptActivationError("Active prompt evaluation lineage is missing.")
    try:
        if restored_pointer is None:
            pointer_path.unlink()
        else:
            _write_atomic(pointer_path, _json_bytes(restored_pointer))
        store.add_evaluation_artifact(
            evaluation_id,
            kind="prompt_rollback",
            content_hash=stable_hash(event),
            content=_json_bytes(event).decode("utf-8"),
        )
    except Exception:
        _write_atomic(pointer_path, old_pointer)
        raise
    return event


def active_prompt_inventory(
    *,
    repository_root: Path | None = None,
    store_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return every validated active prompt pointer."""
    inventory = []
    for role in sorted(SUPPORTED_PROMPT_ROLES):
        pointer = read_active_prompt(
            role,
            repository_root=repository_root,
            store_root=store_root,
        )
        if pointer is not None:
            inventory.append(pointer)
    return inventory
