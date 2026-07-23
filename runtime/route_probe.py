"""Run one exact or reasoning-capability route probe and print bounded JSON."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from .live_e2e import reasoning_capability_probe, route_probe


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--route", required=True, help="LiteLLM logical route alias")
    result.add_argument(
        "--mode",
        choices=("exact", "reasoning"),
        default="exact",
        help="Probe exact final output or bounded reasoning capability",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    probe = reasoning_capability_probe if arguments.mode == "reasoning" else route_probe
    metadata = probe(arguments.route)
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
