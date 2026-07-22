"""Validated atomic source edits generated through the local-fast route."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

API_URL = "http://127.0.0.1:4000/v1/chat/completions"
MAX_FILE_BYTES = 32_000
MAX_TOTAL_BYTES = 64_000
MAX_EDITS = 8

PIPELINE_CONTROLS = {
    ".flake8",
    "AGENTS.md",
    "ROADMAP.md",
    "docs/HANDOFF.md",
    "TASK.md",
    "Makefile",
    "docs/ARCHITECTURE.md",
    "docs/CONVENTIONS.md",
    "docs/PIPELINE.md",
    "docs/RECURSIVE_IMPROVEMENT.md",
    "docs/TASK_PLANS.md",
    "docs/UPSTREAM.json",
    "docs/VALIDATION_HISTORY.md",
    "litellm-config.yaml",
    "local-coder.py",
    "pytest.ini",
    "review-diff.py",
    "requirements-agent.txt",
    "run-editor.py",
}
PIPELINE_CONTROL_PREFIXES = ("evaluation/",)


class EditorError(RuntimeError):
    """Raised when a generated edit cannot be applied safely."""


@dataclass(frozen=True)
class AtomicEdit:
    """One exact search/replace edit against an approved file."""

    path: str
    old_text: str
    new_text: str


def edit_schema(editable_files: list[str]) -> dict[str, Any]:
    """Return the strict JSON schema accepted from the local editor model."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["edits"],
        "properties": {
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_EDITS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "old_text", "new_text"],
                    "properties": {
                        "path": {"type": "string", "enum": editable_files},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                    },
                },
            }
        },
    }


def parse_edit_content(content: str) -> list[AtomicEdit]:
    """Parse strict JSON, allowing one exact Markdown JSON fence."""
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines[0] not in {"```", "```json"} or lines[-1] != "```":
            raise EditorError("The editor returned an invalid JSON fence.")
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise EditorError("The editor returned invalid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"edits"}:
        raise EditorError("The editor response must contain only `edits`.")
    raw_edits = payload["edits"]
    if not isinstance(raw_edits, list) or not 1 <= len(raw_edits) <= MAX_EDITS:
        raise EditorError("The editor returned an invalid number of edits.")
    edits: list[AtomicEdit] = []
    for raw in raw_edits:
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "old_text",
            "new_text",
        }:
            raise EditorError("Each edit must contain path, old_text, and new_text.")
        if not all(isinstance(raw[field], str) for field in raw):
            raise EditorError("Every edit field must be text.")
        edits.append(AtomicEdit(**raw))
    return edits


def _safe_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise EditorError(f"Unsafe editable path: {relative}")
    candidate = (root / path).resolve()
    resolved_root = root.resolve()
    if resolved_root not in candidate.parents:
        raise EditorError(f"Editable path escapes the worktree: {relative}")
    if not candidate.is_file():
        raise EditorError(f"Editable file does not exist: {relative}")
    return candidate


def _is_protected(relative: str, extra: set[str]) -> bool:
    path = Path(relative)
    normalized = path.as_posix()
    return (
        path.name.endswith("_contract.py")
        or path.name == "TASK.md"
        or normalized in PIPELINE_CONTROLS
        or normalized.startswith(PIPELINE_CONTROL_PREFIXES)
        or normalized in extra
    )


def load_editable_files(
    root: Path,
    editable_files: list[str],
    *,
    protected_files: set[str] | None = None,
) -> dict[str, str]:
    """Load approved UTF-8 files within bounded context limits."""
    if not editable_files:
        raise EditorError("At least one editable file is required.")
    if len(set(editable_files)) != len(editable_files):
        raise EditorError("Editable file paths must be unique.")
    protected = protected_files or set()
    contents: dict[str, str] = {}
    total_bytes = 0
    for relative in editable_files:
        if _is_protected(relative, protected):
            raise EditorError(f"Protected file cannot be edited: {relative}")
        path = _safe_file(root, relative)
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise EditorError(f"Editable file is too large: {relative}")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise EditorError("Editable file context exceeds the total size limit.")
        try:
            contents[relative] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditorError(f"Editable file is not UTF-8: {relative}") from exc
    return contents


def validate_edits(
    contents: dict[str, str],
    edits: list[AtomicEdit],
) -> dict[str, str]:
    """Validate all edits in memory and return complete updated contents."""
    updated = dict(contents)
    for edit in edits:
        if edit.path not in updated:
            raise EditorError(f"Edit path is outside the approved scope: {edit.path}")
        if not edit.old_text:
            raise EditorError("old_text must not be empty.")
        if edit.old_text == edit.new_text:
            raise EditorError(f"Edit is a no-op: {edit.path}")
        count = updated[edit.path].count(edit.old_text)
        if count != 1:
            raise EditorError(
                f"old_text must match exactly once in {edit.path}; found {count}."
            )
        updated[edit.path] = updated[edit.path].replace(
            edit.old_text,
            edit.new_text,
            1,
        )
    if updated == contents:
        raise EditorError("The editor produced no changed file content.")
    return updated


def _write_atomically(path: Path, content: str) -> None:
    mode = path.stat().st_mode
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def apply_edits(
    root: Path,
    editable_files: list[str],
    edits: list[AtomicEdit],
    *,
    protected_files: set[str] | None = None,
) -> list[str]:
    """Validate a complete edit set, then atomically replace changed files."""
    contents = load_editable_files(
        root,
        editable_files,
        protected_files=protected_files,
    )
    updated = validate_edits(contents, edits)
    changed = [path for path in editable_files if updated[path] != contents[path]]
    for relative in changed:
        _write_atomically(_safe_file(root, relative), updated[relative])
    return changed


def request_edits(
    *,
    instruction: str,
    contents: dict[str, str],
    task: str,
    model: str = "local-fast",
    api_url: str = API_URL,
    metrics_callback: Callable[..., None] | None = None,
) -> list[AtomicEdit]:
    """Ask local-fast for strict exact replacements against approved contents."""
    context = "\n\n".join(
        f"--- {path} ---\n{content}" for path, content in contents.items()
    )
    prompt = f"""
Generate the smallest exact search/replace edits needed for this atomic task.

Rules:
- Return exactly one JSON object with this shape:
  {{"edits":[{{"path":"<approved path>",
  "old_text":"<exact existing text>",
  "new_text":"<replacement text>"}}]}}
- The top-level object must contain only `edits`.
- Each edit must contain only `path`, `old_text`, and `new_text`.
- Use only approved paths from the constrained schema.
- old_text must be a verbatim, non-empty substring of the supplied file.
- Include enough surrounding text for old_text to match exactly once.
- Preserve all unrelated content.
- Do not create, rename, delete, stage, commit, or mention any other file.
- If the task cannot be completed with exact replacements, return no prose;
  the schema validator will fail closed.

Authoritative task:
{task}

Atomic instruction:
{instruction}

Approved file contents:
{context}
""".strip()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a bounded source editor. Return only validated exact "
                    "search/replace operations for approved existing files."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 4096,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "atomic_edits",
                "strict": True,
                "schema": edit_schema(list(contents)),
            },
        },
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer local",
        },
        method="POST",
    )
    started = time.perf_counter()
    result: dict[str, Any] | None = None
    status = "error"
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise EditorError(f"Editor API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise EditorError(f"Could not reach LiteLLM: {exc}") from exc
    try:
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("Editor content is not text.")
    except (KeyError, IndexError, TypeError) as exc:
        raise EditorError("The editor returned an invalid response envelope.") from exc
    try:
        edits = parse_edit_content(content)
    except EditorError as exc:
        status = "error"
        parse_error: EditorError | None = exc
    else:
        status = "success"
        parse_error = None
    if metrics_callback is not None:
        usage = result.get("usage", {})
        metadata: dict[str, Any] = {
            "status": status,
            "source": "native-editor",
        }
        if parse_error is not None:
            metadata["response_excerpt"] = content[:2000]
        metrics_callback(
            route=model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            duration_ms=(time.perf_counter() - started) * 1000,
            metadata=metadata,
        )
    if parse_error is not None:
        raise parse_error
    return edits


def request_and_apply(
    *,
    root: Path,
    instruction: str,
    editable_files: list[str],
    task: str,
    protected_files: set[str] | None = None,
    metrics_callback: Callable[..., None] | None = None,
) -> list[str]:
    """Request, validate, and apply one atomic local-fast edit batch."""
    contents = load_editable_files(
        root,
        editable_files,
        protected_files=protected_files,
    )
    edits = request_edits(
        instruction=instruction,
        contents=contents,
        task=task,
        metrics_callback=metrics_callback,
    )
    updated = validate_edits(contents, edits)
    changed = [path for path in editable_files if updated[path] != contents[path]]
    for relative in changed:
        _write_atomically(_safe_file(root, relative), updated[relative])
    return changed
