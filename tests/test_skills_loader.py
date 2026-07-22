from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.skills_loader import SkillCatalog, SkillMetadata, discover_skills

ROOT = Path(__file__).resolve().parents[1]


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Perform a focused task. Use when the task matches.",
    extra_frontmatter: str = "",
    body: str = "# Instructions\n\nFollow the procedure.",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    extra = f"{extra_frontmatter.rstrip()}\n" if extra_frontmatter else ""
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_repository_skills_are_discovered_as_metadata_only() -> None:
    catalog = discover_skills(ROOT / ".local-coder" / "skills")

    assert isinstance(catalog, SkillCatalog)
    assert set(catalog) == {
        "explore-repository",
        "plan-change",
        "atomic-implementation",
        "test-and-repair",
        "review-change",
    }
    metadata = catalog["atomic-implementation"]
    assert isinstance(metadata, SkillMetadata)
    assert "Use when" in metadata.description
    assert not hasattr(metadata, "instructions")


def test_discovery_does_not_read_the_markdown_body(tmp_path: Path) -> None:
    _write_skill(tmp_path, "deferred-body")

    with patch.object(Path, "read_text", side_effect=AssertionError("body read")):
        catalog = discover_skills(tmp_path)

    assert catalog["deferred-body"].description.startswith("Perform a focused task")


def test_skill_body_is_validated_only_on_activation(tmp_path: Path) -> None:
    _write_skill(tmp_path, "empty-body", body="")

    catalog = discover_skills(tmp_path)

    assert catalog["empty-body"].name == "empty-body"
    with pytest.raises(ValueError, match="instructions are empty"):
        catalog.activate("empty-body")


def test_activation_loads_and_revalidates_the_skill(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "focused-task")
    catalog = discover_skills(tmp_path)

    activated = catalog.activate("focused-task")

    assert activated.instructions.startswith("# Instructions")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Perform a focused task", "Perform a changed task"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="metadata changed after discovery"):
        catalog.activate("focused-task")


def test_loader_accepts_standard_optional_frontmatter(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "portable-skill",
        extra_frontmatter=(
            "license: Apache-2.0\n"
            "compatibility: Requires git\n"
            "metadata:\n"
            "  author: example-org\n"
            "  version: '1.0'\n"
            "allowed-tools: Read Grep"
        ),
    )

    catalog = discover_skills(tmp_path)

    assert catalog["portable-skill"].name == "portable-skill"


@pytest.mark.parametrize(
    "value",
    ["123", "true", "null", "2026-07-22", "[Read, Grep]"],
)
def test_loader_rejects_non_string_yaml_scalars(tmp_path: Path, value: str) -> None:
    _write_skill(
        tmp_path,
        "typed-metadata",
        extra_frontmatter=f"metadata:\n  version: {value}",
    )

    with pytest.raises(ValueError, match="must be a string"):
        discover_skills(tmp_path)


def test_loader_accepts_quoted_scalar_strings(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "quoted-metadata",
        extra_frontmatter=(
            "license: 'Owner''s license'\n"
            "metadata:\n"
            "  version: '1.0'\n"
            '  released: "2026-07-22"'
        ),
    )

    catalog = discover_skills(tmp_path)

    assert catalog["quoted-metadata"].name == "quoted-metadata"


@pytest.mark.parametrize(
    ("name", "extra_frontmatter", "message"),
    [
        ("bad--name", "", "Invalid Agent Skill name"),
        ("unknown-field", "model: local-fast", "Unsupported skill frontmatter"),
        ("wrong-directory", "", "must match its directory"),
    ],
)
def test_loader_rejects_noncompliant_frontmatter(
    tmp_path: Path,
    name: str,
    extra_frontmatter: str,
    message: str,
) -> None:
    directory_name = "different-directory" if name == "wrong-directory" else name
    directory = tmp_path / directory_name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Perform a task. Use when needed.\n"
        f"{extra_frontmatter}\n"
        "---\n"
        "# Instructions\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        discover_skills(tmp_path)
