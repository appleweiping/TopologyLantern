"""Informational search-scaling benchmark with deterministic invariants."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import topology_lantern
from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec

ROOT = Path(__file__).parents[1]
SPEC_PATH = ROOT / "examples" / "low_voltage_diff_stage.json"


def _package_tree_sha256() -> str:
    if topology_lantern.__file__ is None:
        raise RuntimeError("cannot locate imported topology_lantern package")
    root = Path(topology_lantern.__file__).resolve().parent
    digest = sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _environment() -> dict[str, str]:
    return {
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def run(limits: tuple[int, ...], repetitions: int) -> dict[str, object]:
    """Measure bounded generation while keeping timing out of acceptance criteria."""

    if not limits or any(
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64
        for limit in limits
    ):
        raise ValueError("limits must be integers from 1 through 64")
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or not 1 <= repetitions <= 100
    ):
        raise ValueError("repetitions must be an integer from 1 through 100")
    source = SPEC_PATH.read_bytes()
    spec = DesignSpec.from_json(SPEC_PATH)
    workload = sha256(source)
    measurements: list[dict[str, object]] = []
    for limit in limits:
        workload.update(limit.to_bytes(2, "big"))
        durations: list[float] = []
        result = None
        for _ in range(repetitions):
            started = time.perf_counter()
            result = generate_candidates(spec, limit=limit)
            durations.append(time.perf_counter() - started)
        if result is None or not result.candidates:
            raise RuntimeError("search produced no benchmark candidates")
        candidate_ids = [candidate.candidate_id for candidate in result.candidates]
        identity = sha256("\n".join(candidate_ids).encode("ascii")).hexdigest()
        measurements.append(
            {
                "requested_limit": limit,
                "invariants": {
                    "candidate_count": len(candidate_ids),
                    "candidate_ids_sha256": identity,
                    "explored_states": result.explored_states,
                    "pruned_states": result.pruned_states,
                    "duplicate_states": result.duplicate_states,
                    "exhausted": result.exhausted,
                },
                "timing": {
                    "median_seconds": round(statistics.median(durations), 9),
                    "minimum_seconds": round(min(durations), 9),
                },
            }
        )
    environment = _environment()
    encoded_environment = json.dumps(environment, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "benchmark": "topology-lantern-search-scaling-v1",
        "distribution_version": version("topology-lantern"),
        "package_tree_sha256": _package_tree_sha256(),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "environment": environment,
        "environment_sha256": sha256(encoded_environment.encode()).hexdigest(),
        "workload_sha256": workload.hexdigest(),
        "repetitions": repetitions,
        "results": measurements,
        "timing_policy": "Informational only; no timing value is an acceptance threshold.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limits", default="1,2,4")
    parser.add_argument("--repetitions", default=5, type=int)
    arguments = parser.parse_args()
    try:
        requested_limits = tuple(int(item) for item in arguments.limits.split(","))
        report = run(requested_limits, arguments.repetitions)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, sort_keys=True))
