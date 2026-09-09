"""Measure exact graph-to-sequence reconstruction with reproducible workload evidence."""

from __future__ import annotations

import argparse
import json
import statistics
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import topology_lantern
from topology_lantern import (
    compact_graph,
    decode_graph_sequence,
    encode_graph_sequence,
    graph_sequence_json,
    load_spice,
    parse_spice,
)
from topology_lantern.graph_codec import CompactCircuitGraph


def measure(path: Path, *, top: str | None = None, repetitions: int = 5) -> dict[str, object]:
    return _measure(compact_graph(load_spice(path, top=top)), repetitions)


def measure_chain(owners: int, *, repetitions: int = 5) -> dict[str, object]:
    """Build a deterministic original chain without a generated repository fixture."""
    if type(owners) is not int or not 1 <= owners <= 10_000:
        raise ValueError("chain owners must be an integer from 1 through 10000")
    netlist = "\n".join(f"r{index} n{index} n{index + 1} 1k" for index in range(owners))
    graph = compact_graph(parse_spice(netlist + "\n.end\n"))
    return {
        **_measure(graph, repetitions),
        "workload": "original-resistor-chain",
        "chain_owners": owners,
    }


def _measure(graph: CompactCircuitGraph, repetitions: int) -> dict[str, object]:
    if type(repetitions) is not int or not 1 <= repetitions <= 100:
        raise ValueError("repetitions must be an integer from 1 through 100")
    timings: list[float] = []
    encoded_bytes = 0
    sequence_id = ""
    trails = 0
    for _ in range(repetitions):
        start = perf_counter()
        sequence = encode_graph_sequence(graph, seed=7)
        restored = decode_graph_sequence(sequence)
        timings.append((perf_counter() - start) * 1000)
        if restored != graph:
            raise RuntimeError("graph sequence did not reconstruct the full graph")
        encoded_bytes = len(graph_sequence_json(sequence).encode("utf-8"))
        sequence_id = sequence.sequence_id
        trails = sum(len(scope.trails) for scope in sequence.scopes)
    package_root = Path(topology_lantern.__file__).parent
    source_digest = sha256()
    for source in sorted(package_root.rglob("*.py")):
        name = source.relative_to(package_root).as_posix().encode("utf-8")
        content = source.read_bytes()
        source_digest.update(len(name).to_bytes(4, "big") + name)
        source_digest.update(len(content).to_bytes(8, "big") + content)
    return {
        "schema": "org.topology-lantern.graph-sequence-benchmark",
        "version": 1,
        "tool_version": topology_lantern.__version__,
        "implementation_sha256": source_digest.hexdigest(),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_graph_id": graph.source_graph_id,
        "sequence_id": sequence_id,
        "scopes": len(graph.scopes),
        "owners": sum(len(scope.owners) for scope in graph.scopes),
        "incidences": sum(len(scope.edges) for scope in graph.scopes),
        "trails": trails,
        "sequence_bytes": encoded_bytes,
        "exact_reconstruction": True,
        "repetitions": repetitions,
        "median_encode_decode_ms": round(statistics.median(timings), 6),
        "maximum_encode_decode_ms": round(max(timings), 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", nargs="?", type=Path)
    parser.add_argument("--chain-owners", type=int)
    parser.add_argument("--top")
    parser.add_argument("--repetitions", type=int, default=5)
    arguments = parser.parse_args()
    if (arguments.netlist is None) == (arguments.chain_owners is None):
        parser.error("provide exactly one netlist path or --chain-owners")
    if arguments.chain_owners is not None and arguments.top is not None:
        parser.error("--top applies only to a netlist path")
    try:
        result = (
            measure_chain(arguments.chain_owners, repetitions=arguments.repetitions)
            if arguments.chain_owners is not None
            else measure(arguments.netlist, top=arguments.top, repetitions=arguments.repetitions)
        )
    except ValueError as error:
        parser.error(str(error))
    print(
        json.dumps(
            result,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
