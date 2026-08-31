"""Stable JSON, text summaries, and intentionally unsized SPICE skeletons."""

from __future__ import annotations

import json

from topology_lantern.explain import explain_candidate
from topology_lantern.types import Candidate, DeviceKind, GenerationResult


def result_json(result: GenerationResult, *, pretty: bool = False) -> str:
    return (
        json.dumps(
            result.as_dict(),
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def result_text(result: GenerationResult) -> str:
    lines = [
        f"TopologyLantern: {len(result.candidates)} candidates",
        f"spec: sha256:{result.spec_fingerprint}",
        (
            "search: "
            f"{result.explored_states} explored, {result.pruned_states} pruned, "
            f"{result.duplicate_states} duplicate, exhausted={str(result.exhausted).lower()}"
        ),
    ]
    for index, candidate in enumerate(result.candidates, start=1):
        metrics = candidate.metrics
        lines.append(
            f"{index}. {candidate.candidate_id} front={candidate.pareto_rank} "
            f"score={candidate.score} devices={metrics.device_count} "
            f"stages={metrics.stage_count} headroom={metrics.headroom_units}"
        )
        lines.append("   " + " -> ".join(step.rule_id for step in candidate.trace))
    return "\n".join(lines) + "\n"


def candidate_spice(candidate: Candidate) -> str:
    """Emit a review artifact with placeholders, never a simulation-ready deck."""

    lines = [
        f"* TopologyLantern conceptual candidate {candidate.candidate_id}",
        "* UNSIZED REVIEW ARTIFACT — DO NOT TREAT AS A VERIFIED DESIGN",
        f"* canonical signature {candidate.signature}",
    ]
    for step in candidate.trace:
        lines.append(f"* trace {step.index}: {step.rule_id} — {step.summary}")
    for device in candidate.topology.devices:
        connections = dict(device.connections)
        if device.kind in {DeviceKind.NMOS, DeviceKind.PMOS}:
            model = "NMOS_PLACEHOLDER" if device.kind is DeviceKind.NMOS else "PMOS_PLACEHOLDER"
            lines.append(
                f"{device.name} {connections['d']} {connections['g']} "
                f"{connections['s']} {connections['b']} {model}"
            )
        elif device.kind is DeviceKind.RESISTOR:
            lines.append(f"{device.name} {connections['p']} {connections['n']} {{R_UNSIZED}}")
        elif device.kind is DeviceKind.CAPACITOR:
            lines.append(f"{device.name} {connections['p']} {connections['n']} {{C_UNSIZED}}")
        elif device.kind is DeviceKind.CURRENT_SOURCE:
            lines.append(f"{device.name} {connections['p']} {connections['n']} DC {{I_UNSIZED}}")
    lines.extend(
        [
            "* Missing by design: models, dimensions, bias values, analyses, and verification.",
            ".end",
        ]
    )
    return "\n".join(lines) + "\n"


def candidate_explanation(candidate: Candidate) -> str:
    return explain_candidate(candidate)
