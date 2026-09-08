"""Measure bounded hierarchical SPICE ingest without claiming a performance SLA."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from topology_lantern.circuit_benchmark import circuit_ingest_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--top")
    parser.add_argument("--repetitions", type=int, default=100)
    arguments = parser.parse_args()
    print(
        json.dumps(
            circuit_ingest_benchmark(
                arguments.netlist,
                top=arguments.top,
                repetitions=arguments.repetitions,
            ),
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
