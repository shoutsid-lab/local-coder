"""Compact deterministic verification output for model-facing evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

VERIFICATION_EVIDENCE_SCHEMA_VERSION = 1
MAX_FAILURE_LINES = 40
MAX_FAILURE_CHARS = 6_000

_KNOWN_DSPY_PREFIX_WARNING = re.compile(
    r"DeprecationWarning: The 'prefix' argument in InputField/OutputField "
    r"is deprecated and has no effect in DSPy\."
)
_WARNING_LINE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*Warning:")
_TEST_COUNT = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<label>passed|failed|errors?|skipped|warnings?|xfailed|xpassed)\b"
)
_FAILURE_LINE = re.compile(
    r"^(?:FAILED\s|ERROR\s|E\s{2,}|Traceback \(most recent call last\):|"
    r"make(?:\[\d+\])?: \*\*\*|.*\bError:\s)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VerificationEvidence:
    """Structured compact evidence while raw output remains separately audited."""

    passed: bool
    tests: dict[str, int]
    warnings: dict[str, Any]
    failures: tuple[str, ...]
    raw_sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable payload."""
        return {
            "schema_version": VERIFICATION_EVIDENCE_SCHEMA_VERSION,
            "passed": self.passed,
            "tests": dict(self.tests),
            "warnings": dict(self.warnings),
            "failures": list(self.failures),
            "raw_sha256": self.raw_sha256,
        }

    def to_json(self) -> str:
        """Serialize the evidence deterministically for the audit artifact."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def model_output(self) -> str:
        """Render bounded high-signal text suitable for role prompts."""
        status = "PASS" if self.passed else "FAIL"
        lines = [f"Verification: {status}"]
        counts = [
            f"{self.tests.get('passed', 0)} passed",
            f"{self.tests.get('failed', 0)} failed",
            f"{self.tests.get('errors', 0)} errors",
            f"{self.tests.get('skipped', 0)} skipped",
        ]
        if any(self.tests.values()):
            lines.append(f"Tests: {', '.join(counts)}.")
        known = int(self.warnings.get("known_third_party", 0))
        unexpected = int(self.warnings.get("unexpected", 0))
        lines.append(
            "Warnings: "
            f"{known} known third-party DSPy deprecations; "
            f"{unexpected} unexpected."
        )
        if self.failures:
            lines.append("Failure evidence:")
            lines.extend(self.failures)
        lines.append(
            "Raw verification output preserved in the audit store "
            f"(sha256:{self.raw_sha256})."
        )
        return "\n".join(lines)


def _test_counts(output: str) -> dict[str, int]:
    counts = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "warnings": 0,
        "xfailed": 0,
        "xpassed": 0,
    }
    candidates = [
        line.strip()
        for line in output.splitlines()
        if " in " in line and _TEST_COUNT.search(line)
    ]
    if not candidates:
        candidates = [
            line.strip() for line in output.splitlines() if _TEST_COUNT.search(line)
        ]
    if not candidates:
        return counts
    for match in _TEST_COUNT.finditer(candidates[-1]):
        label = match.group("label")
        if label == "error":
            label = "errors"
        elif label == "warning":
            label = "warnings"
        counts[label] = int(match.group("count"))
    return counts


def _warning_counts(output: str) -> dict[str, Any]:
    known = 0
    unexpected = 0
    for line in output.splitlines():
        if _KNOWN_DSPY_PREFIX_WARNING.search(line):
            known += 1
        elif _WARNING_LINE.search(line):
            unexpected += 1
    return {
        "known_third_party": known,
        "unexpected": unexpected,
        "fingerprints": ({"dspy-prefix-input-output-field": known} if known else {}),
    }


def _failure_evidence(output: str, passed: bool) -> tuple[str, ...]:
    if passed:
        return ()
    selected: list[str] = []
    for line in output.splitlines():
        stripped = line.rstrip()
        if _KNOWN_DSPY_PREFIX_WARNING.search(stripped):
            continue
        if _FAILURE_LINE.search(stripped):
            selected.append(stripped[:500])
    if not selected:
        useful = [
            line.rstrip()
            for line in output.splitlines()
            if line.strip()
            and not _KNOWN_DSPY_PREFIX_WARNING.search(line)
            and "warnings summary" not in line.lower()
            and not line.startswith("-- Docs:")
        ]
        selected = useful[-MAX_FAILURE_LINES:]
    bounded: list[str] = []
    remaining = MAX_FAILURE_CHARS
    for line in selected[:MAX_FAILURE_LINES]:
        if remaining <= 0:
            break
        item = line[:remaining]
        bounded.append(item)
        remaining -= len(item) + 1
    return tuple(bounded)


def summarize_verification_output(
    output: str,
    *,
    passed: bool,
) -> VerificationEvidence:
    """Summarize raw command output without discarding the raw audit record."""
    if not isinstance(output, str):
        raise TypeError("verification output must be text")
    return VerificationEvidence(
        passed=bool(passed),
        tests=_test_counts(output),
        warnings=_warning_counts(output),
        failures=_failure_evidence(output, bool(passed)),
        raw_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
    )
