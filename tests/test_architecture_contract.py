import ast
from pathlib import Path

from runtime.editor import PIPELINE_CONTROLS
from runtime.models import ModelRegistry
from runtime.skills import discover_skills

ROOT = Path(__file__).resolve().parents[1]


def test_logical_model_routes_remain_stable() -> None:
    assert set(ModelRegistry().routes) == {
        "local-fast",
        "local-plan",
        "local-review",
        "local-reason",
    }


def test_trusted_repository_controls_are_not_agent_editable() -> None:
    assert {
        ".flake8",
        "AGENTS.md",
        "ROADMAP.md",
        "docs/HANDOFF.md",
        "Makefile",
        "docs/ARCHITECTURE.md",
        "docs/CONVENTIONS.md",
        "docs/PIPELINE.md",
        "docs/RECURSIVE_IMPROVEMENT.md",
        "docs/VALIDATION_HISTORY.md",
        "litellm-config.yaml",
        "local-coder.py",
        "pytest.ini",
        "review-diff.py",
        "requirements-agent.txt",
        "run-editor.py",
    } <= PIPELINE_CONTROLS


def test_only_code_action_skills_expose_the_atomic_editor() -> None:
    skills = discover_skills(ROOT / ".local-coder" / "skills")
    editing_skills = {
        name for name, skill in skills.items() if "apply_atomic_edit" in skill.tools
    }

    assert editing_skills == {"atomic-implementation", "test-and-repair"}


def test_runtime_contains_no_git_promotion_command() -> None:
    forbidden = {"add", "commit", "merge", "push"}
    project_sources = [
        path
        for path in ROOT.rglob("*.py")
        if not ({"tests", ".venv", ".local-coder"} & set(path.parts))
    ]
    for path in sorted(project_sources):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            words = [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            if len(words) >= 2 and words[0] == "git":
                assert (
                    words[1] not in forbidden
                ), f"Forbidden Git promotion command in {path}"
