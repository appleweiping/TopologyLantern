"""Repeatable performance evidence for hierarchical circuit ingest."""

from __future__ import annotations

import statistics
from pathlib import Path
from time import perf_counter_ns

from topology_lantern.spice import load_spice


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]


def circuit_ingest_benchmark(
    path: str | Path, *, top: str | None = None, repetitions: int = 100
) -> dict[str, object]:
    """Measure ingest and pair timings with stable identity/cardinality evidence."""
    if not 1 <= repetitions <= 10_000:
        raise ValueError("repetitions must be in [1, 10000]")
    source = Path(path)
    reference = load_spice(source, top=top)
    samples_ms: list[float] = []
    for _ in range(repetitions):
        started = perf_counter_ns()
        graph = load_spice(source, top=top)
        samples_ms.append((perf_counter_ns() - started) / 1_000_000)
        if graph.graph_id != reference.graph_id:
            raise RuntimeError("ingest produced a non-deterministic graph identity")
    return {
        "schema": "org.topology-lantern.circuit-ingest-benchmark",
        "version": 1,
        "input": source.name,
        "top": reference.top,
        "graph_id": reference.graph_id,
        "repetitions": repetitions,
        "cardinality": {
            "scopes": len(reference.scopes),
            "devices": sum(len(scope.devices) for scope in reference.scopes),
            "instances": sum(len(scope.instances) for scope in reference.scopes),
            "nets": sum(len(scope.nets) for scope in reference.scopes),
        },
        "milliseconds": {
            "min": min(samples_ms),
            "median": statistics.median(samples_ms),
            "p95": _percentile95(samples_ms),
        },
    }
