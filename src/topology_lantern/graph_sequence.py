"""Reversible Euler-trail coding of terminal-labelled circuit multigraphs.

Metadata is retained once; connectivity is represented only by ordered walks.
Odd-degree vertices are paired with temporary edges for Hierholzer traversal.
Removing those edges gives the minimum number of open trails per component.
Temporary edges never appear in the wire format.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import cast

from topology_lantern.circuit import CircuitNet
from topology_lantern.graph_codec import (
    CompactCircuitGraph,
    CompactEdge,
    CompactScopeGraph,
    ConnectivityOwner,
    ConnectivityRepresentationError,
    parse_connectivity_graph,
    validate_compact_graph,
)

_SCHEMA = "org.topology-lantern.euler-sequence"
_MAX_BYTES = 64 * 1024 * 1024
_MAX_STEPS = 500_000
_MAX_SCOPES = 512


class GraphSequenceError(ConnectivityRepresentationError):
    """A sequence fails its bounds, walk, identity, or reconstruction contract."""


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _identity(value: object) -> str:
    return "sha256:" + sha256(_json(value).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class TraversalStep:
    edge_id: str
    target: str
    terminal: str
    ordinal: int

    def as_dict(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "target": self.target,
            "terminal": self.terminal,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class EulerTrail:
    start: str
    steps: tuple[TraversalStep, ...]

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "steps": [step.as_dict() for step in self.steps]}


@dataclass(frozen=True, slots=True)
class SequenceScope:
    scope_id: str
    name: str
    parameters: tuple[tuple[str, str], ...]
    nets: tuple[CircuitNet, ...]
    owners: tuple[ConnectivityOwner, ...]
    trails: tuple[EulerTrail, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.scope_id,
            "name": self.name,
            "parameters": dict(self.parameters),
            "nets": [net.as_dict() for net in self.nets],
            "owners": [owner.as_dict() for owner in self.owners],
            "trails": [trail.as_dict() for trail in self.trails],
        }


@dataclass(frozen=True, slots=True)
class CircuitGraphSequence:
    sequence_id: str
    source_graph_id: str
    top: str
    sources: tuple[str, ...]
    seed: int
    scopes: tuple[SequenceScope, ...]

    def body_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "version": 1,
            "source_graph_id": self.source_graph_id,
            "top": self.top,
            "sources": list(self.sources),
            "seed": self.seed,
            "scopes": [scope.as_dict() for scope in self.scopes],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body_dict(), "sequence_id": self.sequence_id}


def _seed(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise GraphSequenceError("seed must be an unsigned 64-bit integer")
    return value


def _priority(seed: int, label: str) -> bytes:
    return sha256(seed.to_bytes(8, "big") + label.encode("utf-8")).digest()


def _trails(scope: CompactScopeGraph, seed: int) -> tuple[EulerTrail, ...]:
    endpoints = [(edge.owner_id, edge.net_id) for edge in scope.edges]
    adjacency: dict[str, list[int]] = defaultdict(list)
    for index, (left, right) in enumerate(endpoints):
        adjacency[left].append(index)
        adjacency[right].append(index)

    components: list[list[str]] = []
    unseen = set(adjacency)
    for root in sorted(adjacency, key=lambda node: (_priority(seed, node), node)):
        if root not in unseen:
            continue
        unseen.remove(root)
        pending = [root]
        component: list[str] = []
        while pending:
            node = pending.pop()
            component.append(node)
            for index in adjacency[node]:
                left, right = endpoints[index]
                other = right if left == node else left
                if other in unseen:
                    unseen.remove(other)
                    pending.append(other)
        components.append(component)

    real_count = len(endpoints)
    for component in components:
        odd = sorted(
            (node for node in component if len(adjacency[node]) % 2),
            key=lambda node: (_priority(seed, node), node),
        )
        for left, right in zip(odd[::2], odd[1::2], strict=True):
            index = len(endpoints)
            endpoints.append((left, right))
            adjacency[left].append(index)
            adjacency[right].append(index)
    priorities = [_priority(seed, f"{scope.scope_id}:{index}") for index in range(len(endpoints))]
    for edges in adjacency.values():
        edges.sort(key=lambda index: (priorities[index], index), reverse=True)
    used: set[int] = set()
    result: list[EulerTrail] = []
    for component in components:
        vertices = [component[0]]
        entered: list[int] = []
        reverse_walk: list[tuple[int, str, str]] = []
        while vertices:
            node = vertices[-1]
            incident = adjacency[node]
            while incident and incident[-1] in used:
                incident.pop()
            if incident:
                index = incident.pop()
                used.add(index)
                left, right = endpoints[index]
                vertices.append(right if node == left else left)
                entered.append(index)
            else:
                target = vertices.pop()
                if entered:
                    reverse_walk.append((entered.pop(), vertices[-1], target))
        walk = list(reversed(reverse_walk))
        virtual = next((index for index, item in enumerate(walk) if item[0] >= real_count), None)
        if virtual is not None:
            walk = walk[virtual + 1 :] + walk[: virtual + 1]
        steps: list[TraversalStep] = []
        start = ""
        for index, source, target in walk:
            if index >= real_count:
                if steps:
                    result.append(EulerTrail(start, tuple(steps)))
                    steps = []
            else:
                if not steps:
                    start = source
                edge = scope.edges[index]
                steps.append(TraversalStep(edge.edge_id, target, edge.terminal, edge.ordinal))
        if steps:
            result.append(EulerTrail(start, tuple(steps)))
    return tuple(result)


def encode_graph_sequence(graph: CompactCircuitGraph, *, seed: int = 0) -> CircuitGraphSequence:
    """Cover each incidence exactly once with deterministic minimum-count trails."""

    seed = _seed(seed)
    validate_compact_graph(graph)
    scopes = tuple(
        SequenceScope(
            scope.scope_id,
            scope.name,
            scope.parameters,
            scope.nets,
            scope.owners,
            _trails(scope, seed),
        )
        for scope in graph.scopes
    )
    result = CircuitGraphSequence("", graph.source_graph_id, graph.top, graph.sources, seed, scopes)
    result = replace(result, sequence_id=_identity(result.body_dict()))
    # Encoding is also constrained by the exact public serialization envelope.
    if len(_json(result.as_dict())) > _MAX_BYTES:
        raise GraphSequenceError("graph sequence exceeds its byte limit")
    return result


def _object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise GraphSequenceError(f"{context} has an invalid object shape")
    return cast(dict[str, object], value)


def _array(value: object, maximum: int, context: str) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise GraphSequenceError(f"{context} must be a bounded array")
    return cast(list[object], value)


def _text(value: object, context: str) -> str:
    if type(value) is not str or not value or len(value) > 4_096:
        raise GraphSequenceError(f"{context} must be bounded non-empty text")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise GraphSequenceError("duplicate graph sequence member")
        result[name] = value
    return result


def _constant(value: str) -> object:
    raise GraphSequenceError("non-finite graph sequence number")


def _minimum_trail_count(scope: CompactScopeGraph) -> int:
    """Check the degree lower bound independently of the Euler construction."""
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in scope.edges:
        adjacency[edge.owner_id].append(edge.net_id)
        adjacency[edge.net_id].append(edge.owner_id)
    unseen = set(adjacency)
    count = 0
    while unseen:
        pending = [unseen.pop()]
        odd = 0
        while pending:
            node = pending.pop()
            odd += len(adjacency[node]) % 2
            for other in adjacency[node]:
                if other in unseen:
                    unseen.remove(other)
                    pending.append(other)
        count += max(1, odd // 2)
    return count


def _parse(document: str | bytes) -> tuple[CircuitGraphSequence, CompactCircuitGraph]:
    if type(document) not in {str, bytes} or len(document) > _MAX_BYTES:
        raise GraphSequenceError("graph sequence requires bounded UTF-8 JSON")
    try:
        text = document.decode("utf-8") if isinstance(document, bytes) else document
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise GraphSequenceError("graph sequence exceeds its byte limit")
        value: object = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise GraphSequenceError(f"invalid graph sequence JSON: {exc}") from exc
    root = _object(
        value,
        {"schema", "version", "sequence_id", "source_graph_id", "top", "sources", "seed", "scopes"},
        "graph sequence",
    )
    if root["schema"] != _SCHEMA or type(root["version"]) is not int or root["version"] != 1:
        raise GraphSequenceError("unsupported graph sequence schema or version")
    seed = _seed(root["seed"])
    body = {key: item for key, item in root.items() if key != "sequence_id"}
    if root["sequence_id"] != _identity(body):
        raise GraphSequenceError("sequence identity does not match its body")
    compact_scopes: list[dict[str, object]] = []
    all_trails: list[tuple[EulerTrail, ...]] = []
    total_steps = 0
    for raw_scope in _array(root["scopes"], _MAX_SCOPES, "scopes"):
        scope = _object(
            raw_scope, {"id", "name", "parameters", "nets", "owners", "trails"}, "scope"
        )
        raw_owners = _array(scope["owners"], 100_000, "owners")
        raw_nets = _array(scope["nets"], 100_000, "nets")
        owner_order: dict[str, int] = {}
        for index, owner in enumerate(raw_owners):
            if type(owner) is not dict:
                raise GraphSequenceError("owner must be an object")
            owner_order[_text(owner.get("id"), "owner ID")] = index
        net_ids: set[str] = set()
        for net in raw_nets:
            if type(net) is not dict:
                raise GraphSequenceError("net must be an object")
            net_ids.add(_text(net.get("id"), "net ID"))
        edges: list[CompactEdge] = []
        trails: list[EulerTrail] = []
        for raw_trail in _array(scope["trails"], _MAX_STEPS, "trails"):
            trail = _object(raw_trail, {"start", "steps"}, "trail")
            start = _text(trail["start"], "trail start")
            current = start
            steps: list[TraversalStep] = []
            for raw_step in _array(trail["steps"], _MAX_STEPS, "steps"):
                total_steps += 1
                if total_steps > _MAX_STEPS:
                    raise GraphSequenceError("graph sequence exceeds its total step limit")
                step = _object(raw_step, {"edge_id", "target", "terminal", "ordinal"}, "step")
                target = _text(step["target"], "step target")
                if current in owner_order and target in net_ids:
                    owner_id, net_id = current, target
                elif current in net_ids and target in owner_order:
                    owner_id, net_id = target, current
                else:
                    raise GraphSequenceError("trail must alternate known owner and net vertices")
                ordinal = step["ordinal"]
                if type(ordinal) is not int or not 0 <= ordinal < _MAX_STEPS:
                    raise GraphSequenceError("step ordinal must be a bounded non-negative integer")
                edge_id = _text(step["edge_id"], "edge ID")
                terminal = _text(step["terminal"], "terminal")
                edges.append(CompactEdge(edge_id, owner_id, terminal, ordinal, net_id))
                steps.append(TraversalStep(edge_id, target, terminal, ordinal))
                current = target
            if not steps:
                raise GraphSequenceError(
                    "empty trails are forbidden; isolated vertices remain in metadata"
                )
            trails.append(EulerTrail(start, tuple(steps)))
        edges.sort(key=lambda edge: (owner_order[edge.owner_id], edge.ordinal))
        compact_scopes.append(
            {
                **{key: item for key, item in scope.items() if key != "trails"},
                "edges": [edge.as_dict() for edge in edges],
            }
        )
        all_trails.append(tuple(trails))
    compact_body = {
        "schema": "org.topology-lantern.connectivity-graph",
        "version": 1,
        "view": "compact",
        "source_graph_id": root["source_graph_id"],
        "top": root["top"],
        "sources": root["sources"],
        "scopes": compact_scopes,
    }
    decoded = parse_connectivity_graph(
        _json({**compact_body, "representation_id": _identity(compact_body)})
    )
    if not isinstance(decoded, CompactCircuitGraph):
        raise GraphSequenceError("sequence failed compact reconstruction")
    for scope_graph, scope_trails in zip(decoded.scopes, all_trails, strict=True):
        if len(scope_trails) != _minimum_trail_count(scope_graph):
            raise GraphSequenceError("sequence does not use the minimum trail count")
    result = CircuitGraphSequence(
        _text(root["sequence_id"], "sequence ID"),
        decoded.source_graph_id,
        decoded.top,
        decoded.sources,
        seed,
        tuple(
            SequenceScope(
                scope.scope_id, scope.name, scope.parameters, scope.nets, scope.owners, trails
            )
            for scope, trails in zip(decoded.scopes, all_trails, strict=True)
        ),
    )
    return result, decoded


def parse_graph_sequence(document: str | bytes) -> CircuitGraphSequence:
    """Decode a strict sequence and prove exact graph reconstruction."""

    return _parse(document)[0]


def decode_graph_sequence(sequence: CircuitGraphSequence) -> CompactCircuitGraph:
    """Rebuild connectivity exclusively from the ordered traversal steps."""

    try:
        return _parse(_json(sequence.as_dict()))[1]
    except (AttributeError, TypeError, ValueError, RecursionError) as exc:
        raise GraphSequenceError(f"invalid graph sequence: {exc}") from exc


def graph_sequence_json(sequence: CircuitGraphSequence, *, pretty: bool = False) -> str:
    """Validate and serialize a sequence within the public byte envelope."""

    decode_graph_sequence(sequence)
    text = json.dumps(
        sequence.as_dict(), indent=2 if pretty else None, sort_keys=True, ensure_ascii=True
    )
    if len(text) + 1 > _MAX_BYTES:
        raise GraphSequenceError("serialized graph sequence exceeds its byte limit")
    return text + "\n"


def load_graph_sequence(path: str | Path) -> CircuitGraphSequence:
    """Read at most the sequence byte allowance plus one overflow byte."""

    try:
        with Path(path).open("rb") as stream:
            document = stream.read(_MAX_BYTES + 1)
    except OSError as exc:
        raise GraphSequenceError(f"cannot read graph sequence: {exc}") from exc
    return parse_graph_sequence(document)
