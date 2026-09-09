"""Lossless compact and pin-level circuit graph representations.

The SPICE ingest contract describes hierarchy and keeps terminal maps on each
owner.  This module derives two independent connectivity views from that
contract:

* a compact owner/net bipartite graph with terminal-labelled edges; and
* a pin/net bipartite graph in which every terminal is an explicit node.

Neither representation embeds the source :class:`CircuitGraph`.  Conversion
back to the source contract is therefore a useful executable round-trip oracle
rather than a wrapper around the original object.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import cast

from topology_lantern.circuit import (
    CircuitDevice,
    CircuitElementKind,
    CircuitGraph,
    CircuitGraphError,
    CircuitInstance,
    CircuitNet,
    CircuitPort,
    CircuitScope,
    SourceLocation,
    circuit_graph_identity,
    stable_node_id,
)

_SCHEMA = "org.topology-lantern.connectivity-graph"
_VERSION = 1
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SCOPES = 512
_MAX_SOURCES = 32
_MAX_PARAMETERS = 4_096
_MAX_OWNERS = 100_000
_MAX_NETS = 100_000
_MAX_EDGES = 500_000
_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_DEVICE_TERMINALS: dict[CircuitElementKind, tuple[str, ...]] = {
    CircuitElementKind.MOSFET: ("d", "g", "s", "b"),
    CircuitElementKind.RESISTOR: ("p", "n"),
    CircuitElementKind.CAPACITOR: ("p", "n"),
    CircuitElementKind.CURRENT_SOURCE: ("p", "n"),
    CircuitElementKind.VOLTAGE_SOURCE: ("p", "n"),
    CircuitElementKind.DIODE: ("a", "c"),
    CircuitElementKind.BJT: ("c", "b", "e"),
}
_MODELLED_KINDS = {
    CircuitElementKind.MOSFET,
    CircuitElementKind.DIODE,
    CircuitElementKind.BJT,
}


class ConnectivityRepresentationError(CircuitGraphError):
    """A compact or pin-level representation violates its lossless contract."""


class ConnectivityView(StrEnum):
    """The two version-1 connectivity representations."""

    COMPACT = "compact"
    PIN_LEVEL = "pin-level"


class OwnerKind(StrEnum):
    """Objects that own one or more electrical terminals."""

    PORT = "port"
    DEVICE = "device"
    INSTANCE = "instance"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return f"sha256:{sha256(_canonical(value).encode('ascii')).hexdigest()}"


def _stable_id(kind: str, *parts: str) -> str:
    payload = _canonical((kind, *parts)).encode("ascii")
    return f"tlg-{kind}-{sha256(payload).hexdigest()[:24]}"


def _safe_text(value: object, context: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConnectivityRepresentationError(
            f"{context} must be non-empty text of at most {maximum} characters"
        )
    try:
        exact = str.encode(value, "utf-8", errors="strict").decode("utf-8")
    except UnicodeError as exc:
        raise ConnectivityRepresentationError(f"{context} must be valid Unicode") from exc
    if unicodedata.normalize("NFC", exact) != exact or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in exact
    ):
        raise ConnectivityRepresentationError(
            f"{context} must be NFC text without control or separator characters"
        )
    return exact


def _sha(value: object, context: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ConnectivityRepresentationError(f"{context} must be a sha256: identity")
    return value


@dataclass(frozen=True, slots=True)
class ConnectivityOwner:
    """Metadata for one port, primitive device, or hierarchical instance."""

    owner_id: str
    name: str
    kind: OwnerKind
    index: int | None = None
    element_kind: CircuitElementKind | None = None
    model: str | None = None
    reference: str | None = None
    reference_scope_id: str | None = None
    parameters: tuple[tuple[str, str], ...] = ()
    effective_parameters: tuple[tuple[str, str], ...] = ()
    source: SourceLocation | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.owner_id,
            "name": self.name,
            "kind": self.kind.value,
            "index": self.index,
            "element_kind": self.element_kind.value if self.element_kind is not None else None,
            "model": self.model,
            "reference": self.reference,
            "reference_scope_id": self.reference_scope_id,
            "parameters": dict(self.parameters),
            "effective_parameters": dict(self.effective_parameters),
            "source": self.source.as_dict() if self.source is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CompactEdge:
    """One labelled incidence between an owner and a net."""

    edge_id: str
    owner_id: str
    terminal: str
    ordinal: int
    net_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.edge_id,
            "owner_id": self.owner_id,
            "terminal": self.terminal,
            "ordinal": self.ordinal,
            "net_id": self.net_id,
        }


@dataclass(frozen=True, slots=True)
class CompactScopeGraph:
    """One definition scope in the compact owner/net view."""

    scope_id: str
    name: str
    parameters: tuple[tuple[str, str], ...]
    nets: tuple[CircuitNet, ...]
    owners: tuple[ConnectivityOwner, ...]
    edges: tuple[CompactEdge, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.scope_id,
            "name": self.name,
            "parameters": dict(self.parameters),
            "nets": [net.as_dict() for net in self.nets],
            "owners": [owner.as_dict() for owner in self.owners],
            "edges": [edge.as_dict() for edge in self.edges],
        }


@dataclass(frozen=True, slots=True)
class CompactCircuitGraph:
    """Versioned, lossless terminal-labelled owner/net graph."""

    representation_id: str
    source_graph_id: str
    top: str
    sources: tuple[str, ...]
    scopes: tuple[CompactScopeGraph, ...]

    def body_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "view": ConnectivityView.COMPACT.value,
            "source_graph_id": self.source_graph_id,
            "top": self.top,
            "sources": list(self.sources),
            "scopes": [scope.as_dict() for scope in self.scopes],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body_dict(), "representation_id": self.representation_id}


@dataclass(frozen=True, slots=True)
class PinNode:
    """An explicit electrical pin owned by a port, device, or instance."""

    pin_id: str
    owner_id: str
    terminal: str
    ordinal: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.pin_id,
            "owner_id": self.owner_id,
            "terminal": self.terminal,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class PinLink:
    """One edge in the pin/net bipartite graph."""

    link_id: str
    pin_id: str
    net_id: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.link_id, "pin_id": self.pin_id, "net_id": self.net_id}


@dataclass(frozen=True, slots=True)
class PinScopeGraph:
    """One definition scope with explicit pin vertices."""

    scope_id: str
    name: str
    parameters: tuple[tuple[str, str], ...]
    nets: tuple[CircuitNet, ...]
    owners: tuple[ConnectivityOwner, ...]
    pins: tuple[PinNode, ...]
    links: tuple[PinLink, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.scope_id,
            "name": self.name,
            "parameters": dict(self.parameters),
            "nets": [net.as_dict() for net in self.nets],
            "owners": [owner.as_dict() for owner in self.owners],
            "pins": [pin.as_dict() for pin in self.pins],
            "links": [link.as_dict() for link in self.links],
        }


@dataclass(frozen=True, slots=True)
class PinCircuitGraph:
    """Versioned, lossless pin/net bipartite graph."""

    representation_id: str
    source_graph_id: str
    top: str
    sources: tuple[str, ...]
    scopes: tuple[PinScopeGraph, ...]

    def body_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "view": ConnectivityView.PIN_LEVEL.value,
            "source_graph_id": self.source_graph_id,
            "top": self.top,
            "sources": list(self.sources),
            "scopes": [scope.as_dict() for scope in self.scopes],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body_dict(), "representation_id": self.representation_id}


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ConnectivityRepresentationError(f"duplicate JSON member {name!r}")
        result[name] = value
    return result


def _reject_non_finite(token: str) -> object:
    raise ConnectivityRepresentationError(f"non-finite JSON number {token!r} is forbidden")


def _object(value: object, context: str, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ConnectivityRepresentationError(f"{context} must be an object")
    raw = cast(dict[object, object], value)
    if any(type(name) is not str for name in raw):
        raise ConnectivityRepresentationError(f"{context} has a non-text member name")
    result = cast(dict[str, object], raw)
    actual = frozenset(result)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise ConnectivityRepresentationError(
            f"{context} has an invalid shape (missing={missing}, extra={extra})"
        )
    return result


def _array(value: object, context: str, maximum: int) -> list[object]:
    if type(value) is not list:
        raise ConnectivityRepresentationError(f"{context} must be an array")
    result = cast(list[object], value)
    if len(result) > maximum:
        raise ConnectivityRepresentationError(f"{context} exceeds its item limit")
    return result


def _text(value: object, context: str) -> str:
    if type(value) is not str:
        raise ConnectivityRepresentationError(f"{context} must be text")
    return value


def _optional_text(value: object, context: str) -> str | None:
    return None if value is None else _text(value, context)


def _integer_or_none(value: object, context: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ConnectivityRepresentationError(f"{context} must be an integer or null")
    return value


def _string_pairs(value: object, context: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not dict:
        raise ConnectivityRepresentationError(f"{context} must be an object")
    raw = cast(dict[object, object], value)
    if len(raw) > _MAX_PARAMETERS:
        raise ConnectivityRepresentationError(f"{context} exceeds its member limit")
    result: list[tuple[str, str]] = []
    for name, item in raw.items():
        result.append((_text(name, f"{context} name"), _text(item, f"{context} value")))
    return tuple(result)


def _source(value: object, context: str) -> SourceLocation | None:
    if value is None:
        return None
    raw = _object(value, context, frozenset({"file", "line"}))
    line = raw["line"]
    if type(line) is not int:
        raise ConnectivityRepresentationError(f"{context} line must be an integer")
    return SourceLocation(_text(raw["file"], f"{context} file"), line)


def _net(value: object, context: str) -> CircuitNet:
    raw = _object(value, context, frozenset({"id", "name", "is_port", "is_global"}))
    if type(raw["is_port"]) is not bool or type(raw["is_global"]) is not bool:
        raise ConnectivityRepresentationError(f"{context} flags must be booleans")
    return CircuitNet(
        _text(raw["id"], f"{context} id"),
        _text(raw["name"], f"{context} name"),
        raw["is_port"],
        raw["is_global"],
    )


_OWNER_FIELDS = frozenset(
    {
        "id",
        "name",
        "kind",
        "index",
        "element_kind",
        "model",
        "reference",
        "reference_scope_id",
        "parameters",
        "effective_parameters",
        "source",
    }
)


def _owner(value: object, context: str) -> ConnectivityOwner:
    raw = _object(value, context, _OWNER_FIELDS)
    try:
        kind = OwnerKind(_text(raw["kind"], f"{context} kind"))
        element_kind = (
            None
            if raw["element_kind"] is None
            else CircuitElementKind(_text(raw["element_kind"], f"{context} element kind"))
        )
    except ValueError as error:
        raise ConnectivityRepresentationError(f"{context} has an unknown enum value") from error
    return ConnectivityOwner(
        _text(raw["id"], f"{context} id"),
        _text(raw["name"], f"{context} name"),
        kind,
        _integer_or_none(raw["index"], f"{context} index"),
        element_kind,
        _optional_text(raw["model"], f"{context} model"),
        _optional_text(raw["reference"], f"{context} reference"),
        _optional_text(raw["reference_scope_id"], f"{context} reference scope id"),
        _string_pairs(raw["parameters"], f"{context} parameters"),
        _string_pairs(raw["effective_parameters"], f"{context} effective parameters"),
        _source(raw["source"], f"{context} source"),
    )


def _compact_scope(value: object, context: str) -> CompactScopeGraph:
    fields = frozenset({"id", "name", "parameters", "nets", "owners", "edges"})
    raw = _object(value, context, fields)
    nets = _array(raw["nets"], f"{context} nets", _MAX_NETS)
    owners = _array(raw["owners"], f"{context} owners", _MAX_OWNERS)
    edges = _array(raw["edges"], f"{context} edges", _MAX_EDGES)
    parsed_edges: list[CompactEdge] = []
    for index, value_edge in enumerate(edges):
        edge_context = f"{context} edge[{index}]"
        edge = _object(
            value_edge,
            edge_context,
            frozenset({"id", "owner_id", "terminal", "ordinal", "net_id"}),
        )
        ordinal = edge["ordinal"]
        if type(ordinal) is not int:
            raise ConnectivityRepresentationError(f"{edge_context} ordinal must be an integer")
        parsed_edges.append(
            CompactEdge(
                _text(edge["id"], f"{edge_context} id"),
                _text(edge["owner_id"], f"{edge_context} owner id"),
                _text(edge["terminal"], f"{edge_context} terminal"),
                ordinal,
                _text(edge["net_id"], f"{edge_context} net id"),
            )
        )
    return CompactScopeGraph(
        _text(raw["id"], f"{context} id"),
        _text(raw["name"], f"{context} name"),
        _string_pairs(raw["parameters"], f"{context} parameters"),
        tuple(_net(item, f"{context} net[{index}]") for index, item in enumerate(nets)),
        tuple(_owner(item, f"{context} owner[{index}]") for index, item in enumerate(owners)),
        tuple(parsed_edges),
    )


def _pin_scope(value: object, context: str) -> PinScopeGraph:
    fields = frozenset({"id", "name", "parameters", "nets", "owners", "pins", "links"})
    raw = _object(value, context, fields)
    nets = _array(raw["nets"], f"{context} nets", _MAX_NETS)
    owners = _array(raw["owners"], f"{context} owners", _MAX_OWNERS)
    pins = _array(raw["pins"], f"{context} pins", _MAX_EDGES)
    links = _array(raw["links"], f"{context} links", _MAX_EDGES)
    parsed_pins: list[PinNode] = []
    for index, value_pin in enumerate(pins):
        pin_context = f"{context} pin[{index}]"
        pin = _object(
            value_pin,
            pin_context,
            frozenset({"id", "owner_id", "terminal", "ordinal"}),
        )
        ordinal = pin["ordinal"]
        if type(ordinal) is not int:
            raise ConnectivityRepresentationError(f"{pin_context} ordinal must be an integer")
        parsed_pins.append(
            PinNode(
                _text(pin["id"], f"{pin_context} id"),
                _text(pin["owner_id"], f"{pin_context} owner id"),
                _text(pin["terminal"], f"{pin_context} terminal"),
                ordinal,
            )
        )
    parsed_links: list[PinLink] = []
    for index, value_link in enumerate(links):
        link_context = f"{context} link[{index}]"
        link = _object(value_link, link_context, frozenset({"id", "pin_id", "net_id"}))
        parsed_links.append(
            PinLink(
                _text(link["id"], f"{link_context} id"),
                _text(link["pin_id"], f"{link_context} pin id"),
                _text(link["net_id"], f"{link_context} net id"),
            )
        )
    return PinScopeGraph(
        _text(raw["id"], f"{context} id"),
        _text(raw["name"], f"{context} name"),
        _string_pairs(raw["parameters"], f"{context} parameters"),
        tuple(_net(item, f"{context} net[{index}]") for index, item in enumerate(nets)),
        tuple(_owner(item, f"{context} owner[{index}]") for index, item in enumerate(owners)),
        tuple(parsed_pins),
        tuple(parsed_links),
    )


def _owner_port(port: CircuitPort) -> ConnectivityOwner:
    return ConnectivityOwner(port.node_id, port.name, OwnerKind.PORT, index=port.index)


def _owner_device(device: CircuitDevice) -> ConnectivityOwner:
    return ConnectivityOwner(
        device.node_id,
        device.name,
        OwnerKind.DEVICE,
        element_kind=device.kind,
        model=device.model,
        parameters=device.parameters,
        source=device.source,
    )


def _owner_instance(instance: CircuitInstance) -> ConnectivityOwner:
    return ConnectivityOwner(
        instance.node_id,
        instance.name,
        OwnerKind.INSTANCE,
        reference=instance.reference,
        reference_scope_id=instance.reference_scope_id,
        parameters=instance.parameters,
        effective_parameters=instance.effective_parameters,
        source=instance.source,
    )


def _edge(scope_id: str, owner_id: str, terminal: str, ordinal: int, net_id: str) -> CompactEdge:
    return CompactEdge(
        _stable_id("edge", scope_id, owner_id, terminal, str(ordinal), net_id),
        owner_id,
        terminal,
        ordinal,
        net_id,
    )


def _scope_to_compact(scope: CircuitScope) -> CompactScopeGraph:
    owners = (
        *(_owner_port(port) for port in scope.ports),
        *(_owner_device(device) for device in scope.devices),
        *(_owner_instance(instance) for instance in scope.instances),
    )
    edges: list[CompactEdge] = []
    for port in scope.ports:
        edges.append(_edge(scope.scope_id, port.node_id, "port", 0, port.net_id))
    for device in scope.devices:
        edges.extend(
            _edge(scope.scope_id, device.node_id, terminal, ordinal, net_id)
            for ordinal, (terminal, net_id) in enumerate(device.terminals)
        )
    for instance in scope.instances:
        edges.extend(
            _edge(scope.scope_id, instance.node_id, terminal, ordinal, net_id)
            for ordinal, (terminal, net_id) in enumerate(instance.connections)
        )
    return CompactScopeGraph(
        scope.scope_id,
        scope.name,
        scope.parameters,
        scope.nets,
        tuple(owners),
        tuple(edges),
    )


def compact_graph(graph: CircuitGraph) -> CompactCircuitGraph:
    """Derive and validate the compact owner/net view of ``graph``."""

    if not isinstance(graph, CircuitGraph):
        raise ConnectivityRepresentationError("graph must be a CircuitGraph")
    compact = CompactCircuitGraph(
        "sha256:" + "0" * 64,
        graph.graph_id,
        graph.top,
        graph.sources,
        tuple(_scope_to_compact(scope) for scope in graph.scopes),
    )
    compact = CompactCircuitGraph(
        _digest(compact.body_dict()),
        compact.source_graph_id,
        compact.top,
        compact.sources,
        compact.scopes,
    )
    return validate_compact_graph(compact)


def _validate_owner(owner: ConnectivityOwner, context: str) -> None:
    if not isinstance(owner, ConnectivityOwner):
        raise ConnectivityRepresentationError(f"{context} is not an owner")
    _safe_text(owner.owner_id, f"{context} id")
    _safe_text(owner.name, f"{context} name")
    if not isinstance(owner.kind, OwnerKind):
        raise ConnectivityRepresentationError(f"{context} kind is invalid")
    parameters = tuple(owner.parameters)
    effective = tuple(owner.effective_parameters)
    for field, values in (("parameters", parameters), ("effective parameters", effective)):
        if len(values) > _MAX_PARAMETERS:
            raise ConnectivityRepresentationError(f"{context} has too many {field}")
        if len({name for name, _value in values}) != len(values):
            raise ConnectivityRepresentationError(f"{context} has duplicate {field}")
        for name, value in values:
            _safe_text(name, f"{context} {field} name")
            _safe_text(value, f"{context} {field} value")
    if owner.model is not None:
        _safe_text(owner.model, f"{context} model")
    if owner.reference is not None:
        _safe_text(owner.reference, f"{context} reference")
    if owner.reference_scope_id is not None:
        _safe_text(owner.reference_scope_id, f"{context} reference scope id")
    if owner.source is not None:
        _safe_text(owner.source.file, f"{context} source file")
        if (
            isinstance(owner.source.line, bool)
            or not isinstance(owner.source.line, int)
            or owner.source.line < 1
        ):
            raise ConnectivityRepresentationError(f"{context} source line is invalid")
    if owner.kind is OwnerKind.PORT:
        if (
            isinstance(owner.index, bool)
            or not isinstance(owner.index, int)
            or owner.index < 0
            or any(
                value is not None
                for value in (
                    owner.element_kind,
                    owner.model,
                    owner.reference,
                    owner.reference_scope_id,
                    owner.source,
                )
            )
            or parameters
            or effective
        ):
            raise ConnectivityRepresentationError(f"{context} has invalid port metadata")
    elif owner.kind is OwnerKind.DEVICE:
        if (
            owner.index is not None
            or not isinstance(owner.element_kind, CircuitElementKind)
            or owner.reference is not None
            or owner.reference_scope_id is not None
            or owner.effective_parameters
            or not isinstance(owner.source, SourceLocation)
        ):
            raise ConnectivityRepresentationError(f"{context} has invalid device metadata")
    elif (
        owner.index is not None
        or owner.element_kind is not None
        or owner.model is not None
        or not owner.reference
        or not owner.reference_scope_id
        or not isinstance(owner.source, SourceLocation)
    ):
        raise ConnectivityRepresentationError(f"{context} has invalid instance metadata")


def _validate_common(
    source_graph_id: object,
    top: object,
    sources: tuple[str, ...],
    scopes: tuple[CompactScopeGraph | PinScopeGraph, ...],
) -> None:
    _sha(source_graph_id, "source graph id")
    _safe_text(top, "top")
    if not 1 <= len(scopes) <= _MAX_SCOPES:
        raise ConnectivityRepresentationError("connectivity graph has an invalid scope count")
    if not 1 <= len(sources) <= _MAX_SOURCES:
        raise ConnectivityRepresentationError("connectivity graph has an invalid source count")
    if len(set(sources)) != len(sources):
        raise ConnectivityRepresentationError("connectivity graph has duplicate sources")
    for source in sources:
        _safe_text(source, "source path", maximum=4_096)
    scope_ids: set[str] = set()
    scope_names: set[str] = set()
    owner_ids: set[str] = set()
    net_identities: dict[str, tuple[str, bool]] = {}
    total_owners = total_nets = 0
    if tuple(scope.name for scope in scopes) != tuple(sorted(scope.name for scope in scopes)):
        raise ConnectivityRepresentationError("scope order is not canonical")
    for index, scope in enumerate(scopes):
        context = f"scope[{index}]"
        _safe_text(scope.scope_id, f"{context} id")
        _safe_text(scope.name, f"{context} name")
        if scope.scope_id != stable_node_id("scope", scope.name, scope.name):
            raise ConnectivityRepresentationError(f"{context} identity is not canonical")
        if scope.scope_id in scope_ids or scope.name in scope_names:
            raise ConnectivityRepresentationError("scope identities and names must be unique")
        scope_ids.add(scope.scope_id)
        scope_names.add(scope.name)
        if len(scope.parameters) > _MAX_PARAMETERS:
            raise ConnectivityRepresentationError(f"{context} has too many parameters")
        if len({name for name, _value in scope.parameters}) != len(scope.parameters):
            raise ConnectivityRepresentationError(f"{context} has duplicate parameters")
        if scope.parameters != tuple(sorted(scope.parameters)):
            raise ConnectivityRepresentationError(f"{context} parameter order is not canonical")
        for name, value in scope.parameters:
            _safe_text(name, f"{context} parameter name")
            _safe_text(value, f"{context} parameter value")
        if tuple(net.name for net in scope.nets) != tuple(sorted(net.name for net in scope.nets)):
            raise ConnectivityRepresentationError(f"{context} net order is not canonical")
        local_nets: set[str] = set()
        for net in scope.nets:
            if not isinstance(net, CircuitNet):
                raise ConnectivityRepresentationError(f"{context} has an invalid net")
            _safe_text(net.node_id, f"{context} net id")
            _safe_text(net.name, f"{context} net name")
            if type(net.is_port) is not bool or type(net.is_global) is not bool:
                raise ConnectivityRepresentationError(f"{context} net flags must be booleans")
            expected_net_id = stable_node_id(
                "net", "__global__" if net.is_global else scope.name, net.name
            )
            if net.node_id != expected_net_id:
                raise ConnectivityRepresentationError(f"{context} net identity is not canonical")
            if net.node_id in local_nets:
                raise ConnectivityRepresentationError("net identities must be locally unique")
            prior = net_identities.get(net.node_id)
            identity = (net.name, net.is_global)
            if prior is not None and (not net.is_global or prior != identity):
                raise ConnectivityRepresentationError(
                    "only identically named global nets may share an identity across scopes"
                )
            local_nets.add(net.node_id)
            net_identities[net.node_id] = identity
        for owner_index, owner in enumerate(scope.owners):
            _validate_owner(owner, f"{context} owner[{owner_index}]")
        expected_owner_order = (
            *(
                owner.owner_id
                for owner in sorted(
                    (owner for owner in scope.owners if owner.kind is OwnerKind.PORT),
                    key=lambda owner: owner.index if owner.index is not None else -1,
                )
            ),
            *(
                owner.owner_id
                for owner in sorted(
                    (owner for owner in scope.owners if owner.kind is OwnerKind.DEVICE),
                    key=lambda owner: owner.name,
                )
            ),
            *(
                owner.owner_id
                for owner in sorted(
                    (owner for owner in scope.owners if owner.kind is OwnerKind.INSTANCE),
                    key=lambda owner: owner.name,
                )
            ),
        )
        if tuple(owner.owner_id for owner in scope.owners) != expected_owner_order:
            raise ConnectivityRepresentationError(f"{context} owner order is not canonical")
        local_owners: set[str] = set()
        for owner_index, owner in enumerate(scope.owners):
            expected_owner_id = stable_node_id(owner.kind.value, scope.name, owner.name)
            if owner.owner_id != expected_owner_id:
                raise ConnectivityRepresentationError(
                    f"{context} owner[{owner_index}] identity is not canonical"
                )
            if owner.owner_id in owner_ids or owner.owner_id in local_owners:
                raise ConnectivityRepresentationError("owner identities must be globally unique")
            local_owners.add(owner.owner_id)
            owner_ids.add(owner.owner_id)
        port_indexes = sorted(
            owner.index
            for owner in scope.owners
            if owner.kind is OwnerKind.PORT and owner.index is not None
        )
        if port_indexes != list(range(len(port_indexes))):
            raise ConnectivityRepresentationError(f"{context} port indexes must be contiguous")
        total_owners += len(scope.owners)
        total_nets += len(scope.nets)
    if total_owners > _MAX_OWNERS or total_nets > _MAX_NETS:
        raise ConnectivityRepresentationError("connectivity graph exceeds owner or net limits")
    if top not in scope_names:
        raise ConnectivityRepresentationError("top does not name a represented scope")
    scopes_by_name = {scope.name: scope for scope in scopes}
    scopes_by_id = {scope.scope_id: scope for scope in scopes}
    for scope in scopes:
        for owner in scope.owners:
            if owner.kind is not OwnerKind.INSTANCE:
                continue
            referenced = scopes_by_name.get(owner.reference or "")
            if (
                referenced is None
                or scopes_by_id.get(owner.reference_scope_id or "") is not referenced
            ):
                raise ConnectivityRepresentationError(
                    f"instance {owner.name!r} references an unknown or inconsistent scope"
                )
            defaults = dict(referenced.parameters)
            overrides = dict(owner.parameters)
            if set(overrides) - set(defaults) or dict(owner.effective_parameters) != (
                defaults | overrides
            ):
                raise ConnectivityRepresentationError(
                    f"instance {owner.name!r} has inconsistent effective parameters"
                )


def _validate_incidence_contract(
    owner: ConnectivityOwner,
    terminals: tuple[str, ...],
    scopes_by_name: Mapping[str, CompactScopeGraph | PinScopeGraph],
) -> None:
    expected: tuple[str, ...]
    if owner.kind is OwnerKind.PORT:
        expected = ("port",)
    elif owner.kind is OwnerKind.DEVICE:
        if owner.element_kind is None:  # defensive after metadata validation
            raise ConnectivityRepresentationError("device owner has no element kind")
        expected = _DEVICE_TERMINALS[owner.element_kind]
        needs_model = owner.element_kind in _MODELLED_KINDS
        if needs_model != (owner.model is not None):
            raise ConnectivityRepresentationError(
                f"device {owner.name!r} has inconsistent model metadata"
            )
    else:
        referenced = scopes_by_name.get(owner.reference or "")
        if referenced is None:  # defensive after common validation
            raise ConnectivityRepresentationError("instance owner has no referenced scope")
        expected = tuple(
            item.name
            for item in sorted(
                (item for item in referenced.owners if item.kind is OwnerKind.PORT),
                key=lambda item: item.index if item.index is not None else -1,
            )
        )
    if terminals != expected:
        raise ConnectivityRepresentationError(
            f"owner {owner.name!r} terminals do not match its electrical contract"
        )


def _validate_port_net_contract(
    scope: CompactScopeGraph | PinScopeGraph,
    owner_net_ids: dict[str, tuple[str, ...]],
) -> None:
    port_net_ids = {
        owner_net_ids[owner.owner_id][0] for owner in scope.owners if owner.kind is OwnerKind.PORT
    }
    declared_port_net_ids = {net.node_id for net in scope.nets if net.is_port}
    if port_net_ids != declared_port_net_ids:
        raise ConnectivityRepresentationError(
            f"scope {scope.name!r} port owners and net flags are inconsistent"
        )


def validate_compact_graph(graph: CompactCircuitGraph) -> CompactCircuitGraph:
    """Validate identities, cardinalities, metadata unions, and edge coverage."""

    if not isinstance(graph, CompactCircuitGraph):
        raise ConnectivityRepresentationError("compact graph has the wrong object type")
    _validate_common(graph.source_graph_id, graph.top, graph.sources, graph.scopes)
    total_edges = 0
    edge_ids: set[str] = set()
    scopes_by_name = {scope.name: scope for scope in graph.scopes}
    for scope_index, scope in enumerate(graph.scopes):
        owners = {owner.owner_id: owner for owner in scope.owners}
        nets = {net.node_id: net for net in scope.nets}
        terminal_keys: set[tuple[str, int]] = set()
        owner_edges: dict[str, list[CompactEdge]] = {owner_id: [] for owner_id in owners}
        for edge_index, edge in enumerate(scope.edges):
            context = f"scope[{scope_index}] edge[{edge_index}]"
            if not isinstance(edge, CompactEdge):
                raise ConnectivityRepresentationError(f"{context} is invalid")
            _safe_text(edge.edge_id, f"{context} id")
            _safe_text(edge.terminal, f"{context} terminal")
            if edge.owner_id not in owners or edge.net_id not in nets:
                raise ConnectivityRepresentationError(f"{context} references an unknown vertex")
            if (
                isinstance(edge.ordinal, bool)
                or not isinstance(edge.ordinal, int)
                or edge.ordinal < 0
            ):
                raise ConnectivityRepresentationError(f"{context} ordinal is invalid")
            key = (edge.owner_id, edge.ordinal)
            if edge.edge_id in edge_ids or key in terminal_keys:
                raise ConnectivityRepresentationError(
                    "edge identities and owner ordinals must be unique"
                )
            expected_id = _edge(
                scope.scope_id, edge.owner_id, edge.terminal, edge.ordinal, edge.net_id
            ).edge_id
            if edge.edge_id != expected_id:
                raise ConnectivityRepresentationError(f"{context} identity does not match its body")
            edge_ids.add(edge.edge_id)
            terminal_keys.add(key)
            owner_edges[edge.owner_id].append(edge)
        for owner_id, edges in owner_edges.items():
            edges.sort(key=lambda item: item.ordinal)
            if not edges or [edge.ordinal for edge in edges] != list(range(len(edges))):
                raise ConnectivityRepresentationError(
                    f"owner {owner_id!r} terminal ordinals must be contiguous"
                )
            if owners[owner_id].kind is OwnerKind.PORT and (
                len(edges) != 1 or edges[0].terminal != "port"
            ):
                raise ConnectivityRepresentationError("port owners require one 'port' edge")
            if len({edge.terminal for edge in edges}) != len(edges):
                raise ConnectivityRepresentationError(
                    f"owner {owner_id!r} terminal names must be unique"
                )
            _validate_incidence_contract(
                owners[owner_id], tuple(edge.terminal for edge in edges), scopes_by_name
            )
        expected_edge_order = tuple(
            (owner.owner_id, edge.ordinal)
            for owner in scope.owners
            for edge in sorted(owner_edges[owner.owner_id], key=lambda item: item.ordinal)
        )
        if tuple((edge.owner_id, edge.ordinal) for edge in scope.edges) != expected_edge_order:
            raise ConnectivityRepresentationError(
                f"scope[{scope_index}] edge order is not canonical"
            )
        _validate_port_net_contract(
            scope,
            {
                owner_id: tuple(
                    edge.net_id for edge in sorted(edges, key=lambda item: item.ordinal)
                )
                for owner_id, edges in owner_edges.items()
            },
        )
        total_edges += len(scope.edges)
    if total_edges > _MAX_EDGES:
        raise ConnectivityRepresentationError("compact graph exceeds the edge limit")
    if graph.representation_id != _digest(graph.body_dict()):
        raise ConnectivityRepresentationError(
            "compact representation identity does not match its body"
        )
    reconstructed = _compact_to_circuit_unchecked(graph)
    if circuit_graph_identity(reconstructed.top, reconstructed.scopes) != graph.source_graph_id:
        raise ConnectivityRepresentationError(
            "compact source graph identity does not match its semantic body"
        )
    return graph


def _compact_to_circuit_unchecked(graph: CompactCircuitGraph) -> CircuitGraph:
    scopes: list[CircuitScope] = []
    for scope in graph.scopes:
        edges: dict[str, list[CompactEdge]] = {owner.owner_id: [] for owner in scope.owners}
        for edge in scope.edges:
            edges[edge.owner_id].append(edge)
        for owner_edges in edges.values():
            owner_edges.sort(key=lambda item: item.ordinal)
        ports: list[CircuitPort] = []
        devices: list[CircuitDevice] = []
        instances: list[CircuitInstance] = []
        for owner in scope.owners:
            connections = tuple((edge.terminal, edge.net_id) for edge in edges[owner.owner_id])
            if owner.kind is OwnerKind.PORT:
                if owner.index is None:  # defensive after public validation
                    raise ConnectivityRepresentationError("port owner has no index")
                ports.append(
                    CircuitPort(owner.owner_id, owner.name, owner.index, connections[0][1])
                )
            elif owner.kind is OwnerKind.DEVICE:
                if owner.element_kind is None or owner.source is None:  # defensive
                    raise ConnectivityRepresentationError("device owner metadata is incomplete")
                devices.append(
                    CircuitDevice(
                        owner.owner_id,
                        owner.name,
                        owner.element_kind,
                        connections,
                        owner.model,
                        owner.parameters,
                        owner.source,
                    )
                )
            else:
                if (
                    owner.reference is None
                    or owner.reference_scope_id is None
                    or owner.source is None
                ):  # defensive
                    raise ConnectivityRepresentationError("instance owner metadata is incomplete")
                instances.append(
                    CircuitInstance(
                        owner.owner_id,
                        owner.name,
                        owner.reference,
                        owner.reference_scope_id,
                        connections,
                        owner.parameters,
                        owner.effective_parameters,
                        owner.source,
                    )
                )
        ports.sort(key=lambda item: item.index)
        scopes.append(
            CircuitScope(
                scope.scope_id,
                scope.name,
                tuple(ports),
                scope.parameters,
                scope.nets,
                tuple(devices),
                tuple(instances),
            )
        )
    return CircuitGraph(graph.source_graph_id, graph.top, tuple(scopes), graph.sources)


def compact_to_circuit(graph: CompactCircuitGraph) -> CircuitGraph:
    """Reconstruct the exact source circuit contract from a compact view."""

    return _compact_to_circuit_unchecked(validate_compact_graph(graph))


def compact_to_pin(graph: CompactCircuitGraph) -> PinCircuitGraph:
    """Expand every compact terminal-labelled edge into a pin vertex and link."""

    graph = validate_compact_graph(graph)
    scopes: list[PinScopeGraph] = []
    for scope in graph.scopes:
        pins: list[PinNode] = []
        links: list[PinLink] = []
        for edge in scope.edges:
            pin_id = _stable_id("pin", edge.edge_id)
            pins.append(PinNode(pin_id, edge.owner_id, edge.terminal, edge.ordinal))
            links.append(PinLink(_stable_id("link", pin_id, edge.net_id), pin_id, edge.net_id))
        scopes.append(
            PinScopeGraph(
                scope.scope_id,
                scope.name,
                scope.parameters,
                scope.nets,
                scope.owners,
                tuple(pins),
                tuple(links),
            )
        )
    pin = PinCircuitGraph(
        "sha256:" + "0" * 64,
        graph.source_graph_id,
        graph.top,
        graph.sources,
        tuple(scopes),
    )
    pin = PinCircuitGraph(
        _digest(pin.body_dict()), pin.source_graph_id, pin.top, pin.sources, pin.scopes
    )
    return validate_pin_graph(pin)


def pin_graph(graph: CircuitGraph) -> PinCircuitGraph:
    """Derive a pin-level view directly from a source circuit graph."""

    return compact_to_pin(compact_graph(graph))


def validate_pin_graph(graph: PinCircuitGraph) -> PinCircuitGraph:
    """Validate pin ownership, exact link coverage, limits, and content identity."""

    if not isinstance(graph, PinCircuitGraph):
        raise ConnectivityRepresentationError("pin graph has the wrong object type")
    _validate_common(graph.source_graph_id, graph.top, graph.sources, graph.scopes)
    pin_ids: set[str] = set()
    link_ids: set[str] = set()
    total_links = 0
    scopes_by_name = {scope.name: scope for scope in graph.scopes}
    for scope_index, scope in enumerate(graph.scopes):
        owners = {owner.owner_id for owner in scope.owners}
        nets = {net.node_id for net in scope.nets}
        local_pins: dict[str, PinNode] = {}
        owner_pins: dict[str, list[PinNode]] = {owner_id: [] for owner_id in owners}
        owner_ordinals: set[tuple[str, int]] = set()
        for pin_index, pin in enumerate(scope.pins):
            context = f"scope[{scope_index}] pin[{pin_index}]"
            if not isinstance(pin, PinNode):
                raise ConnectivityRepresentationError(f"{context} is invalid")
            _safe_text(pin.pin_id, f"{context} id")
            _safe_text(pin.terminal, f"{context} terminal")
            if pin.owner_id not in owners:
                raise ConnectivityRepresentationError(f"{context} references an unknown owner")
            if isinstance(pin.ordinal, bool) or not isinstance(pin.ordinal, int) or pin.ordinal < 0:
                raise ConnectivityRepresentationError(f"{context} ordinal is invalid")
            key = (pin.owner_id, pin.ordinal)
            if pin.pin_id in pin_ids or key in owner_ordinals:
                raise ConnectivityRepresentationError(
                    "pin identities and owner ordinals must be unique"
                )
            pin_ids.add(pin.pin_id)
            local_pins[pin.pin_id] = pin
            owner_pins[pin.owner_id].append(pin)
            owner_ordinals.add(key)
        links_by_pin: dict[str, PinLink] = {}
        for link_index, link in enumerate(scope.links):
            context = f"scope[{scope_index}] link[{link_index}]"
            if not isinstance(link, PinLink):
                raise ConnectivityRepresentationError(f"{context} is invalid")
            if link.pin_id not in local_pins or link.net_id not in nets:
                raise ConnectivityRepresentationError(f"{context} references an unknown vertex")
            if link.pin_id in links_by_pin or link.link_id in link_ids:
                raise ConnectivityRepresentationError(
                    "each pin requires one uniquely identified link"
                )
            if link.link_id != _stable_id("link", link.pin_id, link.net_id):
                raise ConnectivityRepresentationError(f"{context} identity does not match its body")
            links_by_pin[link.pin_id] = link
            link_ids.add(link.link_id)
        if set(links_by_pin) != set(local_pins):
            raise ConnectivityRepresentationError(
                "every represented pin must have exactly one link"
            )
        expected_pin_order = tuple(
            (owner.owner_id, ordinal)
            for owner in scope.owners
            for ordinal in range(len(owner_pins[owner.owner_id]))
        )
        if tuple((pin.owner_id, pin.ordinal) for pin in scope.pins) != expected_pin_order:
            raise ConnectivityRepresentationError(
                f"scope[{scope_index}] pin order is not canonical"
            )
        if tuple(link.pin_id for link in scope.links) != tuple(pin.pin_id for pin in scope.pins):
            raise ConnectivityRepresentationError(
                f"scope[{scope_index}] link order is not canonical"
            )
        for owner in scope.owners:
            pins = sorted(
                owner_pins[owner.owner_id],
                key=lambda item: item.ordinal,
            )
            if not pins or [pin.ordinal for pin in pins] != list(range(len(pins))):
                raise ConnectivityRepresentationError(
                    f"owner {owner.owner_id!r} pin ordinals must be contiguous"
                )
            if owner.kind is OwnerKind.PORT and (len(pins) != 1 or pins[0].terminal != "port"):
                raise ConnectivityRepresentationError("port owners require one 'port' pin")
            if len({pin.terminal for pin in pins}) != len(pins):
                raise ConnectivityRepresentationError(
                    f"owner {owner.owner_id!r} pin names must be unique"
                )
            for pin in pins:
                edge_id = _edge(
                    scope.scope_id,
                    pin.owner_id,
                    pin.terminal,
                    pin.ordinal,
                    links_by_pin[pin.pin_id].net_id,
                ).edge_id
                if pin.pin_id != _stable_id("pin", edge_id):
                    raise ConnectivityRepresentationError(
                        f"pin {pin.pin_id!r} identity does not match its incidence"
                    )
            _validate_incidence_contract(owner, tuple(pin.terminal for pin in pins), scopes_by_name)
        _validate_port_net_contract(
            scope,
            {
                owner.owner_id: tuple(
                    links_by_pin[pin.pin_id].net_id
                    for pin in sorted(owner_pins[owner.owner_id], key=lambda item: item.ordinal)
                )
                for owner in scope.owners
            },
        )
        total_links += len(scope.links)
    if total_links > _MAX_EDGES:
        raise ConnectivityRepresentationError("pin graph exceeds the link limit")
    if graph.representation_id != _digest(graph.body_dict()):
        raise ConnectivityRepresentationError("pin representation identity does not match its body")
    reconstructed = _compact_to_circuit_unchecked(_pin_to_compact_unchecked(graph))
    if circuit_graph_identity(reconstructed.top, reconstructed.scopes) != graph.source_graph_id:
        raise ConnectivityRepresentationError(
            "pin source graph identity does not match its semantic body"
        )
    return graph


def _pin_to_compact_unchecked(graph: PinCircuitGraph) -> CompactCircuitGraph:
    scopes: list[CompactScopeGraph] = []
    for scope in graph.scopes:
        link_by_pin = {link.pin_id: link for link in scope.links}
        edges = tuple(
            _edge(
                scope.scope_id,
                pin.owner_id,
                pin.terminal,
                pin.ordinal,
                link_by_pin[pin.pin_id].net_id,
            )
            for pin in scope.pins
        )
        scopes.append(
            CompactScopeGraph(
                scope.scope_id,
                scope.name,
                scope.parameters,
                scope.nets,
                scope.owners,
                edges,
            )
        )
    compact = CompactCircuitGraph(
        "sha256:" + "0" * 64,
        graph.source_graph_id,
        graph.top,
        graph.sources,
        tuple(scopes),
    )
    compact = CompactCircuitGraph(
        _digest(compact.body_dict()),
        compact.source_graph_id,
        compact.top,
        compact.sources,
        compact.scopes,
    )
    return compact


def pin_to_compact(graph: PinCircuitGraph) -> CompactCircuitGraph:
    """Collapse a pin/net graph into its terminal-labelled compact view."""

    return validate_compact_graph(_pin_to_compact_unchecked(validate_pin_graph(graph)))


def connectivity_graph_json(
    graph: CompactCircuitGraph | PinCircuitGraph, *, pretty: bool = False
) -> str:
    """Serialize either validated view as canonical or readable JSON."""

    if isinstance(graph, CompactCircuitGraph):
        value = validate_compact_graph(graph).as_dict()
    elif isinstance(graph, PinCircuitGraph):
        value = validate_pin_graph(graph).as_dict()
    else:
        raise ConnectivityRepresentationError("connectivity graph object type is invalid")
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def parse_connectivity_graph(document: str | bytes) -> CompactCircuitGraph | PinCircuitGraph:
    """Parse an untrusted version-1 connectivity document and validate every identity.

    Duplicate members, non-finite numbers, unknown fields, oversized documents,
    and mixed-view shapes are rejected before a graph is returned.
    """

    if type(document) is bytes:
        encoded = document
        if len(encoded) > _MAX_DOCUMENT_BYTES:
            raise ConnectivityRepresentationError("connectivity document exceeds its byte limit")
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ConnectivityRepresentationError(
                "connectivity document must be valid UTF-8"
            ) from error
    elif type(document) is str:
        text = document
        try:
            if len(text.encode("utf-8", errors="strict")) > _MAX_DOCUMENT_BYTES:
                raise ConnectivityRepresentationError(
                    "connectivity document exceeds its byte limit"
                )
        except UnicodeError as error:
            raise ConnectivityRepresentationError(
                "connectivity document must be valid Unicode"
            ) from error
    else:
        raise ConnectivityRepresentationError("connectivity document must be text or bytes")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_non_finite,
        )
    except ConnectivityRepresentationError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError) as error:
        raise ConnectivityRepresentationError(f"invalid connectivity JSON: {error}") from error
    root = _object(
        value,
        "connectivity document",
        frozenset(
            {
                "schema",
                "version",
                "view",
                "representation_id",
                "source_graph_id",
                "top",
                "sources",
                "scopes",
            }
        ),
    )
    if root["schema"] != _SCHEMA:
        raise ConnectivityRepresentationError("connectivity document schema is unsupported")
    if type(root["version"]) is not int or root["version"] != _VERSION:
        raise ConnectivityRepresentationError("connectivity document version is unsupported")
    try:
        view = ConnectivityView(_text(root["view"], "connectivity document view"))
    except ValueError as error:
        raise ConnectivityRepresentationError(
            "connectivity document view is unsupported"
        ) from error
    source_values = _array(root["sources"], "connectivity document sources", _MAX_SOURCES)
    scope_values = _array(root["scopes"], "connectivity document scopes", _MAX_SCOPES)
    common = (
        _text(root["representation_id"], "representation id"),
        _text(root["source_graph_id"], "source graph id"),
        _text(root["top"], "top"),
        tuple(_text(item, f"source[{index}]") for index, item in enumerate(source_values)),
    )
    if view is ConnectivityView.COMPACT:
        compact = CompactCircuitGraph(
            *common,
            tuple(
                _compact_scope(item, f"scope[{index}]") for index, item in enumerate(scope_values)
            ),
        )
        return validate_compact_graph(compact)
    pin = PinCircuitGraph(
        *common,
        tuple(_pin_scope(item, f"scope[{index}]") for index, item in enumerate(scope_values)),
    )
    return validate_pin_graph(pin)


def load_connectivity_graph(
    path: str | Path, *, maximum_bytes: int = _MAX_DOCUMENT_BYTES
) -> CompactCircuitGraph | PinCircuitGraph:
    """Read a bounded UTF-8 connectivity document from ``path``."""

    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= _MAX_DOCUMENT_BYTES:
        raise ConnectivityRepresentationError(
            f"maximum_bytes must be in [1, {_MAX_DOCUMENT_BYTES}]"
        )
    source = Path(path)
    try:
        with source.open("rb") as stream:
            document = stream.read(maximum_bytes + 1)
    except OSError as error:
        raise ConnectivityRepresentationError(
            f"cannot read connectivity document {source}: {error}"
        ) from error
    if len(document) > maximum_bytes:
        raise ConnectivityRepresentationError("connectivity document exceeds its byte limit")
    return parse_connectivity_graph(document)


def assert_lossless_round_trip(graph: CircuitGraph) -> tuple[CompactCircuitGraph, PinCircuitGraph]:
    """Execute both independent round trips and return their validated views."""

    compact = compact_graph(graph)
    pin = compact_to_pin(compact)
    if compact_to_circuit(compact) != graph:
        raise ConnectivityRepresentationError("compact graph did not round-trip the source graph")
    if compact_to_circuit(pin_to_compact(pin)) != graph:
        raise ConnectivityRepresentationError("pin graph did not round-trip the source graph")
    if compact_to_pin(pin_to_compact(pin)) != pin:
        raise ConnectivityRepresentationError("pin and compact views are not mutually lossless")
    return compact, pin
