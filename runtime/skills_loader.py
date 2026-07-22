"""Discover and activate Agent Skills with progressive disclosure."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ALLOWED_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_STRING_PLAIN_SCALAR = re.compile(
    r"(?:~|null|true|false|yes|no|on|off|"
    r"[-+]?(?:\.inf|\.nan)|"
    r"[-+]?(?:0|[1-9][0-9_]*|0b[01_]+|0o[0-7_]+|0x[0-9a-f_]+)"
    r"(?:\.[0-9_]*)?(?:e[-+]?[0-9]+)?|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[tT ]|$).*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SkillMetadata:
    """Discovery metadata retained without loading skill instructions."""

    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class ActivatedSkill:
    """A skill whose Markdown instructions have been loaded on demand."""

    name: str
    description: str
    instructions: str
    path: Path


class SkillCatalog(Mapping[str, SkillMetadata]):
    """An immutable catalog that activates full skill instructions by name."""

    def __init__(self, skills: dict[str, SkillMetadata]) -> None:
        self._skills = dict(skills)

    def __getitem__(self, name: str) -> SkillMetadata:
        return self._skills[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._skills)

    def __len__(self) -> int:
        return len(self._skills)

    def activate(self, name: str) -> ActivatedSkill:
        """Load and validate one skill body when a role is selected."""
        metadata = self._skills[name]
        discovered, body = _load_skill_file(metadata.path)
        if discovered != metadata:
            raise ValueError(f"Skill metadata changed after discovery: {metadata.path}")
        if not body:
            raise ValueError(f"Skill instructions are empty: {metadata.path}")
        return ActivatedSkill(
            name=metadata.name,
            description=metadata.description,
            instructions=body,
            path=metadata.path,
        )


def _strip_inline_comment(value: str) -> str:
    """Strip an unquoted YAML comment from a scalar value."""
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None:
            if index == 0 or value[index - 1].isspace():
                return value[:index].rstrip()
    return value.strip()


def _parse_scalar(value: str, *, field: str) -> str:
    """Parse one string-valued frontmatter scalar."""
    stripped = _strip_inline_comment(value)
    if not stripped:
        raise ValueError(f"Skill frontmatter field '{field}' must be a string.")
    if stripped[0] in {"'", '"'}:
        try:
            if stripped[0] == "'":
                if len(stripped) < 2 or stripped[-1] != "'":
                    raise ValueError
                body = stripped[1:-1]
                characters: list[str] = []
                index = 0
                while index < len(body):
                    if body[index] != "'":
                        characters.append(body[index])
                        index += 1
                        continue
                    if index + 1 >= len(body) or body[index + 1] != "'":
                        raise ValueError
                    characters.append("'")
                    index += 2
                parsed = "".join(characters)
            else:
                parsed = ast.literal_eval(stripped)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(
                f"Invalid quoted value for skill frontmatter field '{field}'."
            ) from exc
        if not isinstance(parsed, str):
            raise ValueError(f"Skill frontmatter field '{field}' must be a string.")
        return parsed
    if stripped[0] in {"[", "{", "&", "*", "!"} or (
        _NON_STRING_PLAIN_SCALAR.fullmatch(stripped)
    ):
        raise ValueError(f"Skill frontmatter field '{field}' must be a string.")
    return stripped


def _parse_block_scalar(
    lines: list[str], start: int, *, folded: bool, field: str
) -> tuple[str, int]:
    """Parse a simple indented YAML block scalar."""
    values: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line and not line[0].isspace():
            break
        if line.strip():
            values.append(line.lstrip())
        else:
            values.append("")
        index += 1
    if not values:
        raise ValueError(f"Skill frontmatter field '{field}' must be non-empty.")
    separator = " " if folded else "\n"
    return separator.join(values).strip(), index


def _parse_frontmatter(lines: list[str], *, path: Path) -> dict[str, Any]:
    """Parse the supported Agent Skills frontmatter fields."""
    parsed: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace():
            raise ValueError(f"Unexpected indentation in skill frontmatter: {path}")
        if ":" not in line:
            raise ValueError(f"Invalid skill frontmatter line in {path}: {line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if key not in _ALLOWED_FIELDS:
            raise ValueError(f"Unsupported skill frontmatter field '{key}': {path}")
        if key in parsed:
            raise ValueError(f"Duplicate skill frontmatter field '{key}': {path}")

        if key == "metadata":
            if raw_value and raw_value != "{}":
                raise ValueError(f"Skill metadata must be a string mapping: {path}")
            metadata: dict[str, str] = {}
            while index < len(lines):
                nested = lines[index]
                if not nested.strip() or nested.lstrip().startswith("#"):
                    index += 1
                    continue
                if not nested[0].isspace():
                    break
                index += 1
                content = nested.strip()
                if ":" not in content:
                    raise ValueError(f"Invalid skill metadata line in {path}: {nested}")
                metadata_key, metadata_value = content.split(":", 1)
                metadata_key = metadata_key.strip()
                if not metadata_key or metadata_key in metadata:
                    raise ValueError(f"Invalid duplicate skill metadata key: {path}")
                metadata[metadata_key] = _parse_scalar(
                    metadata_value, field=f"metadata.{metadata_key}"
                )
            parsed[key] = metadata
            continue

        if raw_value in {"|", "|-", "|+", ">", ">-", ">+"}:
            value, index = _parse_block_scalar(
                lines,
                index,
                folded=raw_value.startswith(">"),
                field=key,
            )
            parsed[key] = value
            continue
        parsed[key] = _parse_scalar(raw_value, field=key)

    return parsed


def _validate_metadata(metadata: dict[str, Any], path: Path) -> SkillMetadata:
    """Validate strict Agent Skills discovery metadata."""
    missing = {"name", "description"} - metadata.keys()
    if missing:
        raise ValueError(f"Missing skill metadata in {path}: {sorted(missing)}")

    name = metadata["name"]
    description = metadata["description"]
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid Agent Skill name '{name}': {path}")
    if len(name) > 64:
        raise ValueError(f"Agent Skill name exceeds 64 characters: {path}")
    if name != path.parent.name:
        raise ValueError(f"Agent Skill name must match its directory: {path}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Agent Skill description must be non-empty: {path}")
    if len(description) > 1024:
        raise ValueError(f"Agent Skill description exceeds 1024 characters: {path}")

    license_name = metadata.get("license")
    if license_name is not None and not isinstance(license_name, str):
        raise ValueError(f"Agent Skill license must be a string: {path}")
    compatibility = metadata.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str) or not compatibility:
            raise ValueError(f"Agent Skill compatibility must be non-empty: {path}")
        if len(compatibility) > 500:
            raise ValueError(
                f"Agent Skill compatibility exceeds 500 characters: {path}"
            )
    allowed_tools = metadata.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        raise ValueError(f"Agent Skill allowed-tools must be a string: {path}")
    skill_metadata = metadata.get("metadata")
    if skill_metadata is not None and (
        not isinstance(skill_metadata, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in skill_metadata.items()
        )
    ):
        raise ValueError(f"Agent Skill metadata must map strings to strings: {path}")

    return SkillMetadata(
        name=name,
        description=description.strip(),
        path=path,
    )


def _read_frontmatter(path: Path) -> tuple[list[str], int]:
    """Read only the YAML frontmatter and return its ending line number."""
    with path.open(encoding="utf-8") as stream:
        first = stream.readline()
        if first.strip() != "---":
            raise ValueError("Skill file must begin with YAML frontmatter.")
        lines: list[str] = []
        for line_number, line in enumerate(stream, start=2):
            if line.strip() == "---":
                return [item.rstrip("\n") for item in lines], line_number
            lines.append(line)
    raise ValueError("Skill frontmatter is not terminated with '---'.")


def _load_metadata(path: Path) -> SkillMetadata:
    """Read and validate only one skill's discovery metadata."""
    lines, _ = _read_frontmatter(path)
    return _validate_metadata(_parse_frontmatter(lines, path=path), path)


def _load_skill_file(path: Path) -> tuple[SkillMetadata, str]:
    """Load complete skill instructions after metadata discovery."""
    metadata = _load_metadata(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    end_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if end_index is None:
        raise ValueError("Skill frontmatter is not terminated with '---'.")
    return metadata, "\n".join(lines[end_index + 1 :]).strip()


def discover_skills(directory: Path) -> SkillCatalog:
    """Discover nested SKILL.md metadata without loading instruction bodies."""
    skills: dict[str, SkillMetadata] = {}
    if not directory.exists():
        return SkillCatalog(skills)
    for path in sorted(directory.glob("*/SKILL.md")):
        skill = _load_metadata(path)
        if skill.name in skills:
            raise ValueError(f"Duplicate skill name: {skill.name}")
        skills[skill.name] = skill
    return SkillCatalog(skills)
