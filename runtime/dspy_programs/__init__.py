"""Typed DSPy role programs used behind fixed local-coder adapters."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .gepa_dataset import (
    GepaDatasetError,
    build_gepa_examples,
    export_gepa_dataset,
    load_gepa_dataset,
    to_dspy_examples,
)
from .gepa_runner import (
    GepaRunnerError,
    assess_dataset_readiness,
    build_gepa_metric,
    run_gepa_optimization,
    to_role_dspy_examples,
)

_LAZY_EXPORTS = {
    "ExplorerProgram": (".explorer", "ExplorerProgram"),
    "ExplorerSignature": (".explorer", "ExplorerSignature"),
    "run_explorer_program": (".explorer", "run_explorer_program"),
    "AtomicEditSpec": (".implementer", "AtomicEditSpec"),
    "ImplementerProgram": (".implementer", "ImplementerProgram"),
    "ImplementerSignature": (".implementer", "ImplementerSignature"),
    "run_implementer_program": (".implementer", "run_implementer_program"),
    "PlannerProgram": (".planner", "PlannerProgram"),
    "PlannerSignature": (".planner", "PlannerSignature"),
    "run_planner_program": (".planner", "run_planner_program"),
    "RepairerProgram": (".repairer", "RepairerProgram"),
    "RepairerSignature": (".repairer", "RepairerSignature"),
    "run_repairer_program": (".repairer", "run_repairer_program"),
    "ReviewerProgram": (".reviewer", "ReviewerProgram"),
    "ReviewerSignature": (".reviewer", "ReviewerSignature"),
    "run_reviewer_program": (".reviewer", "run_reviewer_program"),
}


def __getattr__(name: str) -> Any:
    """Load DSPy-backed role modules only when their exports are requested."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


__all__ = [
    *_LAZY_EXPORTS,
    "GepaDatasetError",
    "GepaRunnerError",
    "assess_dataset_readiness",
    "build_gepa_metric",
    "build_gepa_examples",
    "export_gepa_dataset",
    "load_gepa_dataset",
    "to_dspy_examples",
    "to_role_dspy_examples",
    "run_gepa_optimization",
]
