"""Typed DSPy role programs used behind fixed local-coder adapters."""

from .explorer import ExplorerProgram, ExplorerSignature, run_explorer_program
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
from .implementer import (
    AtomicEditSpec,
    ImplementerProgram,
    ImplementerSignature,
    run_implementer_program,
)
from .planner import PlannerProgram, PlannerSignature, run_planner_program
from .repairer import RepairerProgram, RepairerSignature, run_repairer_program
from .reviewer import ReviewerProgram, ReviewerSignature, run_reviewer_program

__all__ = [
    "ExplorerProgram",
    "ExplorerSignature",
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
    "AtomicEditSpec",
    "ImplementerProgram",
    "ImplementerSignature",
    "PlannerProgram",
    "PlannerSignature",
    "RepairerProgram",
    "RepairerSignature",
    "ReviewerProgram",
    "ReviewerSignature",
    "run_explorer_program",
    "run_implementer_program",
    "run_planner_program",
    "run_repairer_program",
    "run_reviewer_program",
]
