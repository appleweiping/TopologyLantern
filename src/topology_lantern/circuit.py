"""Typed, deterministic representation of a hierarchical circuit graph."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from topology_lantern.types import LanternError


class CircuitGraphError(LanternError, ValueError):
    """SPICE input cannot be represented by the supported graph contract."""


class CircuitElementKind(StrEnum):
    """Primitive device families supported by the clean-room ingest subset."""

    MOSFET = "mosfet"
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    CURRENT_SOURCE = "current_source"
    VOLTAGE_SOURCE = "voltage_source"
    DIODE = "diode"
    BJT = "bjt"


def stable_node_id(kind: str, scope: str, name: str) -> str:
    """Derive a stable, namespaced node identity from normalized names."""
    payload = json.dumps((kind, scope, name), separators=(",", ":"), ensure_ascii=True)
    return f"tlg-{kind}-{sha256(payload.encode('ascii')).hexdigest()[:20]}"


@dataclass(frozen=True, slots=True)
class CircuitPort:
    node_id: str
    name: str
    index: int
    net_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "name": self.name,
            "index": self.index,
            "net_id": self.net_id,
        }


@dataclass(frozen=True, slots=True)
class CircuitNet:
    node_id: str
    name: str
    is_port: bool
    is_global: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "name": self.name,
            "is_port": self.is_port,
            "is_global": self.is_global,
        }


@dataclass(frozen=True, slots=True)
class SourceLocation:
    file: str
    line: int

    def as_dict(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line}


@dataclass(frozen=True, slots=True)
class CircuitDevice:
    node_id: str
    name: str
    kind: CircuitElementKind
    terminals: tuple[tuple[str, str], ...]
    model: str | None
    parameters: tuple[tuple[str, str], ...]
    source: SourceLocation

    def terminal_map(self) -> dict[str, str]:
        return dict(self.terminals)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "name": self.name,
            "kind": self.kind.value,
            "terminals": dict(self.terminals),
            "model": self.model,
            "parameters": dict(self.parameters),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CircuitInstance:
    node_id: str
    name: str
    reference: str
    reference_scope_id: str
    connections: tuple[tuple[str, str], ...]
    parameters: tuple[tuple[str, str], ...]
    effective_parameters: tuple[tuple[str, str], ...]
    source: SourceLocation

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "name": self.name,
            "reference": self.reference,
            "reference_scope_id": self.reference_scope_id,
            "connections": dict(self.connections),
            "parameters": dict(self.parameters),
            "effective_parameters": dict(self.effective_parameters),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CircuitScope:
    scope_id: str
    name: str
    ports: tuple[CircuitPort, ...]
    parameters: tuple[tuple[str, str], ...]
    nets: tuple[CircuitNet, ...]
    devices: tuple[CircuitDevice, ...]
    instances: tuple[CircuitInstance, ...]

    def net_map(self) -> dict[str, CircuitNet]:
        return {net.name: net for net in self.nets}

    def device_map(self) -> dict[str, CircuitDevice]:
        return {device.name: device for device in self.devices}

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.scope_id,
            "name": self.name,
            "ports": [port.as_dict() for port in self.ports],
            "parameters": dict(self.parameters),
            "nets": [net.as_dict() for net in self.nets],
            "devices": [device.as_dict() for device in self.devices],
            "instances": [instance.as_dict() for instance in self.instances],
        }


@dataclass(frozen=True, slots=True)
class CircuitGraph:
    graph_id: str
    top: str
    scopes: tuple[CircuitScope, ...]
    sources: tuple[str, ...]

    def scope_map(self) -> dict[str, CircuitScope]:
        return {scope.name: scope for scope in self.scopes}

    def node_ids(self) -> frozenset[str]:
        return frozenset(
            node_id
            for scope in self.scopes
            for node_id in (
                scope.scope_id,
                *(port.node_id for port in scope.ports),
                *(net.node_id for net in scope.nets),
                *(device.node_id for device in scope.devices),
                *(instance.node_id for instance in scope.instances),
            )
        )

    def device_ids(self) -> frozenset[str]:
        return frozenset(device.node_id for scope in self.scopes for device in scope.devices)

    def placement_ids(self) -> frozenset[str]:
        return frozenset(
            node_id
            for scope in self.scopes
            for node_id in (
                *(device.node_id for device in scope.devices),
                *(instance.node_id for instance in scope.instances),
            )
        )

    def placement_scope_map(self) -> dict[str, str]:
        return {
            node_id: scope.name
            for scope in self.scopes
            for node_id in (
                *(device.node_id for device in scope.devices),
                *(instance.node_id for instance in scope.instances),
            )
        }

    def net_ids(self) -> frozenset[str]:
        return frozenset(net.node_id for scope in self.scopes for net in scope.nets)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "org.topology-lantern.circuit-graph",
            "version": 1,
            "graph_id": self.graph_id,
            "top": self.top,
            "sources": list(self.sources),
            "scopes": [scope.as_dict() for scope in self.scopes],
        }


def circuit_graph_identity(top: str, scopes: tuple[CircuitScope, ...]) -> str:
    """Compute the semantic graph identity, excluding source-path provenance."""

    semantic_scopes: list[dict[str, object]] = []
    for scope in scopes:
        net_names_by_id = {net.node_id: net.name for net in scope.nets}
        semantic_scopes.append(
            {
                "name": scope.name,
                "ports": [port.name for port in scope.ports],
                "parameters": list(scope.parameters),
                "nets": [{"name": net.name, "is_global": net.is_global} for net in scope.nets],
                "devices": [
                    {
                        "name": device.name,
                        "kind": device.kind.value,
                        "terminals": [
                            (terminal, net_names_by_id[net_id])
                            for terminal, net_id in device.terminals
                        ],
                        "model": device.model,
                        "parameters": list(device.parameters),
                    }
                    for device in scope.devices
                ],
                "instances": [
                    {
                        "name": instance.name,
                        "reference": instance.reference,
                        "connections": [
                            (port, net_names_by_id[net_id]) for port, net_id in instance.connections
                        ],
                        "parameters": list(instance.parameters),
                    }
                    for instance in scope.instances
                ],
            }
        )
    semantic = {"version": 1, "top": top, "scopes": semantic_scopes}
    canonical = json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{sha256(canonical.encode('ascii')).hexdigest()}"


def circuit_graph_json(graph: CircuitGraph, *, pretty: bool = False) -> str:
    """Serialize the stable version-1 circuit graph contract."""
    return (
        json.dumps(
            graph.as_dict(),
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )
