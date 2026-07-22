"""Validate portable Agent Skill packages without changing runtime behavior."""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .skills_loader import _load_skill_file

_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_STANDARD_RESOURCE_DIRECTORIES = ("scripts", "references", "assets")


def _markdown_link_target(raw_target: str) -> str:
    """Return the path portion of one Markdown link target."""
    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing == -1:
            raise ValueError("unterminated angle-bracket link target")
        return target[1:closing]
    try:
        parts = shlex.split(target)
    except ValueError as exc:
        raise ValueError("invalid quoted Markdown link target") from exc
    if not parts:
        raise ValueError("empty Markdown link target")
    return parts[0]


def _local_link_paths(
    markdown: str, *, skill_directory: Path
) -> tuple[set[Path], list[str]]:
    """Collect safe local paths referenced by a Markdown document."""
    paths: set[Path] = set()
    errors: list[str] = []
    root = skill_directory.resolve()
    for match in _LINK_PATTERN.finditer(markdown):
        try:
            target = _markdown_link_target(match.group(1))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        relative = Path(unquote(parsed.path))
        if relative.is_absolute():
            errors.append(f"local resource link must be relative: {target}")
            continue
        resolved = (skill_directory / relative).resolve()
        if resolved != root and root not in resolved.parents:
            errors.append(f"local resource link escapes the skill directory: {target}")
            continue
        normalized = resolved.relative_to(root)
        paths.add(normalized)
        if not resolved.is_file():
            errors.append(
                "referenced resource does not exist: " f"{normalized.as_posix()}"
            )
    return paths, errors


def _lint_standard_resources(skill_directory: Path, instructions: str) -> list[str]:
    """Validate standard resource directories and direct reference links."""
    errors: list[str] = []
    for directory_name in _STANDARD_RESOURCE_DIRECTORIES:
        resource_path = skill_directory / directory_name
        if resource_path.exists() and not resource_path.is_dir():
            errors.append(f"{directory_name}/ must be a directory when present")

    linked_paths, link_errors = _local_link_paths(
        instructions,
        skill_directory=skill_directory,
    )
    errors.extend(link_errors)

    references_directory = skill_directory / "references"
    if not references_directory.is_dir():
        return errors

    reference_files = {
        path.relative_to(skill_directory)
        for path in references_directory.rglob("*")
        if path.is_file()
    }
    for reference in sorted(reference_files):
        if len(reference.parts) != 2:
            errors.append(
                "reference files must be one level below SKILL.md: "
                f"{reference.as_posix()}"
            )
        if reference not in linked_paths:
            errors.append(
                "orphaned reference is not linked from SKILL.md: "
                f"{reference.as_posix()}"
            )
    return errors


def lint_skills(directory: Path) -> list[str]:
    """Return deterministic validation errors for all immediate skill folders."""
    if not directory.is_dir():
        return [f"Skills directory does not exist: {directory}"]

    skill_directories = sorted(path for path in directory.iterdir() if path.is_dir())
    if not skill_directories:
        return [f"No Agent Skill directories found: {directory}"]

    errors: list[str] = []
    for skill_directory in skill_directories:
        skill_path = skill_directory / "SKILL.md"
        if not skill_path.is_file():
            errors.append(f"{skill_directory.name}: missing SKILL.md")
            continue
        try:
            metadata, instructions = _load_skill_file(skill_path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{skill_directory.name}: {exc}")
            continue
        if not instructions:
            errors.append(f"{metadata.name}: skill instructions are empty")
            continue
        for error in _lint_standard_resources(skill_directory, instructions):
            errors.append(f"{metadata.name}: {error}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the skills linter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(".local-coder/skills"),
        help="skills root to validate (default: .local-coder/skills)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the skills linter and return a process status."""
    arguments = build_parser().parse_args(argv)
    errors = lint_skills(arguments.directory)
    if errors:
        for error in errors:
            print(f"skills-lint: {error}")
        return 1
    count = sum(1 for path in arguments.directory.iterdir() if path.is_dir())
    print(f"Validated {count} Agent Skill packages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
