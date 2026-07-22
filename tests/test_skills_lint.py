from pathlib import Path

import pytest

from runtime.skills_lint import lint_skills, main

ROOT = Path(__file__).resolve().parents[1]


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Perform a focused task. Use when the task matches.",
    body: str = "# Instructions\n\nFollow the procedure.",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n" f"{body}\n",
        encoding="utf-8",
    )
    return directory


def test_repository_skill_packages_pass_lint() -> None:
    assert lint_skills(ROOT / ".local-coder" / "skills") == []


def test_linter_reports_missing_skill_file(tmp_path: Path) -> None:
    (tmp_path / "incomplete-skill").mkdir()

    assert lint_skills(tmp_path) == ["incomplete-skill: missing SKILL.md"]


def test_linter_reuses_frontmatter_validation(tmp_path: Path) -> None:
    description = "x" * 1025
    _write_skill(tmp_path, "too-long", description=description)

    assert lint_skills(tmp_path) == [
        f"too-long: Agent Skill description exceeds 1024 characters: "
        f"{tmp_path / 'too-long' / 'SKILL.md'}"
    ]


def test_linter_reports_broken_local_resource_link(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "broken-link",
        body=(
            "# Instructions\n\n"
            "Read [the contract](references/CONTRACT.md) before proceeding."
        ),
    )

    assert lint_skills(tmp_path) == [
        "broken-link: referenced resource does not exist: references/CONTRACT.md"
    ]


def test_linter_reports_orphaned_reference(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path, "orphaned-reference")
    references = directory / "references"
    references.mkdir()
    (references / "REFERENCE.md").write_text("# Reference\n", encoding="utf-8")

    assert lint_skills(tmp_path) == [
        "orphaned-reference: orphaned reference is not linked from SKILL.md: "
        "references/REFERENCE.md"
    ]


def test_linter_accepts_linked_reference(tmp_path: Path) -> None:
    directory = _write_skill(
        tmp_path,
        "linked-reference",
        body="# Instructions\n\nSee [the reference](references/REFERENCE.md).",
    )
    references = directory / "references"
    references.mkdir()
    (references / "REFERENCE.md").write_text("# Reference\n", encoding="utf-8")

    assert lint_skills(tmp_path) == []


def test_linter_rejects_standard_resource_file(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path, "invalid-resources")
    (directory / "assets").write_text("not a directory\n", encoding="utf-8")

    assert lint_skills(tmp_path) == [
        "invalid-resources: assets/ must be a directory when present"
    ]


def test_linter_reports_nested_reference(tmp_path: Path) -> None:
    directory = _write_skill(
        tmp_path,
        "nested-reference",
        body=(
            "# Instructions\n\n"
            "See [the nested reference](references/nested/REFERENCE.md)."
        ),
    )
    references = directory / "references" / "nested"
    references.mkdir(parents=True)
    (references / "REFERENCE.md").write_text("# Reference\n", encoding="utf-8")

    assert lint_skills(tmp_path) == [
        "nested-reference: reference files must be one level below SKILL.md: "
        "references/nested/REFERENCE.md"
    ]


def test_linter_rejects_reference_path_escape(tmp_path: Path) -> None:
    outside = tmp_path / "OUTSIDE.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    _write_skill(
        tmp_path,
        "escaping-reference",
        body="# Instructions\n\nSee [outside](../OUTSIDE.md).",
    )

    assert lint_skills(tmp_path) == [
        "escaping-reference: local resource link escapes the skill directory: "
        "../OUTSIDE.md"
    ]


def test_cli_returns_success_for_repository_skills(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([str(ROOT / ".local-coder" / "skills")]) == 0
    assert capsys.readouterr().out == "Validated 5 Agent Skill packages.\n"
