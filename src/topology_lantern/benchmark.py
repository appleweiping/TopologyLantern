"""Machine-readable topology-and-sizing benchmark contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec
from topology_lantern.types import GenerationResult, SpecError


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise SpecError("generation result is not strict canonical JSON") from error


def sizing_benchmark(spec: DesignSpec, result: GenerationResult) -> dict[str, Any]:
    """Bind generated structures to a bounded, technology-neutral sizing task.

    The response surfaces only analytic proxy coefficients.  It is intended for
    algorithm regression and interoperability tests, not electrical sign-off.
    """
    if result.spec_fingerprint != spec.fingerprint():
        raise SpecError("generation result does not belong to the supplied specification")
    if not result.candidates:
        raise SpecError("cannot build a sizing benchmark without candidates")
    regenerated = generate_candidates(spec, limit=result.requested_limit)
    if _canonical_json(result.as_dict()) != _canonical_json(regenerated.as_dict()):
        raise SpecError("generation result does not match a deterministic replay")
    candidates = []
    for candidate in result.candidates:
        metrics = candidate.metrics
        candidates.append(
            {
                "candidate_id": candidate.candidate_id,
                "signature": candidate.signature,
                "device_count": metrics.device_count,
                "transistor_count": metrics.transistor_count,
                "passive_count": metrics.passive_count,
                "stage_count": metrics.stage_count,
                "headroom_units": metrics.headroom_units,
                "symmetry_penalty": metrics.symmetry_penalty,
            }
        )
    body: dict[str, Any] = {
        "schema": "org.topology-lantern.analog-sizing-benchmark",
        "version": 1,
        "model": "deterministic-analytic-proxy-v1",
        "disclaimer": "Algorithm benchmark only; metrics are not simulated electrical results.",
        "provenance": {
            "data_source": "Caller-provided DesignSpec bound by source.spec_sha256",
            "license": (
                "Generated contract: MIT; caller retains responsibility "
                "for DesignSpec source rights."
            ),
            "generation_command": (
                f"topology-lantern benchmark <SPEC> --limit {result.requested_limit} --pretty"
            ),
        },
        "source": {
            "spec_sha256": spec.fingerprint(),
            "candidate_count": len(candidates),
            "supply_voltage": spec.supply_voltage,
        },
        "candidates": candidates,
        "sizing": {
            "width_um": {"low": 0.5, "high": 40.0, "scale": "log"},
            "length_um": {"low": 0.18, "high": 2.0, "scale": "log"},
            "bias_ua": {"low": 5.0, "high": 500.0, "scale": "log"},
            "compensation_pf": {"low": 0.1, "high": 10.0, "scale": "log"},
        },
        "objectives": ["power_mw", "area_um2", "bandwidth_mhz"],
        "constraints": {"gain_db_min": 20.0, "phase_margin_deg_min": 45.0},
        "expected_metrics": [
            "gain_db",
            "phase_margin_deg",
            "power_mw",
            "area_um2",
            "bandwidth_mhz",
        ],
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    body["contract_sha256"] = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    return body


def benchmark_json(spec: DesignSpec, result: GenerationResult, *, pretty: bool = False) -> str:
    """Serialize a sizing contract deterministically."""
    return (
        json.dumps(
            sizing_benchmark(spec, result),
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        + "\n"
    )
