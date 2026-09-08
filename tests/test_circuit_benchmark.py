from __future__ import annotations

from pathlib import Path

import pytest

from topology_lantern.circuit_benchmark import _percentile95, circuit_ingest_benchmark


def test_circuit_ingest_benchmark_reports_identity_and_cardinality(tmp_path: Path) -> None:
    netlist = tmp_path / "pair.sp"
    netlist.write_text(
        ".subckt pair a b out1 out2 tail vss\n"
        "m1 out1 a tail vss nch\n"
        "m2 out2 b tail vss nch\n"
        ".ends\n",
        encoding="utf-8",
    )
    report = circuit_ingest_benchmark(netlist, top="pair", repetitions=3)
    assert report["schema"] == "org.topology-lantern.circuit-ingest-benchmark"
    assert report["repetitions"] == 3
    assert report["cardinality"] == {
        "scopes": 1,
        "devices": 2,
        "instances": 0,
        "nets": 6,
    }
    assert str(report["graph_id"]).startswith("sha256:")
    timings = report["milliseconds"]
    assert isinstance(timings, dict)
    assert 0 <= timings["min"] <= timings["median"] <= timings["p95"]


@pytest.mark.parametrize("repetitions", [0, 10_001])
def test_circuit_ingest_benchmark_bounds_repetitions(tmp_path: Path, repetitions: int) -> None:
    with pytest.raises(ValueError, match="repetitions"):
        circuit_ingest_benchmark(tmp_path / "unused.sp", top=None, repetitions=repetitions)


def test_nearest_rank_p95() -> None:
    assert _percentile95([3.0]) == 3.0
    assert _percentile95([float(value) for value in range(1, 101)]) == 95.0
