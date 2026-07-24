"""Committed search profile and external derived-state paths."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SIZE = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[KMG]?)(?:B)?$", re.I)


@dataclass(frozen=True)
class SearchProfile:
    """Trusted command names and bounded search defaults."""

    ripgrep_binary: str
    zoekt_binary: str
    zoekt_index_binary: str
    ctags_binary: str
    locate_binary: str
    max_file_size: str
    default_limit: int
    timeout_seconds: float
    context_character_budget: int
    zoekt_module_version: str

    @property
    def max_file_bytes(self) -> int:
        """Return the configured per-file limit as bytes."""
        match = _SIZE.fullmatch(self.max_file_size.strip())
        if match is None:
            raise ValueError(
                f"Invalid repository search max_file_size: {self.max_file_size}"
            )
        multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[
            match.group("unit").upper()
        ]
        return int(match.group("value")) * multiplier


def search_data_root() -> Path:
    """Return the machine-local derived repository-search root."""
    override = os.environ.get("LOCAL_CODER_SEARCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "local-coder" / "search").resolve()


def load_search_profile(repository: Path) -> SearchProfile:
    """Load and validate the committed repository-search profile."""
    path = repository / "profiles" / "repository-search-v1.json"
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Unsupported repository search profile.")
    binaries = raw.get("binaries")
    limits = raw.get("limits")
    pins = raw.get("pins")
    if not all(isinstance(value, dict) for value in (binaries, limits, pins)):
        raise ValueError("Repository search profile is incomplete.")
    profile = SearchProfile(
        ripgrep_binary=str(binaries["ripgrep"]),
        zoekt_binary=str(binaries["zoekt"]),
        zoekt_index_binary=str(binaries["zoekt_git_index"]),
        ctags_binary=str(binaries["ctags"]),
        locate_binary=str(binaries["plocate"]),
        max_file_size=str(limits["max_file_size"]),
        default_limit=int(limits["default_limit"]),
        timeout_seconds=float(limits["timeout_seconds"]),
        context_character_budget=int(limits["context_character_budget"]),
        zoekt_module_version=str(pins["zoekt_module_version"]),
    )
    if profile.default_limit < 1 or profile.default_limit > 200:
        raise ValueError("Repository search default_limit must be between 1 and 200.")
    if profile.timeout_seconds <= 0 or profile.timeout_seconds > 60:
        raise ValueError("Repository search timeout_seconds is out of bounds.")
    if profile.context_character_budget < 1000:
        raise ValueError("Repository context_character_budget is too small.")
    _ = profile.max_file_bytes
    return profile
