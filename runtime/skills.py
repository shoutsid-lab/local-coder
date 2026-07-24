"""Bind portable Agent Skills to local-coder runtime capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .role_profiles import role_route
from .skills_loader import SkillCatalog, SkillMetadata
from .skills_loader import discover_skills as discover_skill_catalog


@dataclass(frozen=True)
class SkillRuntimeConfig:
    """Trusted model, tool, and step boundaries for one local skill."""

    model: str
    tools: tuple[str, ...]
    max_steps: int


@dataclass(frozen=True)
class Skill:
    """An activated procedure and its trusted runtime boundaries."""

    name: str
    description: str
    model: str
    tools: tuple[str, ...]
    max_steps: int
    instructions: str
    path: Path


_SKILL_CONFIGS = {
    "atomic-implementation": SkillRuntimeConfig(
        model=role_route("implementer"),
        tools=(
            "read_file",
            "search_repository",
            "apply_atomic_edit",
            "inspect_diff",
            "run_verification",
            "git_status",
        ),
        max_steps=7,
    ),
    "explore-repository": SkillRuntimeConfig(
        model=role_route("explorer"),
        tools=("list_files", "search_repository", "read_file", "git_status"),
        max_steps=1,
    ),
    "plan-change": SkillRuntimeConfig(
        model=role_route("planner"),
        tools=("list_files", "search_repository", "read_file", "git_status"),
        max_steps=1,
    ),
    "review-change": SkillRuntimeConfig(
        model=role_route("reviewer"),
        tools=(
            "read_file",
            "inspect_diff",
            "run_verification",
            "review_diff",
            "git_status",
        ),
        max_steps=5,
    ),
    "test-and-repair": SkillRuntimeConfig(
        model=role_route("repairer"),
        tools=(
            "read_file",
            "search_repository",
            "apply_atomic_edit",
            "run_verification",
            "inspect_diff",
            "rollback_worktree",
        ),
        max_steps=7,
    ),
}


def runtime_skill_config(name: str) -> SkillRuntimeConfig:
    """Return the trusted local runtime configuration for a skill."""
    try:
        return _SKILL_CONFIGS[name]
    except KeyError as exc:
        raise ValueError(f"No local runtime configuration for skill: {name}") from exc


def activate_skill(catalog: SkillCatalog, name: str) -> Skill:
    """Activate a portable skill and attach trusted local boundaries."""
    activated = catalog.activate(name)
    config = runtime_skill_config(name)
    return Skill(
        name=activated.name,
        description=activated.description,
        model=config.model,
        tools=config.tools,
        max_steps=config.max_steps,
        instructions=activated.instructions,
        path=activated.path,
    )


def load_skill(path: Path) -> Skill:
    """Eagerly load one configured skill for compatibility utilities."""
    catalog = discover_skill_catalog(path.parent.parent)
    metadata = catalog.get(path.parent.name)
    if metadata is None or metadata.path != path:
        raise ValueError(f"Skill was not discovered at expected path: {path}")
    return activate_skill(catalog, metadata.name)


def discover_skills(directory: Path) -> dict[str, Skill]:
    """Eagerly load configured skills for compatibility and inspection."""
    catalog = discover_skill_catalog(directory)
    return {name: activate_skill(catalog, name) for name in catalog}


__all__ = [
    "Skill",
    "SkillCatalog",
    "SkillMetadata",
    "SkillRuntimeConfig",
    "activate_skill",
    "discover_skill_catalog",
    "discover_skills",
    "load_skill",
    "runtime_skill_config",
]
