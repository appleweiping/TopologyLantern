"""Transparent metrics, Pareto fronts, and deterministic weighted ordering."""

from __future__ import annotations

from dataclasses import replace

from topology_lantern.graph import count_kinds
from topology_lantern.spec import DesignSpec
from topology_lantern.types import (
    Candidate,
    CandidateMetrics,
    DeviceKind,
    SearchState,
    Severity,
)


def measure(state: SearchState) -> CandidateMetrics:
    topology = state.topology
    transistors = count_kinds(topology, (DeviceKind.NMOS, DeviceKind.PMOS))
    passives = count_kinds(topology, (DeviceKind.RESISTOR, DeviceKind.CAPACITOR))
    ports = set(topology.port_map())
    internal_nets = len(set(topology.nets) - ports)
    facts = state.fact_map()
    return CandidateMetrics(
        device_count=len(topology.devices),
        transistor_count=transistors,
        passive_count=passives,
        internal_net_count=internal_nets,
        stage_count=int(facts.get("stage_count", "1")),
        headroom_units=int(facts.get("headroom_units", "1")),
        symmetry_penalty=0 if facts.get("symmetry") == "paired" else 1,
        review_warnings=0,
    )


def with_warning_count(metrics: CandidateMetrics, candidate: Candidate) -> CandidateMetrics:
    count = sum(item.severity in {Severity.WARNING, Severity.NOTE} for item in candidate.violations)
    return replace(metrics, review_warnings=count)


def objective_vector(candidate: Candidate) -> tuple[int, ...]:
    metrics = candidate.metrics
    return (
        metrics.device_count,
        metrics.headroom_units,
        metrics.passive_count,
        metrics.symmetry_penalty,
        metrics.review_warnings,
        metrics.stage_count,
    )


def dominates(left: Candidate, right: Candidate) -> bool:
    one = objective_vector(left)
    two = objective_vector(right)
    return all(a <= b for a, b in zip(one, two, strict=True)) and any(
        a < b for a, b in zip(one, two, strict=True)
    )


def _weighted_score(spec: DesignSpec, candidate: Candidate) -> int:
    metrics = candidate.metrics
    weights = spec.objectives
    return (
        metrics.device_count * weights.device_count
        + metrics.headroom_units * weights.headroom
        + metrics.symmetry_penalty * weights.symmetry
        + metrics.passive_count * weights.passives
        + metrics.review_warnings * weights.warnings
    )


def rank_candidates(spec: DesignSpec, candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Assign non-dominated fronts, then use declared weights as a tie-breaker."""

    remaining = list(candidates)
    ranked: list[Candidate] = []
    front_number = 0
    while remaining:
        front = [
            candidate
            for candidate in remaining
            if not any(dominates(other, candidate) for other in remaining if other is not candidate)
        ]
        decorated = [
            replace(
                candidate,
                pareto_rank=front_number,
                score=_weighted_score(spec, candidate),
            )
            for candidate in front
        ]
        ranked.extend(
            sorted(
                decorated,
                key=lambda item: (item.score, objective_vector(item), item.signature),
            )
        )
        front_signatures = {item.signature for item in front}
        remaining = [item for item in remaining if item.signature not in front_signatures]
        front_number += 1
    return tuple(ranked)
