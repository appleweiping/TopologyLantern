"""Reversible, seeded device-name augmentation of validated circuit graphs.

This changes identifiers, not terminal connectivity or expressions. It is graph
data preparation, not a SPICE source rewrite or a proof of electrical equivalence.
The replay manifest carries no copy of the original graph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256

from topology_lantern.circuit import (
    CircuitElementKind,
    CircuitGraph,
    CircuitGraphError,
    circuit_graph_identity,
    stable_node_id,
)
from topology_lantern.graph_codec import compact_graph, compact_to_circuit

_PREFIX = {
    CircuitElementKind.MOSFET: "m",
    CircuitElementKind.RESISTOR: "r",
    CircuitElementKind.CAPACITOR: "c",
    CircuitElementKind.CURRENT_SOURCE: "i",
    CircuitElementKind.VOLTAGE_SOURCE: "v",
    CircuitElementKind.DIODE: "d",
    CircuitElementKind.BJT: "q",
}
_MAX_SCOPES = 512
_MAX_SOURCES = 32
_MAX_OWNERS = 100_000
_MAX_NETS = 100_000
_MAX_EDGES = 500_000
_MAX_PARAMETERS = 4_096
_MAX_NESTED_PAIRS = 1_000_000


class GraphAugmentationError(CircuitGraphError):
    """A graph or its reversible augmentation evidence is inconsistent."""


@dataclass(frozen=True, slots=True)
class DeviceRename:
    """One exact scoped name/identity substitution, in original canonical order."""

    scope_id: str
    original_id: str
    original_name: str
    renamed_id: str
    renamed_name: str


@dataclass(frozen=True, slots=True)
class RenamedCircuit:
    """Augmented graph plus enough information to reconstruct and verify its source."""

    source_graph_id: str
    source_evidence_id: str
    seed: int
    graph: CircuitGraph
    renames: tuple[DeviceRename, ...]


def _seed(seed: object) -> int:
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise GraphAugmentationError("seed must be an unsigned 64-bit integer")
    return seed


def _digest(value: object, context: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise GraphAugmentationError(f"{context} must be a lowercase sha256: identity")
    return value


def _source_evidence_id(graph: CircuitGraph) -> str:
    """Bind every validated source field, including deliberately non-semantic provenance."""

    canonical = json.dumps(
        graph.as_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return f"sha256:{sha256(canonical.encode('ascii')).hexdigest()}"


def _check_nested_limits(edge_count: int, pair_count: int) -> None:
    if edge_count > _MAX_EDGES or pair_count > _MAX_NESTED_PAIRS:
        raise GraphAugmentationError("graph nested data exceeds augmentation limits")


def _require_immutable_pairs(collection: tuple[tuple[str, str], ...]) -> None:
    if any(not isinstance(pair, tuple) for pair in collection):
        raise GraphAugmentationError("graph must have immutable canonical fields")


def _checked_graph(graph: CircuitGraph) -> CircuitGraph:
    if not isinstance(graph, CircuitGraph):
        raise GraphAugmentationError("graph must be a CircuitGraph")
    try:
        if not isinstance(graph.scopes, tuple) or not 1 <= len(graph.scopes) <= _MAX_SCOPES:
            raise GraphAugmentationError("graph scope count or immutable shape is invalid")
        if not isinstance(graph.sources, tuple) or not 1 <= len(graph.sources) <= _MAX_SOURCES:
            raise GraphAugmentationError(
                "graph source count or immutable canonical shape is invalid"
            )
        owner_count = net_count = edge_count = nested_pair_count = 0
        for scope in graph.scopes:
            collections = (
                scope.ports,
                scope.parameters,
                scope.nets,
                scope.devices,
                scope.instances,
            )
            if any(not isinstance(value, tuple) for value in collections):
                raise GraphAugmentationError("graph must have immutable canonical fields")
            owner_count += len(scope.ports) + len(scope.devices) + len(scope.instances)
            net_count += len(scope.nets)
            edge_count += len(scope.ports)
            nested_pair_count += len(scope.parameters)
            if len(scope.parameters) > _MAX_PARAMETERS:
                raise GraphAugmentationError("graph parameter count exceeds augmentation limits")
            if owner_count > _MAX_OWNERS:
                raise GraphAugmentationError("graph owner count exceeds augmentation limits")
            if net_count > _MAX_NETS:
                raise GraphAugmentationError("graph net count exceeds augmentation limits")
            _check_nested_limits(edge_count, nested_pair_count)
            _require_immutable_pairs(scope.parameters)
            for device in scope.devices:
                if not isinstance(device.terminals, tuple) or not isinstance(
                    device.parameters, tuple
                ):
                    raise GraphAugmentationError("graph must have immutable canonical fields")
                if len(device.parameters) > _MAX_PARAMETERS:
                    raise GraphAugmentationError(
                        "graph parameter count exceeds augmentation limits"
                    )
                edge_count += len(device.terminals)
                nested_pair_count += len(device.terminals) + len(device.parameters)
                _check_nested_limits(edge_count, nested_pair_count)
                _require_immutable_pairs(device.terminals)
                _require_immutable_pairs(device.parameters)
            for instance in scope.instances:
                instance_collections = (
                    instance.connections,
                    instance.parameters,
                    instance.effective_parameters,
                )
                if any(not isinstance(value, tuple) for value in instance_collections):
                    raise GraphAugmentationError("graph must have immutable canonical fields")
                if any(len(value) > _MAX_PARAMETERS for value in instance_collections[1:]):
                    raise GraphAugmentationError(
                        "graph parameter count exceeds augmentation limits"
                    )
                edge_count += len(instance.connections)
                nested_pair_count += sum(len(value) for value in instance_collections)
                _check_nested_limits(edge_count, nested_pair_count)
                for collection in instance_collections:
                    _require_immutable_pairs(collection)
        checked = compact_to_circuit(compact_graph(graph))
    except (CircuitGraphError, AttributeError, TypeError, KeyError, ValueError) as error:
        raise GraphAugmentationError(f"cannot augment invalid circuit graph: {error}") from error
    if checked != graph:
        raise GraphAugmentationError("graph must have immutable canonical fields")
    return checked


def rename_devices(graph: CircuitGraph, *, seed: int = 0) -> RenamedCircuit:
    """Rename primitive devices reproducibly; retain scopes, nets and all other data.

    SHA-256 priorities are independent of host RNG implementations. Names retain
    the primitive-kind prefix, include the full seed and use unique scope-local
    ranks. Source file/line evidence and expressions are retained verbatim. All
    output device collections are sorted into the normal graph canonical order.
    """
    seed = _seed(seed)
    graph = _checked_graph(graph)
    scopes = []
    renames = []
    for scope in graph.scopes:
        ordered = sorted(
            scope.devices,
            key=lambda device: (
                sha256(f"{seed}:{scope.scope_id}:{device.node_id}".encode("ascii")).digest(),
                device.node_id,
            ),
        )
        names = {
            device.node_id: f"{_PREFIX[device.kind]}_aug_{seed:016x}_{index:06d}"
            for index, device in enumerate(ordered)
        }
        devices = []
        for device in scope.devices:
            name = names[device.node_id]
            node_id = stable_node_id("device", scope.name, name)
            renames.append(DeviceRename(scope.scope_id, device.node_id, device.name, node_id, name))
            devices.append(replace(device, node_id=node_id, name=name))
        scopes.append(
            replace(scope, devices=tuple(sorted(devices, key=lambda device: device.name)))
        )
    result_scopes = tuple(scopes)
    result = replace(
        graph,
        scopes=result_scopes,
        graph_id=circuit_graph_identity(graph.top, result_scopes),
    )
    _checked_graph(result)
    return RenamedCircuit(
        graph.graph_id,
        _source_evidence_id(graph),
        seed,
        result,
        tuple(renames),
    )


def restore_device_names(augmentation: RenamedCircuit) -> CircuitGraph:
    """Reconstruct original names and verify every mapping by deterministic replay.

    A matching identity is a consistency check, not a signature. Dataset split
    logic must group this source and all its augmentations before partitioning.
    """
    if not isinstance(augmentation, RenamedCircuit):
        raise GraphAugmentationError("augmentation must be a RenamedCircuit")
    _seed(augmentation.seed)
    _digest(augmentation.source_graph_id, "source graph id")
    _digest(augmentation.source_evidence_id, "source evidence id")
    graph = _checked_graph(augmentation.graph)
    count = sum(len(scope.devices) for scope in graph.scopes)
    if not isinstance(augmentation.renames, tuple) or len(augmentation.renames) != count:
        raise GraphAugmentationError("rename manifest must cover every device exactly once")
    by_target: dict[tuple[str, str], DeviceRename] = {}
    for entry in augmentation.renames:
        if not isinstance(entry, DeviceRename) or not all(
            isinstance(value, str) and 1 <= len(value) <= 256
            for value in (
                entry.scope_id,
                entry.original_id,
                entry.original_name,
                entry.renamed_id,
                entry.renamed_name,
            )
        ):
            raise GraphAugmentationError("rename manifest entry is invalid")
        key = (entry.scope_id, entry.renamed_id)
        if key in by_target:
            raise GraphAugmentationError("rename manifest contains duplicate device targets")
        by_target[key] = entry
    scopes = []
    for scope in graph.scopes:
        devices = []
        for device in scope.devices:
            matched = by_target.get((scope.scope_id, device.node_id))
            if matched is None or matched.renamed_name != device.name:
                raise GraphAugmentationError("rename manifest does not match the augmented graph")
            if matched.original_id != stable_node_id("device", scope.name, matched.original_name):
                raise GraphAugmentationError("original device identity does not match its name")
            devices.append(replace(device, node_id=matched.original_id, name=matched.original_name))
        scopes.append(
            replace(scope, devices=tuple(sorted(devices, key=lambda device: device.name)))
        )
    source = replace(graph, graph_id=augmentation.source_graph_id, scopes=tuple(scopes))
    source = _checked_graph(source)
    if _source_evidence_id(source) != augmentation.source_evidence_id:
        raise GraphAugmentationError("source evidence differs from the original circuit")
    if rename_devices(source, seed=augmentation.seed) != augmentation:
        raise GraphAugmentationError("rename manifest differs from deterministic source replay")
    return source
