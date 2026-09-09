"""Bounded graph-dataset lineage, conservative leakage grouping, and splitting.

The leakage bucket is intentionally coarser than graph isomorphism.  Equality is
only a reason to keep records together; inequality is not a proof of unrelated
circuits, and neither result says anything about electrical equivalence.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from itertools import chain

from topology_lantern.circuit import (
    CircuitDevice,
    CircuitGraph,
    CircuitGraphError,
    CircuitInstance,
    CircuitNet,
    CircuitPort,
    CircuitScope,
    SourceLocation,
)
from topology_lantern.graph_augmentation import DeviceRename, RenamedCircuit, restore_device_names
from topology_lantern.graph_codec import (
    CompactCircuitGraph,
    ConnectivityOwner,
    OwnerKind,
    compact_graph,
    compact_to_circuit,
)
from topology_lantern.graph_sequence import (
    CircuitGraphSequence,
    EulerTrail,
    SequenceScope,
    TraversalStep,
    decode_graph_sequence,
)

_BUCKET_PROFILE = "topology-lantern-conservative-leakage-wl-v1"
_MAX_SCOPES = 512
_MAX_SOURCES = 32
_MAX_OWNERS = 100_000
_MAX_NETS = 100_000
_MAX_EDGES = 500_000
_MAX_PARAMETERS = 4_096
_MAX_NESTED_PAIRS = 1_000_000
_MAX_TEXT_BYTES = 64 * 1024 * 1024
_MAX_WL_ROUNDS = 64
_MAX_RECORDS = 10_000
_MAX_DATASET_VERTICES = 1_000_000
_MAX_DATASET_EDGES = 2_000_000
_MAX_DATASET_NESTED_PAIRS = 4_000_000
_MAX_DATASET_TEXT_BYTES = 256 * 1024 * 1024
_MAX_ANCESTRY_DEPTH = 64
_MAX_WEIGHT = 1_000_000


class DatasetError(CircuitGraphError):
    """Dataset evidence, lineage, resource bounds, or partitioning is invalid."""


class DatasetRecordKind(StrEnum):
    """The exact operation that produced a dataset record."""

    ROOT = "root"
    DEVICE_RENAME = "device_rename"
    EULER_TRAVERSAL = "euler_traversal"


class DatasetPartition(StrEnum):
    """Supported leakage-exclusive dataset partitions."""

    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class LeakageBucket:
    """One bounded color-refinement bucket, not an isomorphism certificate."""

    bucket_id: str
    profile: str
    vertex_count: int
    edge_count: int
    rounds: int
    refinement_truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "bucket_id": self.bucket_id,
            "profile": self.profile,
            "vertex_count": self.vertex_count,
            "edge_count": self.edge_count,
            "rounds": self.rounds,
            "refinement_truncated": self.refinement_truncated,
        }


DatasetPayload = CircuitGraph | RenamedCircuit | CircuitGraphSequence


@dataclass(frozen=True, slots=True)
class GraphDatasetRecord:
    """A content-bound graph view with explicit root and immediate identities."""

    record_id: str
    kind: DatasetRecordKind
    root_lineage_id: str
    root_source_evidence_id: str
    immediate_graph_id: str
    immediate_source_evidence_id: str
    leakage_bucket: LeakageBucket
    parent_record_id: str | None
    seed: int | None
    derivation_evidence_id: str
    payload: DatasetPayload


@dataclass(frozen=True, slots=True)
class GraphDatasetSplit:
    """Canonical train/validation/test assignment of complete leakage groups."""

    split_id: str
    seed: int
    weights: tuple[int, int, int]
    train: tuple[GraphDatasetRecord, ...]
    validation: tuple[GraphDatasetRecord, ...]
    test: tuple[GraphDatasetRecord, ...]


@dataclass(frozen=True, slots=True)
class _Metrics:
    vertices: int
    edges: int
    nested_pairs: int
    text_bytes: int


@dataclass(frozen=True, slots=True)
class _PayloadInfo:
    graph: CircuitGraph
    predecessor_graph: CircuitGraph | None
    derivation_evidence_id: str
    seed: int | None


class _TextBudget:
    def __init__(self, maximum: int | None = None) -> None:
        self.maximum = _MAX_TEXT_BYTES if maximum is None else maximum
        self.total = 0

    def add(self, value: object, context: str, *, characters: int = 4_096) -> None:
        if type(value) is not str or not value or len(value) > characters:
            raise DatasetError(f"{context} must be bounded text")
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeError as error:
            raise DatasetError(f"{context} must be valid UTF-8 text") from error
        self.total += size
        if self.total > self.maximum:
            raise DatasetError("payload text exceeds its UTF-8 byte limit")


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _identity(value: object) -> str:
    return f"sha256:{sha256(_json(value).encode('ascii')).hexdigest()}"


def _sha(value: object, context: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise DatasetError(f"{context} must be a lowercase sha256: identity")
    return value


def _seed(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise DatasetError("dataset seed must be an unsigned 64-bit integer")
    return value


def _bucket_shape(value: object) -> LeakageBucket:
    if not isinstance(value, LeakageBucket):
        raise DatasetError("record leakage bucket has an invalid type")
    _sha(value.bucket_id, "leakage bucket identity")
    if type(value.profile) is not str or value.profile != _BUCKET_PROFILE:
        raise DatasetError("record leakage bucket has an unsupported profile")
    if any(
        type(count) is not int or count < 0
        for count in (value.vertex_count, value.edge_count, value.rounds)
    ):
        raise DatasetError("record leakage bucket counts must be non-negative integers")
    if value.rounds > _MAX_WL_ROUNDS:
        raise DatasetError("record leakage bucket round count exceeds its limit")
    if type(value.refinement_truncated) is not bool:
        raise DatasetError("record leakage bucket truncation flag must be a boolean")
    return value


def _record_shape(value: object) -> GraphDatasetRecord:
    if not isinstance(value, GraphDatasetRecord) or not isinstance(value.kind, DatasetRecordKind):
        raise DatasetError("dataset record has an invalid typed shape")
    for identity, context in (
        (value.record_id, "record identity"),
        (value.root_lineage_id, "root lineage id"),
        (value.root_source_evidence_id, "root source evidence id"),
        (value.immediate_graph_id, "immediate graph id"),
        (value.immediate_source_evidence_id, "immediate source evidence id"),
        (value.derivation_evidence_id, "derivation evidence id"),
    ):
        _sha(identity, context)
    if value.parent_record_id is not None:
        _sha(value.parent_record_id, "parent record id")
    if value.seed is not None:
        _seed(value.seed)
    _bucket_shape(value.leakage_bucket)
    return value


def _pairs(
    value: object,
    context: str,
    budget: _TextBudget,
    *,
    maximum: int = _MAX_PARAMETERS,
) -> int:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise DatasetError(f"{context} must be an immutable bounded tuple")
    for pair in value:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise DatasetError(f"{context} entries must be immutable pairs")
        budget.add(pair[0], f"{context} name", characters=256)
        budget.add(pair[1], f"{context} value", characters=256)
    return len(value)


def _source_text(source: object, context: str, budget: _TextBudget) -> None:
    if not isinstance(source, SourceLocation):
        raise DatasetError(f"{context} source location is invalid")
    budget.add(source.file, f"{context} source file")


def _graph_metrics(graph: object) -> _Metrics:
    """Bound shape and UTF-8 work before graph conversion allocates a second view."""

    if not isinstance(graph, CircuitGraph):
        raise DatasetError("dataset payload graph must be a CircuitGraph")
    if not isinstance(graph.scopes, tuple) or not 1 <= len(graph.scopes) <= _MAX_SCOPES:
        raise DatasetError("graph scopes must be an immutable bounded tuple")
    if not isinstance(graph.sources, tuple) or not 1 <= len(graph.sources) <= _MAX_SOURCES:
        raise DatasetError("graph sources must be an immutable bounded tuple")
    budget = _TextBudget()
    budget.add(graph.graph_id, "graph id", characters=256)
    budget.add(graph.top, "graph top", characters=256)
    for source in graph.sources:
        budget.add(source, "graph source")
    owners = nets = edges = nested_pairs = 0
    for scope in graph.scopes:
        if not isinstance(scope, CircuitScope):
            raise DatasetError("graph scope has an invalid type")
        collections = (scope.ports, scope.parameters, scope.nets, scope.devices, scope.instances)
        if any(not isinstance(collection, tuple) for collection in collections):
            raise DatasetError("graph collections must be immutable bounded tuples")
        owners += len(scope.ports) + len(scope.devices) + len(scope.instances)
        nets += len(scope.nets)
        edges += len(scope.ports)
        if owners > _MAX_OWNERS or nets > _MAX_NETS:
            raise DatasetError("graph owner or net count exceeds dataset limits")
        budget.add(scope.scope_id, "scope id", characters=256)
        budget.add(scope.name, "scope name", characters=256)
        nested_pairs += _pairs(scope.parameters, "scope parameters", budget)
        if nested_pairs > _MAX_NESTED_PAIRS:
            raise DatasetError("graph nested data exceeds dataset limits")
        for port in scope.ports:
            if not isinstance(port, CircuitPort):
                raise DatasetError("graph port has an invalid type")
            budget.add(port.node_id, "port id", characters=256)
            budget.add(port.name, "port name", characters=256)
            budget.add(port.net_id, "port net id", characters=256)
        for net in scope.nets:
            if not isinstance(net, CircuitNet):
                raise DatasetError("graph net has an invalid type")
            budget.add(net.node_id, "net id", characters=256)
            budget.add(net.name, "net name", characters=256)
        for device in scope.devices:
            if not isinstance(device, CircuitDevice):
                raise DatasetError("graph device has an invalid type")
            if not isinstance(device.terminals, tuple):
                raise DatasetError("device terminals must be an immutable bounded tuple")
            terminal_count = len(device.terminals)
            parameter_count = len(device.parameters) if isinstance(device.parameters, tuple) else 0
            if (
                edges + terminal_count > _MAX_EDGES
                or nested_pairs + terminal_count + parameter_count > _MAX_NESTED_PAIRS
            ):
                raise DatasetError("graph nested data exceeds dataset limits")
            edges += terminal_count
            budget.add(device.node_id, "device id", characters=256)
            budget.add(device.name, "device name", characters=256)
            if device.model is not None:
                budget.add(device.model, "device model", characters=256)
            nested_pairs += _pairs(device.terminals, "device terminals", budget, maximum=_MAX_EDGES)
            nested_pairs += _pairs(device.parameters, "device parameters", budget)
            _source_text(device.source, "device", budget)
            if edges > _MAX_EDGES or nested_pairs > _MAX_NESTED_PAIRS:
                raise DatasetError("graph nested data exceeds dataset limits")
        for instance in scope.instances:
            if not isinstance(instance, CircuitInstance):
                raise DatasetError("graph instance has an invalid type")
            if not isinstance(instance.connections, tuple):
                raise DatasetError("instance connections must be an immutable bounded tuple")
            connection_count = len(instance.connections)
            parameter_count = sum(
                len(value)
                for value in (instance.parameters, instance.effective_parameters)
                if isinstance(value, tuple)
            )
            if (
                edges + connection_count > _MAX_EDGES
                or nested_pairs + connection_count + parameter_count > _MAX_NESTED_PAIRS
            ):
                raise DatasetError("graph nested data exceeds dataset limits")
            edges += connection_count
            budget.add(instance.node_id, "instance id", characters=256)
            budget.add(instance.name, "instance name", characters=256)
            budget.add(instance.reference, "instance reference", characters=256)
            budget.add(instance.reference_scope_id, "instance reference scope", characters=256)
            nested_pairs += _pairs(
                instance.connections, "instance connections", budget, maximum=_MAX_EDGES
            )
            nested_pairs += _pairs(instance.parameters, "instance parameters", budget)
            nested_pairs += _pairs(
                instance.effective_parameters, "instance effective parameters", budget
            )
            _source_text(instance.source, "instance", budget)
            if edges > _MAX_EDGES or nested_pairs > _MAX_NESTED_PAIRS:
                raise DatasetError("graph nested data exceeds dataset limits")
    return _Metrics(len(graph.scopes) + owners + nets, edges, nested_pairs, budget.total)


def _sequence_metrics(sequence: object) -> _Metrics:
    """Bound a typed sequence before ``as_dict`` materializes its complete wire tree."""

    if not isinstance(sequence, CircuitGraphSequence):
        raise DatasetError("traversal payload must be a CircuitGraphSequence")
    if not isinstance(sequence.scopes, tuple) or not 1 <= len(sequence.scopes) <= _MAX_SCOPES:
        raise DatasetError("sequence scopes must be an immutable bounded tuple")
    if not isinstance(sequence.sources, tuple) or not 1 <= len(sequence.sources) <= _MAX_SOURCES:
        raise DatasetError("sequence sources must be an immutable bounded tuple")
    budget = _TextBudget()
    budget.add(sequence.sequence_id, "sequence id", characters=256)
    budget.add(sequence.source_graph_id, "sequence source graph id", characters=256)
    budget.add(sequence.top, "sequence top", characters=256)
    for source in sequence.sources:
        budget.add(source, "sequence source")
    owners = nets = steps = trails = nested_pairs = 0
    for scope in sequence.scopes:
        if not isinstance(scope, SequenceScope):
            raise DatasetError("sequence scope has an invalid type")
        if any(
            not isinstance(collection, tuple)
            for collection in (scope.parameters, scope.nets, scope.owners, scope.trails)
        ):
            raise DatasetError("sequence collections must be immutable bounded tuples")
        owners += len(scope.owners)
        nets += len(scope.nets)
        if owners > _MAX_OWNERS or nets > _MAX_NETS:
            raise DatasetError("sequence owner or net count exceeds dataset limits")
        budget.add(scope.scope_id, "sequence scope id", characters=256)
        budget.add(scope.name, "sequence scope name", characters=256)
        nested_pairs += _pairs(scope.parameters, "sequence scope parameters", budget)
        if nested_pairs > _MAX_NESTED_PAIRS:
            raise DatasetError("sequence nested data exceeds dataset limits")
        for net in scope.nets:
            if not isinstance(net, CircuitNet):
                raise DatasetError("sequence net has an invalid type")
            budget.add(net.node_id, "sequence net id", characters=256)
            budget.add(net.name, "sequence net name", characters=256)
        for owner in scope.owners:
            if not isinstance(owner, ConnectivityOwner):
                raise DatasetError("sequence owner has an invalid type")
            budget.add(owner.owner_id, "sequence owner id", characters=256)
            budget.add(owner.name, "sequence owner name", characters=256)
            for value, context in (
                (owner.model, "sequence owner model"),
                (owner.reference, "sequence owner reference"),
                (owner.reference_scope_id, "sequence owner reference scope"),
            ):
                if value is not None:
                    budget.add(value, context, characters=256)
            nested_pairs += _pairs(owner.parameters, "sequence owner parameters", budget)
            nested_pairs += _pairs(
                owner.effective_parameters, "sequence owner effective parameters", budget
            )
            if owner.source is not None:
                _source_text(owner.source, "sequence owner", budget)
            if nested_pairs > _MAX_NESTED_PAIRS:
                raise DatasetError("sequence nested data exceeds dataset limits")
        trails += len(scope.trails)
        if trails > _MAX_EDGES:
            raise DatasetError("sequence trail count exceeds dataset limits")
        for trail in scope.trails:
            if not isinstance(trail, EulerTrail) or not isinstance(trail.steps, tuple):
                raise DatasetError("sequence trails must contain immutable typed steps")
            steps += len(trail.steps)
            if trails + steps > _MAX_EDGES:
                raise DatasetError("sequence traversal work exceeds dataset limits")
            budget.add(trail.start, "sequence trail start", characters=256)
            for step in trail.steps:
                if not isinstance(step, TraversalStep):
                    raise DatasetError("sequence step has an invalid type")
                budget.add(step.edge_id, "sequence edge id", characters=256)
                budget.add(step.target, "sequence step target", characters=256)
                budget.add(step.terminal, "sequence terminal", characters=256)
    return _Metrics(
        len(sequence.scopes) + owners + nets,
        trails + steps,
        nested_pairs,
        budget.total,
    )


def _augmentation_metrics(augmentation: object) -> _Metrics:
    if not isinstance(augmentation, RenamedCircuit):
        raise DatasetError("rename payload must be a RenamedCircuit")
    metrics = _graph_metrics(augmentation.graph)
    if not isinstance(augmentation.renames, tuple) or len(augmentation.renames) > _MAX_OWNERS:
        raise DatasetError("rename manifest must be an immutable bounded tuple")
    budget = _TextBudget(_MAX_TEXT_BYTES - metrics.text_bytes)
    budget.add(augmentation.source_graph_id, "rename source graph id", characters=256)
    budget.add(augmentation.source_evidence_id, "rename source evidence id", characters=256)
    for entry in augmentation.renames:
        if not isinstance(entry, DeviceRename):
            raise DatasetError("rename manifest entry has an invalid type")
        for value, context in (
            (entry.scope_id, "rename scope id"),
            (entry.original_id, "rename original id"),
            (entry.original_name, "rename original name"),
            (entry.renamed_id, "rename target id"),
            (entry.renamed_name, "rename target name"),
        ):
            budget.add(value, context, characters=256)
    return replace(metrics, text_bytes=metrics.text_bytes + budget.total)


def _validated_graph(graph: CircuitGraph) -> tuple[CircuitGraph, CompactCircuitGraph]:
    _graph_metrics(graph)
    try:
        compact = compact_graph(graph)
        checked = compact_to_circuit(compact)
    except (CircuitGraphError, AttributeError, KeyError, TypeError, ValueError) as error:
        raise DatasetError(f"invalid dataset graph: {error}") from error
    if checked != graph:
        raise DatasetError("dataset graph is not in exact immutable canonical form")
    return checked, compact


def _graph_evidence_id(graph: CircuitGraph) -> str:
    return _identity(graph.as_dict())


def _augmentation_evidence_id(augmentation: RenamedCircuit) -> str:
    return _identity(
        {
            "kind": DatasetRecordKind.DEVICE_RENAME.value,
            "source_graph_id": augmentation.source_graph_id,
            "source_evidence_id": augmentation.source_evidence_id,
            "seed": augmentation.seed,
            "graph_evidence_id": _graph_evidence_id(augmentation.graph),
            "renames": [
                {
                    "scope_id": entry.scope_id,
                    "original_id": entry.original_id,
                    "original_name": entry.original_name,
                    "renamed_id": entry.renamed_id,
                    "renamed_name": entry.renamed_name,
                }
                for entry in augmentation.renames
            ],
        }
    )


def _hash_parts(parts: Iterable[str]) -> str:
    digest = sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _bucket_from_compact(graph: CompactCircuitGraph) -> LeakageBucket:
    descriptors: list[tuple[object, ...]] = []
    adjacency: list[list[tuple[str, int]]] = []

    def vertex(descriptor: tuple[object, ...]) -> int:
        index = len(descriptors)
        descriptors.append(descriptor)
        adjacency.append([])
        return index

    def link(left: int, right: int, outward: str, inward: str) -> None:
        adjacency[left].append((outward, right))
        adjacency[right].append((inward, left))

    scope_vertices = {
        scope.scope_id: vertex(("scope", scope.name == graph.top)) for scope in graph.scopes
    }
    global_port_flags: dict[str, bool] = {}
    for scope in graph.scopes:
        for net in scope.nets:
            if net.is_global:
                global_port_flags[net.node_id] = (
                    global_port_flags.get(net.node_id, False) or net.is_port
                )
    net_vertices: dict[tuple[str, str], int] = {}
    owner_vertices: dict[str, int] = {}
    formal_indexes_by_scope = {
        scope.scope_id: {
            owner.name: owner.index
            for owner in scope.owners
            if owner.kind is OwnerKind.PORT and owner.index is not None
        }
        for scope in graph.scopes
    }
    for scope in graph.scopes:
        scope_vertex = scope_vertices[scope.scope_id]
        for net in scope.nets:
            key = ("global", net.node_id) if net.is_global else (scope.scope_id, net.node_id)
            net_vertex = net_vertices.get(key)
            if net_vertex is None:
                is_port = global_port_flags[net.node_id] if net.is_global else net.is_port
                net_vertex = vertex(("net", is_port, net.is_global))
                net_vertices[key] = net_vertex
            link(scope_vertex, net_vertex, "contains:net", "within:scope")
        for owner in scope.owners:
            descriptor: tuple[object, ...]
            if owner.kind is OwnerKind.PORT:
                descriptor = ("port", owner.index)
            elif owner.kind is OwnerKind.DEVICE:
                descriptor = (
                    "device",
                    owner.element_kind.value if owner.element_kind is not None else "",
                )
            else:
                descriptor = ("instance",)
            owner_vertex = vertex(descriptor)
            owner_vertices[owner.owner_id] = owner_vertex
            link(scope_vertex, owner_vertex, "contains:owner", "within:scope")
    for scope in graph.scopes:
        owners_by_id = {owner.owner_id: owner for owner in scope.owners}
        global_net_ids = {net.node_id for net in scope.nets if net.is_global}
        for owner in scope.owners:
            if owner.kind is not OwnerKind.INSTANCE or owner.reference_scope_id is None:
                continue
            link(
                owner_vertices[owner.owner_id],
                scope_vertices[owner.reference_scope_id],
                "references:scope",
                "referenced-by:instance",
            )
        for edge in scope.edges:
            owner = owners_by_id[edge.owner_id]
            if owner.kind is OwnerKind.INSTANCE:
                if owner.reference_scope_id is None:
                    raise DatasetError("validated instance is missing its referenced scope")
                terminal = (
                    "instance-port:"
                    f"{formal_indexes_by_scope[owner.reference_scope_id][edge.terminal]}"
                )
            elif owner.kind is OwnerKind.PORT:
                terminal = "port"
            else:
                terminal = f"device-terminal:{edge.ordinal}:{edge.terminal}"
            net_key = (
                ("global", edge.net_id)
                if edge.net_id in global_net_ids
                else (scope.scope_id, edge.net_id)
            )
            link(
                owner_vertices[edge.owner_id],
                net_vertices[net_key],
                f"incidence:{terminal}",
                f"incident-from:{terminal}",
            )

    edge_count = sum(len(neighbors) for neighbors in adjacency) // 2

    colors = [_identity(descriptor) for descriptor in descriptors]
    class_count = len(set(colors))
    rounds = 0
    stable = not colors
    for _round in range(min(len(colors), _MAX_WL_ROUNDS)):
        refined = []
        for index, color in enumerate(colors):
            neighbors = sorted((label, colors[target]) for label, target in adjacency[index])
            parts = chain((color,), (f"{label}:{neighbor}" for label, neighbor in neighbors))
            refined.append(_hash_parts(parts))
        rounds += 1
        next_count = len(set(refined))
        colors = refined
        if next_count == class_count:
            stable = True
            break
        class_count = next_count
    histogram = sorted(Counter(colors).items())
    body = {
        "profile": _BUCKET_PROFILE,
        "vertex_count": len(colors),
        "edge_count": edge_count,
        "rounds": rounds,
        "refinement_truncated": not stable,
        "color_histogram": histogram,
    }
    return LeakageBucket(
        _identity(body),
        _BUCKET_PROFILE,
        len(colors),
        edge_count,
        rounds,
        not stable,
    )


def conservative_leakage_bucket(graph: CircuitGraph) -> LeakageBucket:
    """Return a rename-invariant bucket used only to prevent dataset leakage."""

    _checked, compact = _validated_graph(graph)
    return _bucket_from_compact(compact)


def _record_body(record: GraphDatasetRecord) -> dict[str, object]:
    return {
        "schema": "org.topology-lantern.graph-dataset-record",
        "version": 1,
        "kind": record.kind.value,
        "root_lineage_id": record.root_lineage_id,
        "root_source_evidence_id": record.root_source_evidence_id,
        "immediate_graph_id": record.immediate_graph_id,
        "immediate_source_evidence_id": record.immediate_source_evidence_id,
        "leakage_bucket": record.leakage_bucket.as_dict(),
        "parent_record_id": record.parent_record_id,
        "seed": record.seed,
        "derivation_evidence_id": record.derivation_evidence_id,
    }


def _make_record(
    kind: DatasetRecordKind,
    payload: DatasetPayload,
    graph: CircuitGraph,
    derivation_evidence_id: str,
    seed: int | None,
    parent: GraphDatasetRecord | None,
    *,
    bucket: LeakageBucket | None = None,
) -> GraphDatasetRecord:
    immediate_evidence = _graph_evidence_id(graph)
    if parent is None:
        root_lineage = graph.graph_id
        root_evidence = immediate_evidence
        parent_id = None
    else:
        root_lineage = parent.root_lineage_id
        root_evidence = parent.root_source_evidence_id
        parent_id = parent.record_id
    record = GraphDatasetRecord(
        "",
        kind,
        root_lineage,
        root_evidence,
        graph.graph_id,
        immediate_evidence,
        bucket or conservative_leakage_bucket(graph),
        parent_id,
        seed,
        derivation_evidence_id,
        payload,
    )
    return replace(record, record_id=_identity(_record_body(record)))


def root_dataset_record(graph: CircuitGraph) -> GraphDatasetRecord:
    """Create a validated root record from one canonical circuit graph."""

    graph, compact = _validated_graph(graph)
    evidence = _graph_evidence_id(graph)
    return _make_record(
        DatasetRecordKind.ROOT,
        graph,
        graph,
        evidence,
        None,
        None,
        bucket=_bucket_from_compact(compact),
    )


def _record_payload_info(record: GraphDatasetRecord) -> _PayloadInfo:
    if record.kind is DatasetRecordKind.ROOT:
        if not isinstance(record.payload, CircuitGraph):
            raise DatasetError("root record payload must be a CircuitGraph")
        graph, _compact = _validated_graph(record.payload)
        return _PayloadInfo(graph, None, _graph_evidence_id(graph), None)
    if record.kind is DatasetRecordKind.DEVICE_RENAME:
        if not isinstance(record.payload, RenamedCircuit):
            raise DatasetError("rename record payload must be a RenamedCircuit")
        _augmentation_metrics(record.payload)
        try:
            predecessor = restore_device_names(record.payload)
        except (CircuitGraphError, AttributeError, TypeError, ValueError) as error:
            raise DatasetError(f"invalid rename derivation: {error}") from error
        graph, _compact = _validated_graph(record.payload.graph)
        return _PayloadInfo(
            graph,
            predecessor,
            _augmentation_evidence_id(record.payload),
            record.payload.seed,
        )
    if not isinstance(record.payload, CircuitGraphSequence):
        raise DatasetError("traversal record payload must be a CircuitGraphSequence")
    _sequence_metrics(record.payload)
    try:
        graph = compact_to_circuit(decode_graph_sequence(record.payload))
    except (CircuitGraphError, AttributeError, TypeError, ValueError) as error:
        raise DatasetError(f"invalid traversal derivation: {error}") from error
    return _PayloadInfo(graph, graph, record.payload.sequence_id, record.payload.seed)


def _validated_record(
    record: GraphDatasetRecord,
    bucket_cache: dict[str, LeakageBucket],
) -> _PayloadInfo:
    _record_shape(record)
    info = _record_payload_info(record)
    if record.immediate_graph_id != info.graph.graph_id:
        raise DatasetError("record immediate graph identity does not match its payload")
    immediate_evidence = _graph_evidence_id(info.graph)
    if record.immediate_source_evidence_id != immediate_evidence:
        raise DatasetError("record immediate source evidence does not match its payload")
    if record.derivation_evidence_id != info.derivation_evidence_id or record.seed != info.seed:
        raise DatasetError("record derivation evidence does not match its payload")
    expected_bucket = bucket_cache.get(immediate_evidence)
    if expected_bucket is None:
        expected_bucket = conservative_leakage_bucket(info.graph)
        bucket_cache[immediate_evidence] = expected_bucket
    if record.leakage_bucket != expected_bucket:
        raise DatasetError("record leakage bucket does not match its graph")
    if record.record_id != _identity(_record_body(record)):
        raise DatasetError("record identity does not match its body")
    return info


def renamed_dataset_record(
    parent: GraphDatasetRecord, augmentation: RenamedCircuit
) -> GraphDatasetRecord:
    """Create a child record after proving exact deterministic device renaming."""

    parent_info = _validated_record(parent, {})
    _augmentation_metrics(augmentation)
    try:
        predecessor = restore_device_names(augmentation)
    except (CircuitGraphError, AttributeError, TypeError, ValueError) as error:
        raise DatasetError(f"invalid rename derivation: {error}") from error
    if predecessor != parent_info.graph:
        raise DatasetError("rename derivation does not restore its exact parent graph")
    graph, compact = _validated_graph(augmentation.graph)
    return _make_record(
        DatasetRecordKind.DEVICE_RENAME,
        augmentation,
        graph,
        _augmentation_evidence_id(augmentation),
        augmentation.seed,
        parent,
        bucket=_bucket_from_compact(compact),
    )


def traversal_dataset_record(
    parent: GraphDatasetRecord, sequence: CircuitGraphSequence
) -> GraphDatasetRecord:
    """Create a child record after exact sequence decode against its parent graph."""

    parent_info = _validated_record(parent, {})
    _sequence_metrics(sequence)
    try:
        graph = compact_to_circuit(decode_graph_sequence(sequence))
    except (CircuitGraphError, AttributeError, TypeError, ValueError) as error:
        raise DatasetError(f"invalid traversal derivation: {error}") from error
    if graph != parent_info.graph:
        raise DatasetError("traversal derivation does not decode to its exact parent graph")
    return _make_record(
        DatasetRecordKind.EULER_TRAVERSAL,
        sequence,
        graph,
        sequence.sequence_id,
        sequence.seed,
        parent,
        bucket=parent.leakage_bucket,
    )


def _payload_metrics(payload: object) -> _Metrics:
    if isinstance(payload, CircuitGraph):
        return _graph_metrics(payload)
    if isinstance(payload, RenamedCircuit):
        return _augmentation_metrics(payload)
    if isinstance(payload, CircuitGraphSequence):
        return _sequence_metrics(payload)
    raise DatasetError("dataset record payload has an invalid type")


def _preflight_records(records: object) -> tuple[GraphDatasetRecord, ...]:
    if not isinstance(records, tuple):
        raise DatasetError("dataset records must be an immutable bounded tuple")
    if not 1 <= len(records) <= _MAX_RECORDS:
        raise DatasetError("dataset record count exceeds its limits")
    vertices = edges = nested_pairs = text_bytes = 0
    record_ids: set[str] = set()
    for record in records:
        record = _record_shape(record)
        if record.record_id in record_ids:
            raise DatasetError("dataset contains duplicate record identities")
        record_ids.add(record.record_id)
        metrics = _payload_metrics(record.payload)
        vertices += metrics.vertices
        edges += metrics.edges
        nested_pairs += metrics.nested_pairs
        text_bytes += metrics.text_bytes
        if (
            vertices > _MAX_DATASET_VERTICES
            or edges > _MAX_DATASET_EDGES
            or nested_pairs > _MAX_DATASET_NESTED_PAIRS
            or text_bytes > _MAX_DATASET_TEXT_BYTES
        ):
            raise DatasetError("dataset aggregate payload exceeds its limits")
    return records


def validate_graph_dataset(
    records: tuple[GraphDatasetRecord, ...],
) -> tuple[GraphDatasetRecord, ...]:
    """Validate content identities, exact derivations, and a bounded ancestry DAG."""

    records = _preflight_records(records)
    by_id: dict[str, GraphDatasetRecord] = {}
    infos: dict[str, _PayloadInfo] = {}
    bucket_cache: dict[str, LeakageBucket] = {}
    for record in records:
        info = _validated_record(record, bucket_cache)
        if record.record_id in by_id:
            raise DatasetError("dataset contains duplicate record identities")
        by_id[record.record_id] = record
        infos[record.record_id] = info
    for record in records:
        info = infos[record.record_id]
        if record.kind is DatasetRecordKind.ROOT:
            if record.parent_record_id is not None or record.seed is not None:
                raise DatasetError("root record cannot declare a parent or derivation seed")
            if (
                record.root_lineage_id != record.immediate_graph_id
                or record.root_source_evidence_id != record.immediate_source_evidence_id
            ):
                raise DatasetError("root record lineage does not match its source graph")
            continue
        if record.parent_record_id is None or record.parent_record_id not in by_id:
            raise DatasetError("derived dataset record has a missing parent")
        parent = by_id[record.parent_record_id]
        parent_info = infos[parent.record_id]
        if (
            record.root_lineage_id != parent.root_lineage_id
            or record.root_source_evidence_id != parent.root_source_evidence_id
        ):
            raise DatasetError("derived record does not preserve its root lineage")
        if info.predecessor_graph != parent_info.graph:
            raise DatasetError("derived record evidence does not match its parent graph")
    for record in records:
        seen: set[str] = set()
        current = record
        depth = 0
        while current.parent_record_id is not None:
            if current.record_id in seen:
                raise DatasetError("dataset derivation ancestry contains a cycle")
            seen.add(current.record_id)
            depth += 1
            if depth > _MAX_ANCESTRY_DEPTH:
                raise DatasetError("dataset derivation ancestry exceeds its depth limit")
            current = by_id[current.parent_record_id]
        if current.kind is not DatasetRecordKind.ROOT:
            raise DatasetError("dataset derivation ancestry does not terminate at a root")
    return tuple(sorted(records, key=lambda record: record.record_id))


def _weights(value: object) -> tuple[int, int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or any(type(weight) is not int or not 0 <= weight <= _MAX_WEIGHT for weight in value)
        or sum(value) == 0
    ):
        raise DatasetError("split weights must be three bounded non-negative integers")
    return value


def _component_indexes(records: tuple[GraphDatasetRecord, ...]) -> tuple[tuple[int, ...], ...]:
    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    by_record = {record.record_id: index for index, record in enumerate(records)}
    seen_keys: dict[tuple[str, str], int] = {}
    for index, record in enumerate(records):
        if record.parent_record_id is not None:
            union(index, by_record[record.parent_record_id])
        for key in (
            ("root-lineage", record.root_lineage_id),
            ("root-evidence", record.root_source_evidence_id),
            ("immediate-graph", record.immediate_graph_id),
            ("leakage-bucket", record.leakage_bucket.bucket_id),
        ):
            prior = seen_keys.setdefault(key, index)
            union(index, prior)
    components: dict[int, list[int]] = {}
    for index in range(len(records)):
        components.setdefault(find(index), []).append(index)
    return tuple(
        tuple(component)
        for _root, component in sorted(
            components.items(),
            key=lambda item: min(records[index].record_id for index in item[1]),
        )
    )


def _split_body(split: GraphDatasetSplit) -> dict[str, object]:
    return {
        "schema": "org.topology-lantern.graph-dataset-split",
        "version": 1,
        "seed": split.seed,
        "weights": list(split.weights),
        "train": [record.record_id for record in split.train],
        "validation": [record.record_id for record in split.validation],
        "test": [record.record_id for record in split.test],
    }


def _build_split(
    seed: int,
    weights: tuple[int, int, int],
    train: tuple[GraphDatasetRecord, ...],
    validation: tuple[GraphDatasetRecord, ...],
    test: tuple[GraphDatasetRecord, ...],
) -> GraphDatasetSplit:
    split = GraphDatasetSplit("", seed, weights, train, validation, test)
    return replace(split, split_id=_identity(_split_body(split)))


def _split_partitions(
    split: object,
) -> tuple[
    tuple[GraphDatasetRecord, ...],
    tuple[GraphDatasetRecord, ...],
    tuple[GraphDatasetRecord, ...],
]:
    if not isinstance(split, GraphDatasetSplit):
        raise DatasetError("dataset split has an invalid type")
    partitions = (split.train, split.validation, split.test)
    if any(not isinstance(records, tuple) for records in partitions):
        raise DatasetError("dataset split partitions must be immutable tuples")
    if (
        any(len(records) > _MAX_RECORDS for records in partitions)
        or sum(len(records) for records in partitions) > _MAX_RECORDS
    ):
        raise DatasetError("dataset split record count exceeds its limits")
    return partitions


def _validate_split(split: GraphDatasetSplit) -> GraphDatasetSplit:
    partitions = _split_partitions(split)
    _sha(split.split_id, "split identity")
    seed = _seed(split.seed)
    weights = _weights(split.weights)
    records = tuple(record for partition in partitions for record in partition)
    if not records:
        raise DatasetError("dataset split must contain at least one record")
    validated = validate_graph_dataset(records)
    if any(
        partition != tuple(sorted(partition, key=lambda record: record.record_id))
        for partition in partitions
    ):
        raise DatasetError("dataset split partition order is not canonical")
    partition_by_record = {
        record.record_id: tuple(DatasetPartition)[index]
        for index, partition in enumerate(partitions)
        for record in partition
    }
    if len(partition_by_record) != len(records):
        raise DatasetError("dataset split repeats a record")
    validated_by_id = {record.record_id: record for record in validated}
    normalized = tuple(validated_by_id[record.record_id] for record in records)
    for component in _component_indexes(normalized):
        assigned = {partition_by_record[normalized[index].record_id] for index in component}
        if len(assigned) != 1:
            raise DatasetError("dataset leakage group spans partitions")
    if split.split_id != _identity(_split_body(split)):
        raise DatasetError("split identity does not match its body")
    return GraphDatasetSplit(
        split.split_id,
        seed,
        weights,
        split.train,
        split.validation,
        split.test,
    )


def split_graph_dataset(
    records: tuple[GraphDatasetRecord, ...],
    *,
    seed: int = 0,
    weights: tuple[int, int, int] = (8, 1, 1),
) -> GraphDatasetSplit:
    """Assign complete lineage/leakage components by a deterministic hash."""

    seed = _seed(seed)
    weights = _weights(weights)
    records = validate_graph_dataset(records)
    assigned: list[list[GraphDatasetRecord]] = [[], [], []]
    total = sum(weights)
    thresholds = (weights[0], weights[0] + weights[1])
    for component in _component_indexes(records):
        component_ids = sorted(records[index].record_id for index in component)
        component_key = _identity(component_ids)
        slot = (
            int.from_bytes(
                sha256(seed.to_bytes(8, "big") + component_key.encode("ascii")).digest()[:8],
                "big",
            )
            % total
        )
        partition = 0 if slot < thresholds[0] else 1 if slot < thresholds[1] else 2
        assigned[partition].extend(records[index] for index in component)
    canonical = tuple(
        tuple(sorted(group, key=lambda record: record.record_id)) for group in assigned
    )
    return _build_split(seed, weights, canonical[0], canonical[1], canonical[2])


def stack_dataset_splits(*splits: GraphDatasetSplit) -> GraphDatasetSplit:
    """Stack compatible splits and reject duplicate or cross-partition leakage."""

    if not 1 <= len(splits) <= _MAX_RECORDS:
        raise DatasetError("stack requires a bounded non-empty split collection")
    raw_partitions = tuple(_split_partitions(split) for split in splits)
    for split in splits:
        _sha(split.split_id, "split identity")
        _seed(split.seed)
        _weights(split.weights)
    seed, weights = splits[0].seed, splits[0].weights
    if any(split.seed != seed or split.weights != weights for split in splits[1:]):
        raise DatasetError("stacked dataset splits must use identical seed and weights")
    if sum(sum(len(partition) for partition in partitions) for partitions in raw_partitions) > (
        _MAX_RECORDS
    ):
        raise DatasetError("stacked dataset record count exceeds its limits")
    combined_records = tuple(
        record for partitions in raw_partitions for partition in partitions for record in partition
    )
    _preflight_records(combined_records)
    checked = tuple(_validate_split(split) for split in splits)
    partitions = tuple(
        tuple(
            sorted(
                (record for split in checked for record in getattr(split, partition.value)),
                key=lambda record: record.record_id,
            )
        )
        for partition in DatasetPartition
    )
    return _validate_split(_build_split(seed, weights, partitions[0], partitions[1], partitions[2]))


def validate_dataset_split(split: GraphDatasetSplit) -> GraphDatasetSplit:
    """Validate split identity, record DAG, canonical order, and leakage exclusion."""

    return _validate_split(split)
