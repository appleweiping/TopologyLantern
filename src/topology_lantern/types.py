"""Immutable domain types for explainable topology synthesis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class PortRole(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    SUPPLY = "supply"
    GROUND = "ground"
    BIAS = "bias"


class DeviceKind(StrEnum):
    NMOS = "nmos"
    PMOS = "pmos"
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    CURRENT_SOURCE = "current_source"


class Severity(StrEnum):
    NOTE = "note"
    WARNING = "warning"
    ERROR = "error"


TERMINALS: Mapping[DeviceKind, tuple[str, ...]] = {
    DeviceKind.NMOS: ("d", "g", "s", "b"),
    DeviceKind.PMOS: ("d", "g", "s", "b"),
    DeviceKind.RESISTOR: ("p", "n"),
    DeviceKind.CAPACITOR: ("p", "n"),
    DeviceKind.CURRENT_SOURCE: ("p", "n"),
}


@dataclass(frozen=True, slots=True)
class Port:
    name: str
    role: PortRole

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role.value}


@dataclass(frozen=True, slots=True)
class Device:
    name: str
    kind: DeviceKind
    connections: tuple[tuple[str, str], ...]
    attributes: tuple[tuple[str, str], ...] = ()

    def net(self, terminal: str) -> str:
        for key, value in self.connections:
            if key == terminal:
                return value
        raise KeyError(f"{self.name} has no terminal {terminal!r}")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "connections": dict(self.connections),
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class Topology:
    ports: tuple[Port, ...]
    nets: tuple[str, ...]
    devices: tuple[Device, ...]

    def port_map(self) -> dict[str, PortRole]:
        return {port.name: port.role for port in self.ports}

    def device_map(self) -> dict[str, Device]:
        return {device.name: device for device in self.devices}

    def as_dict(self) -> dict[str, object]:
        return {
            "ports": [port.as_dict() for port in self.ports],
            "nets": list(self.nets),
            "devices": [device.as_dict() for device in self.devices],
        }


class ObligationKind(StrEnum):
    INPUT_STAGE = "input_stage"
    TAIL_BIAS = "tail_bias"
    LOAD = "load"
    OUTPUT = "output"
    COMPENSATION = "compensation"


@dataclass(frozen=True, slots=True)
class Obligation:
    key: str
    kind: ObligationKind
    details: tuple[tuple[str, str], ...] = ()

    def detail(self, name: str, default: str | None = None) -> str | None:
        return dict(self.details).get(name, default)

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "kind": self.kind.value, "details": dict(self.details)}


@dataclass(frozen=True, slots=True)
class Fact:
    name: str
    value: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True, slots=True)
class TraceStep:
    index: int
    rule_id: str
    summary: str
    rationale: str
    consumed: tuple[str, ...]
    produced: tuple[str, ...]
    added_devices: tuple[str, ...]
    added_nets: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "rule_id": self.rule_id,
            "summary": self.summary,
            "rationale": self.rationale,
            "consumed": list(self.consumed),
            "produced": list(self.produced),
            "added_devices": list(self.added_devices),
            "added_nets": list(self.added_nets),
        }


@dataclass(frozen=True, slots=True)
class SearchState:
    topology: Topology
    obligations: tuple[Obligation, ...]
    facts: tuple[Fact, ...]
    trace: tuple[TraceStep, ...] = ()

    def fact_map(self) -> dict[str, str]:
        return {fact.name: fact.value for fact in self.facts}

    def as_dict(self) -> dict[str, object]:
        return {
            "topology": self.topology.as_dict(),
            "obligations": [item.as_dict() for item in self.obligations],
            "facts": [item.as_dict() for item in self.facts],
            "trace": [item.as_dict() for item in self.trace],
        }


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    code: str
    severity: Severity
    message: str
    subjects: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "subjects": list(self.subjects),
        }


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    device_count: int
    transistor_count: int
    passive_count: int
    internal_net_count: int
    stage_count: int
    headroom_units: int
    symmetry_penalty: int
    review_warnings: int

    def as_dict(self) -> dict[str, int]:
        return {
            "device_count": self.device_count,
            "transistor_count": self.transistor_count,
            "passive_count": self.passive_count,
            "internal_net_count": self.internal_net_count,
            "stage_count": self.stage_count,
            "headroom_units": self.headroom_units,
            "symmetry_penalty": self.symmetry_penalty,
            "review_warnings": self.review_warnings,
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    signature: str
    topology: Topology
    facts: tuple[Fact, ...]
    trace: tuple[TraceStep, ...]
    metrics: CandidateMetrics
    violations: tuple[ConstraintViolation, ...] = ()
    pareto_rank: int = 0
    score: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "signature": self.signature,
            "pareto_rank": self.pareto_rank,
            "score": self.score,
            "topology": self.topology.as_dict(),
            "facts": [fact.as_dict() for fact in self.facts],
            "metrics": self.metrics.as_dict(),
            "violations": [item.as_dict() for item in self.violations],
            "trace": [step.as_dict() for step in self.trace],
        }


@dataclass(frozen=True, slots=True)
class GenerationResult:
    spec_fingerprint: str
    candidates: tuple[Candidate, ...]
    explored_states: int
    pruned_states: int
    duplicate_states: int
    exhausted: bool
    requested_limit: int
    rule_catalog: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "tool": {"name": "TopologyLantern", "version": "0.1.0"},
            "spec_fingerprint": self.spec_fingerprint,
            "search": {
                "requested_limit": self.requested_limit,
                "explored_states": self.explored_states,
                "pruned_states": self.pruned_states,
                "duplicate_states": self.duplicate_states,
                "exhausted": self.exhausted,
            },
            "rule_catalog": list(self.rule_catalog),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


class LanternError(Exception):
    """Base class for expected input or generation failures."""


class SpecError(LanternError):
    """A design specification is invalid or ambiguous."""


class ReplayError(LanternError):
    """A trace cannot be deterministically replayed."""
