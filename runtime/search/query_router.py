"""Deterministic task-to-search query routing."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .contracts import QueryCandidate

_PATH = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)+[\w.-]+|" r"(?<![\w.-])[\w.-]+\.[A-Za-z0-9]{1,12}"
)
_QUOTED = re.compile(r"[`\"']([^`\"'\n]{3,240})[`\"']")
_IDENTIFIER = re.compile(r"\b(?:[A-Z][A-Za-z0-9]{2,}|[a-z][a-z0-9]*_[a-zA-Z0-9_]+)\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_REGEX_SIGNAL = re.compile(r"(?:\\[bdsw]|\[[^\]]+\]|\(\?:|\.\*|\^|\$)")
_CHANGED = re.compile(
    r"\b(?:changed|modified|dirty|diff|untracked|renamed|deleted)\b",
    re.I,
)
_DEFINITION = re.compile(
    r"\b(?:definition|class|function|method|symbol|declared|implements?)\b",
    re.I,
)
_STOP_WORDS = {
    "about",
    "after",
    "against",
    "also",
    "before",
    "between",
    "change",
    "code",
    "complete",
    "could",
    "current",
    "does",
    "from",
    "have",
    "implementation",
    "into",
    "local",
    "make",
    "need",
    "only",
    "project",
    "repository",
    "should",
    "that",
    "their",
    "there",
    "these",
    "this",
    "using",
    "what",
    "when",
    "where",
    "which",
    "with",
    "work",
}


def _append_unique(items: list[QueryCandidate], candidate: QueryCandidate) -> None:
    key = (candidate.query.casefold(), candidate.mode, candidate.path_globs)
    if any(
        (item.query.casefold(), item.mode, item.path_globs) == key for item in items
    ):
        return
    items.append(candidate)


def build_query_plan(task: str, *, maximum: int = 10) -> tuple[QueryCandidate, ...]:
    """Derive a small transparent query plan without model involvement."""
    task = task.strip()
    if not task:
        return ()
    candidates: list[QueryCandidate] = []

    if _CHANGED.search(task):
        _append_unique(
            candidates,
            QueryCandidate(
                "changed files",
                "changed",
                "task requests current changes",
                8.0,
            ),
        )

    for raw_path in _PATH.findall(task):
        path = raw_path.strip("`'\".,:;()[]{}")
        if not path or path.startswith(("http://", "https://")):
            continue
        name = PurePosixPath(path).name
        _append_unique(
            candidates,
            QueryCandidate(name, "filename", f"task names path {path}", 10.0),
        )
        if "/" in path:
            _append_unique(
                candidates,
                QueryCandidate(
                    path,
                    "filename",
                    f"task names relative path {path}",
                    11.0,
                ),
            )

    for quoted in _QUOTED.findall(task):
        value = quoted.strip()
        if _REGEX_SIGNAL.search(value):
            mode = "regex"
            reason = "task contains a quoted regular expression"
        else:
            mode = "text"
            reason = "task contains quoted exact text"
        _append_unique(candidates, QueryCandidate(value, mode, reason, 9.0))

    identifiers = list(dict.fromkeys(_IDENTIFIER.findall(task)))
    for identifier in identifiers[:4]:
        _append_unique(
            candidates,
            QueryCandidate(
                identifier,
                "symbol",
                "task contains a code identifier",
                8.5,
            ),
        )

    words = [
        word
        for word in _WORD.findall(task)
        if word.casefold() not in _STOP_WORDS and not word.isdigit()
    ]
    terms = list(dict.fromkeys(word.casefold() for word in words))
    if terms:
        natural = " ".join(terms[:6])
        _append_unique(
            candidates,
            QueryCandidate(natural, "auto", "task behaviour terms", 5.0),
        )
        for term in terms[:3]:
            _append_unique(
                candidates,
                QueryCandidate(term, "text", "high-signal task term", 3.0),
            )

    if _DEFINITION.search(task) and identifiers:
        identifier = identifiers[0]
        _append_unique(
            candidates,
            QueryCandidate(identifier, "symbol", "task requests a definition", 10.0),
        )

    return tuple(sorted(candidates, key=lambda item: -item.weight)[:maximum])
