"""Print only a live E2E summary produced for the current commit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LATEST_SUMMARY = ROOT / ".local-coder" / "live-e2e" / "latest-summary.json"


def current_commit() -> str:
    """Return the checked-out commit for report freshness validation."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def load_latest_summary(path: Path = LATEST_SUMMARY) -> dict[str, Any]:
    """Load the latest report and reject missing or stale results."""
    if not path.is_file():
        raise RuntimeError("No live E2E summary found. Run `make live-e2e` first.")
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid live E2E summary: {exc}") from exc
    if not isinstance(summary, dict):
        raise RuntimeError("Invalid live E2E summary: expected a JSON object.")
    report_commit = summary.get("base_commit")
    checked_out_commit = current_commit()
    if report_commit != checked_out_commit:
        raise RuntimeError(
            "Stale live E2E summary: report was produced for "
            f"{report_commit or 'an unknown commit'}, current commit is "
            f"{checked_out_commit}. Run `make live-e2e`."
        )
    return summary


def main() -> int:
    """Print the current commit's latest live E2E summary."""
    print(json.dumps(load_latest_summary(), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"live-e2e-report: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
