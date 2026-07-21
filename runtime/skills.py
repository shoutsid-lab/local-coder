"""Load focused, vendor-neutral agent skills from Markdown files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Skill:
    """A focused procedure and its tool/model boundaries."""

    name: str
    description: str
    model: str
    tools: tuple[str, ...]
    max_steps: int
    instructions: str
    path: Path


def _parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    try:
        return int(stripped)
    except ValueError:
        return stripped.strip("\"'")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Skill file must begin with YAML-style frontmatter.")

    metadata: dict[str, Any] = {}
    current_list: str | None = None
    end_index: int | None = None

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_list:
            metadata[current_list].append(_parse_scalar(line[4:]))
            continue
        if ":" not in line:
            raise ValueError(f"Invalid skill metadata line: {line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            metadata[key] = _parse_scalar(raw_value)
            current_list = None
        else:
            metadata[key] = []
            current_list = key

    if end_index is None:
        raise ValueError("Skill frontmatter is not terminated with '---'.")

    body = "\n".join(lines[end_index + 1 :]).strip()
    return metadata, body


def load_skill(path: Path) -> Skill:
    """Load and validate one SKILL.md file."""
    metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    required = {"name", "description", "model", "tools", "max_steps"}
    missing = required - metadata.keys()
    if missing:
        raise ValueError(f"Missing skill metadata in {path}: {sorted(missing)}")
    tools = metadata["tools"]
    if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        raise ValueError(f"Skill tools must be a list of names: {path}")
    if not body:
        raise ValueError(f"Skill instructions are empty: {path}")
    return Skill(
        name=str(metadata["name"]),
        description=str(metadata["description"]),
        model=str(metadata["model"]),
        tools=tuple(tools),
        max_steps=int(metadata["max_steps"]),
        instructions=body,
        path=path,
    )


def discover_skills(directory: Path) -> dict[str, Skill]:
    """Discover all nested SKILL.md files under a directory."""
    skills: dict[str, Skill] = {}
    if not directory.exists():
        return skills
    for path in sorted(directory.glob("*/SKILL.md")):
        skill = load_skill(path)
        if skill.name in skills:
            raise ValueError(f"Duplicate skill name: {skill.name}")
        skills[skill.name] = skill
    return skills
