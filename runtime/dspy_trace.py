"""Append-only audit records for typed DSPy role invocations."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from .state import StateStore

DSPY_TRACE_SCHEMA_VERSION = 1
DSPY_TRACE_ARTIFACT_KIND = "dspy_trace"


def jsonable(value: Any) -> Any:
    """Return a deterministic JSON-compatible representation."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        return jsonable(dumped)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [jsonable(item) for item in value]
    raise TypeError(f"DSPy trace value is not JSON-compatible: {type(value).__name__}")


def build_dspy_trace(
    *,
    role: str,
    program: str,
    route: str,
    inputs: Mapping[str, Any],
    output: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one canonical typed-role trace payload."""
    if not role.strip() or not program.strip() or not route.strip():
        raise ValueError("DSPy trace identity fields must be nonempty.")
    payload: dict[str, Any] = {
        "schema_version": DSPY_TRACE_SCHEMA_VERSION,
        "role": role.strip(),
        "program": program.strip(),
        "adapter": "JSONAdapter",
        "route": route.strip(),
        "inputs": jsonable(inputs),
        "output": jsonable(output),
    }
    if metadata:
        payload["metadata"] = jsonable(metadata)
    return payload


def record_dspy_trace(
    state: StateStore,
    run_id: str,
    *,
    role: str,
    program: str,
    route: str,
    inputs: Mapping[str, Any],
    output: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one typed-role trace as an append-only run artifact."""
    payload = build_dspy_trace(
        role=role,
        program=program,
        route=route,
        inputs=inputs,
        output=output,
        metadata=metadata,
    )
    state.add_artifact(
        run_id,
        kind=DSPY_TRACE_ARTIFACT_KIND,
        content=json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    return payload
