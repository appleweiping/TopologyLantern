from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import topology_lantern
from topology_lantern.benchmark import benchmark_json, sizing_benchmark
from topology_lantern.cli import EXIT_INPUT, entrypoint, main
from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec
from topology_lantern.types import SpecError


def _imported_tree_sha256() -> str:
    assert topology_lantern.__file__ is not None
    root = Path(topology_lantern.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def test_benchmark_contract_is_deterministic_and_bound_to_candidates() -> None:
    spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
    result = generate_candidates(spec, limit=4)
    first = sizing_benchmark(spec, result)
    second = sizing_benchmark(spec, result)
    assert first == second
    assert first["source"]["candidate_count"] == 4
    assert [item["candidate_id"] for item in first["candidates"]] == [
        item.candidate_id for item in result.candidates
    ]
    assert len(first["contract_sha256"]) == 64
    assert "not simulated" in first["disclaimer"]


def test_checked_in_manifest_matches_deterministic_generation() -> None:
    spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
    generated = sizing_benchmark(spec, generate_candidates(spec, limit=4))
    checked_in = json.loads(Path("benchmarks/manifest.json").read_text(encoding="utf-8"))
    assert generated == checked_in


def test_sky130_reference_contract_matches_its_single_stage_spec() -> None:
    spec = DesignSpec.from_json("examples/sky130_common_source.json")
    generated = sizing_benchmark(spec, generate_candidates(spec, limit=4))
    checked_in = json.loads(
        Path("benchmarks/sky130-common-source.json").read_text(encoding="utf-8")
    )
    assert generated == checked_in
    assert [item["candidate_id"] for item in generated["candidates"]] == ["TL-00052f7b8c5e"]
    assert generated["provenance"]["data_source"].endswith("source.spec_sha256")


def test_public_schema_accepts_every_checked_in_contract() -> None:
    schema = json.loads(
        Path("docs/schemas/analog-sizing-benchmark-1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for path in Path("benchmarks").glob("*.json"):
        validator.validate(json.loads(path.read_text(encoding="utf-8")))


def test_scaling_benchmark_smoke_reports_stable_search_invariants() -> None:
    completed = subprocess.run(  # nosec B603
        [
            sys.executable,
            "benchmarks/scaling.py",
            "--limits",
            "1,2",
            "--repetitions",
            "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["schema_version"] == 1
    assert len(output["workload_sha256"]) == 64
    assert output["distribution_version"] == topology_lantern.__version__
    assert output["package_tree_sha256"] == _imported_tree_sha256()
    assert (
        output["harness_sha256"]
        == hashlib.sha256(Path("benchmarks/scaling.py").read_bytes()).hexdigest()
    )
    assert [row["invariants"]["candidate_count"] for row in output["results"]] == [1, 2]


def test_scaling_benchmark_rejects_non_integer_parameters() -> None:
    scaling_run = runpy.run_path("benchmarks/scaling.py")["run"]
    with pytest.raises(ValueError, match="limits"):
        scaling_run((1.5,), 1)
    with pytest.raises(ValueError, match="repetitions"):
        scaling_run((1,), 1.5)


def test_scaling_cli_rejects_non_integer_limit_without_traceback() -> None:
    completed = subprocess.run(  # nosec B603
        [sys.executable, "benchmarks/scaling.py", "--limits", "1.5"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "limits" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_benchmark_rejects_result_from_another_spec() -> None:
    spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
    result = generate_candidates(spec, limit=2)
    with pytest.raises(SpecError, match="does not belong"):
        sizing_benchmark(spec, replace(result, spec_fingerprint="0" * 64))


def test_benchmark_rejects_tampered_generation_evidence() -> None:
    spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
    result = generate_candidates(spec, limit=2)
    altered = replace(result.candidates[0], signature="0" * 64)
    with pytest.raises(SpecError, match="deterministic replay"):
        sizing_benchmark(spec, replace(result, candidates=(altered, *result.candidates[1:])))


def test_benchmark_replay_distinguishes_boolean_from_integer_metrics() -> None:
    spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
    result = generate_candidates(spec, limit=2)
    metrics = replace(result.candidates[0].metrics, stage_count=True)
    altered = replace(result.candidates[0], metrics=metrics)
    with pytest.raises(SpecError, match="deterministic replay"):
        sizing_benchmark(spec, replace(result, candidates=(altered, *result.candidates[1:])))


def test_benchmark_cli_writes_parseable_contract(tmp_path) -> None:
    output = tmp_path / "benchmark.json"
    assert (
        main(
            [
                "benchmark",
                "examples/low_voltage_diff_stage.json",
                "--limit",
                "3",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["candidates"]) == 3
    assert (
        json.loads(
            benchmark_json(
                DesignSpec.from_json("examples/low_voltage_diff_stage.json"),
                generate_candidates("examples/low_voltage_diff_stage.json", limit=3),
            )
        )
        == data
    )


def test_console_entrypoint_propagates_input_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["topology-lantern", "benchmark", "missing.json"])
    with pytest.raises(SystemExit) as raised:
        entrypoint()
    assert raised.value.code == EXIT_INPUT
