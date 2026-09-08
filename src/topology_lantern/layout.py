"""Versioned layout constraints, conflict checks, and evidence-only inference."""

from __future__ import annotations

import itertools
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar

from topology_lantern.circuit import CircuitDevice, CircuitElementKind, CircuitGraph
from topology_lantern.spec import _load_json_object
from topology_lantern.types import LanternError, SpecError

_MAX_CONSTRAINTS = 10_000
_MAX_REFERENCES = 100_000
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_EnumT = TypeVar("_EnumT", bound=StrEnum)


class LayoutConstraintError(LanternError, ValueError):
    """A layout-constraint contract is invalid or internally contradictory."""


class LayoutConstraintKind(StrEnum):
    SYMMETRY = "symmetry"
    COMMON_CENTROID = "common_centroid"
    MATCHING = "matching"
    ALIGNMENT = "alignment"
    ORDER = "order"
    KEEPOUT = "keepout"
    NET_PRIORITY = "net_priority"


class ConstraintOrigin(StrEnum):
    USER = "user"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class InferenceLimits:
    """Hard caps checked before any inferred constraint object is created."""

    max_inference_devices: int = 10_000
    max_pair_evaluations: int = 100_000
    max_inferred_constraints: int = 10_000


_DEFAULT_INFERENCE_LIMITS = InferenceLimits()


@dataclass(frozen=True, slots=True)
class LayoutConstraint:
    constraint_id: str
    kind: LayoutConstraintKind
    subjects: tuple[str, ...] = ()
    groups: tuple[tuple[str, ...], ...] = ()
    axis: str | None = None
    direction: str | None = None
    tolerance: float | None = None
    margin: float | None = None
    priority: int | None = None
    origin: ConstraintOrigin = ConstraintOrigin.USER
    confidence: float | None = None
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.constraint_id,
            "kind": self.kind.value,
            "origin": self.origin.value,
        }
        if self.subjects:
            result["subjects"] = list(self.subjects)
        if self.groups:
            result["groups"] = [list(group) for group in self.groups]
        for name in ("axis", "direction", "tolerance", "margin", "priority"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.origin is ConstraintOrigin.INFERRED:
            result["confidence"] = self.confidence
            result["evidence"] = list(self.evidence)
        return result

    def as_declaration_dict(self) -> dict[str, object]:
        if self.origin is not ConstraintOrigin.USER:
            raise LayoutConstraintError("only user constraints can be serialized as declarations")
        result = self.as_dict()
        del result["origin"]
        return result


@dataclass(frozen=True, slots=True)
class LayoutConstraintSet:
    graph_id: str
    constraints: tuple[LayoutConstraint, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "org.topology-lantern.layout-constraints",
            "version": 1,
            "graph_id": self.graph_id,
            "constraints": [item.as_declaration_dict() for item in self.constraints],
        }


@dataclass(frozen=True, slots=True)
class LayoutInferenceReport:
    graph_id: str
    user_constraints: tuple[LayoutConstraint, ...]
    inferred_constraints: tuple[LayoutConstraint, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "org.topology-lantern.layout-constraint-report",
            "version": 1,
            "graph_id": self.graph_id,
            "user_constraints": [item.as_dict() for item in self.user_constraints],
            "inferred_constraints": [item.as_dict() for item in self.inferred_constraints],
            "disclaimer": (
                "Inferred constraints are evidence-backed candidates, not user requirements."
            ),
        }


def _exact(value: object, fields: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise LayoutConstraintError(f"{context} has missing or unknown fields")
    return value


def _identifier(value: object, context: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise LayoutConstraintError(f"{context} is not a valid identifier")
    return value


def _enum(enum_type: type[_EnumT], value: object, context: str) -> _EnumT:
    if not isinstance(value, str):
        raise LayoutConstraintError(f"{context} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        choices = ", ".join(item.value for item in enum_type)
        raise LayoutConstraintError(f"{context} must be one of: {choices}") from error


def _number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LayoutConstraintError(f"{context} must be numeric")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise LayoutConstraintError(f"{context} must be finite") from error
    if not math.isfinite(result):
        raise LayoutConstraintError(f"{context} must be finite")
    return result


def _subjects(
    value: object,
    context: str,
    allowed: frozenset[str],
    *,
    minimum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise LayoutConstraintError(f"{context} must contain at least {minimum} IDs")
    if not all(isinstance(item, str) for item in value):
        raise LayoutConstraintError(f"{context} IDs must be strings")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise LayoutConstraintError(f"{context} IDs must be unique")
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise LayoutConstraintError(f"{context} references unknown graph ID {unknown[0]!r}")
    return result


def _require_same_scope(
    subjects: Sequence[str], placement_scopes: Mapping[str, str], context: str
) -> None:
    if len({placement_scopes[subject] for subject in subjects}) != 1:
        raise LayoutConstraintError(f"{context} must reference one definition scope")


def _constraint(
    raw: object,
    index: int,
    placeable: frozenset[str],
    placement_scopes: Mapping[str, str],
    net_ids: frozenset[str],
) -> LayoutConstraint:
    context = f"constraints[{index}]"
    if not isinstance(raw, Mapping):
        raise LayoutConstraintError(f"{context} must be an object")
    kind = _enum(LayoutConstraintKind, raw.get("kind"), f"{context}.kind")
    constraint_id = _identifier(raw.get("id"), f"{context}.id")
    if kind is LayoutConstraintKind.SYMMETRY:
        item = _exact(raw, {"id", "kind", "subjects", "axis"}, context)
        subjects = _subjects(item["subjects"], f"{context}.subjects", placeable, minimum=2)
        _require_same_scope(subjects, placement_scopes, f"{context}.subjects")
        axis = _enum_value(item["axis"], {"horizontal", "vertical"}, f"{context}.axis")
        return LayoutConstraint(constraint_id, kind, subjects=subjects, axis=axis)
    if kind is LayoutConstraintKind.COMMON_CENTROID:
        item = _exact(raw, {"id", "kind", "groups", "axis"}, context)
        groups_raw = item["groups"]
        if not isinstance(groups_raw, list) or len(groups_raw) != 2:
            raise LayoutConstraintError(f"{context}.groups must contain exactly two groups")
        groups = tuple(
            _subjects(group, f"{context}.groups[{group_index}]", placeable, minimum=1)
            for group_index, group in enumerate(groups_raw)
        )
        if len(groups[0]) != len(groups[1]) or set(groups[0]) & set(groups[1]):
            raise LayoutConstraintError(
                f"{context}.groups must be equal-sized and mutually disjoint"
            )
        _require_same_scope((*groups[0], *groups[1]), placement_scopes, f"{context}.groups")
        axis = _enum_value(item["axis"], {"horizontal", "vertical", "both"}, f"{context}.axis")
        return LayoutConstraint(constraint_id, kind, groups=groups, axis=axis)
    if kind is LayoutConstraintKind.MATCHING:
        item = _exact(raw, {"id", "kind", "subjects", "tolerance"}, context)
        subjects = _subjects(item["subjects"], f"{context}.subjects", placeable, minimum=2)
        _require_same_scope(subjects, placement_scopes, f"{context}.subjects")
        tolerance = _number(item["tolerance"], f"{context}.tolerance")
        if tolerance < 0.0:
            raise LayoutConstraintError(f"{context}.tolerance must be non-negative")
        return LayoutConstraint(constraint_id, kind, subjects=subjects, tolerance=tolerance)
    if kind is LayoutConstraintKind.ALIGNMENT:
        item = _exact(raw, {"id", "kind", "subjects", "axis"}, context)
        subjects = _subjects(item["subjects"], f"{context}.subjects", placeable, minimum=2)
        _require_same_scope(subjects, placement_scopes, f"{context}.subjects")
        axis = _enum_value(item["axis"], {"x", "y"}, f"{context}.axis")
        return LayoutConstraint(constraint_id, kind, subjects=subjects, axis=axis)
    if kind is LayoutConstraintKind.ORDER:
        item = _exact(raw, {"id", "kind", "subjects", "direction"}, context)
        subjects = _subjects(item["subjects"], f"{context}.subjects", placeable, minimum=2)
        _require_same_scope(subjects, placement_scopes, f"{context}.subjects")
        direction = _enum_value(
            item["direction"],
            {"left_to_right", "right_to_left", "bottom_to_top", "top_to_bottom"},
            f"{context}.direction",
        )
        return LayoutConstraint(constraint_id, kind, subjects=subjects, direction=direction)
    if kind is LayoutConstraintKind.KEEPOUT:
        item = _exact(raw, {"id", "kind", "subjects", "margin"}, context)
        subjects = _subjects(item["subjects"], f"{context}.subjects", placeable, minimum=1)
        _require_same_scope(subjects, placement_scopes, f"{context}.subjects")
        margin = _number(item["margin"], f"{context}.margin")
        if margin < 0.0:
            raise LayoutConstraintError(f"{context}.margin must be non-negative")
        return LayoutConstraint(constraint_id, kind, subjects=subjects, margin=margin)
    item = _exact(raw, {"id", "kind", "subjects", "priority"}, context)
    subjects = _subjects(item["subjects"], f"{context}.subjects", net_ids, minimum=1)
    priority = item["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
        raise LayoutConstraintError(f"{context}.priority must be an integer in [0, 100]")
    return LayoutConstraint(constraint_id, kind, subjects=subjects, priority=priority)


def _enum_value(value: object, choices: set[str], context: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise LayoutConstraintError(f"{context} must be one of: {', '.join(sorted(choices))}")
    return value


def _normalized_order(constraint: LayoutConstraint) -> tuple[str, tuple[str, ...]]:
    if constraint.direction in {"left_to_right", "right_to_left"}:
        axis = "x"
        reverse = constraint.direction == "right_to_left"
    else:
        axis = "y"
        reverse = constraint.direction == "top_to_bottom"
    return axis, tuple(reversed(constraint.subjects)) if reverse else constraint.subjects


def _semantic_key(constraint: LayoutConstraint) -> tuple[object, ...]:
    order_axis: str | None = None
    if constraint.kind is LayoutConstraintKind.ORDER:
        order_axis, subjects = _normalized_order(constraint)
    else:
        subjects = tuple(sorted(constraint.subjects))
    groups = tuple(sorted(tuple(sorted(group)) for group in constraint.groups))
    return (
        constraint.kind.value,
        subjects,
        groups,
        constraint.axis if constraint.kind is not LayoutConstraintKind.ORDER else order_axis,
        constraint.direction if constraint.kind is not LayoutConstraintKind.ORDER else None,
        constraint.tolerance,
        constraint.margin,
        constraint.priority,
    )


def _order_edges(constraint: LayoutConstraint) -> tuple[str, tuple[tuple[str, str], ...]]:
    axis, subjects = _normalized_order(constraint)
    return axis, tuple(itertools.pairwise(subjects))


def _has_cycle(edges: Sequence[tuple[str, str]]) -> bool:
    adjacency: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {}
    for left, right in edges:
        indegree.setdefault(left, 0)
        indegree.setdefault(right, 0)
        if right not in adjacency[left]:
            adjacency[left].add(right)
            indegree[right] += 1
        adjacency.setdefault(right, set())
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    consumed = 0
    while ready:
        node = ready.pop()
        consumed += 1
        for neighbor in sorted(adjacency[node], reverse=True):
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                ready.append(neighbor)
    return consumed != len(indegree)


def _contract_alignment_groups(
    edges: Sequence[tuple[str, str]], groups: Sequence[frozenset[str]]
) -> tuple[tuple[str, str], ...]:
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != node:
            next_node = parent[node]
            parent[node] = root
            node = next_node
        return root

    for group in groups:
        ordered = sorted(group)
        if not ordered:
            continue
        anchor = find(ordered[0])
        for node in ordered[1:]:
            other = find(node)
            if anchor != other:
                smaller, larger = sorted((anchor, other))
                parent[larger] = smaller
                anchor = smaller
    return tuple((find(left), find(right)) for left, right in edges)


def validate_layout_conflicts(constraints: Sequence[LayoutConstraint]) -> None:
    """Reject duplicates and contradictions with deterministic diagnostics."""
    ids: set[str] = set()
    semantics: set[tuple[object, ...]] = set()
    symmetry_axes: dict[frozenset[str], str] = {}
    alignment_axes: dict[frozenset[str], str] = {}
    alignment_groups: dict[str, list[frozenset[str]]] = {"x": [], "y": []}
    priorities: dict[str, int] = {}
    order_edges: dict[str, list[tuple[str, str]]] = {"x": [], "y": []}
    for constraint in constraints:
        if constraint.constraint_id in ids:
            raise LayoutConstraintError(f"duplicate constraint ID {constraint.constraint_id!r}")
        ids.add(constraint.constraint_id)
        semantic = _semantic_key(constraint)
        if semantic in semantics:
            raise LayoutConstraintError("duplicate semantic layout constraint")
        semantics.add(semantic)
        subject_set = frozenset(constraint.subjects)
        if constraint.kind is LayoutConstraintKind.SYMMETRY:
            previous_axis = symmetry_axes.get(subject_set)
            if previous_axis is not None and previous_axis != constraint.axis:
                raise LayoutConstraintError("same symmetry group declares conflicting axes")
            symmetry_axes[subject_set] = constraint.axis or ""
        elif constraint.kind is LayoutConstraintKind.ALIGNMENT:
            previous_axis = alignment_axes.get(subject_set)
            if previous_axis is not None and previous_axis != constraint.axis:
                raise LayoutConstraintError("same alignment group declares conflicting axes")
            alignment_axes[subject_set] = constraint.axis or ""
            if constraint.axis in alignment_groups:
                alignment_groups[constraint.axis].append(subject_set)
        elif constraint.kind is LayoutConstraintKind.NET_PRIORITY:
            if constraint.priority is None:
                raise LayoutConstraintError("net-priority constraint has no priority")
            for net in constraint.subjects:
                previous_priority = priorities.get(net)
                if previous_priority is not None and previous_priority != constraint.priority:
                    raise LayoutConstraintError(f"net {net!r} has conflicting priorities")
                priorities[net] = constraint.priority
        elif constraint.kind is LayoutConstraintKind.ORDER:
            order_axis, new_edges = _order_edges(constraint)
            order_edges[order_axis].extend(new_edges)
    for axis, axis_edges in order_edges.items():
        if _has_cycle(axis_edges):
            raise LayoutConstraintError(f"{axis}-axis order constraints contain a cycle")
        contracted = _contract_alignment_groups(axis_edges, alignment_groups[axis])
        if any(left == right for left, right in contracted) or _has_cycle(contracted):
            raise LayoutConstraintError(
                f"{axis}-axis ordering conflicts with equal-coordinate alignment"
            )


def _validate_reference_budget(raw_constraints: Sequence[object]) -> None:
    references = 0
    for raw in raw_constraints:
        if not isinstance(raw, Mapping):
            continue
        subjects = raw.get("subjects")
        if isinstance(subjects, list):
            references += len(subjects)
        groups = raw.get("groups")
        if isinstance(groups, list):
            references += sum(len(group) for group in groups if isinstance(group, list))
        if references > _MAX_REFERENCES:
            raise LayoutConstraintError("layout document exceeds the graph-reference limit")


def parse_layout_constraints(value: Mapping[str, Any], graph: CircuitGraph) -> LayoutConstraintSet:
    """Validate a strict version-1 user constraint document against a graph."""
    top = _exact(value, {"schema", "version", "graph_id", "constraints"}, "layout document")
    version = top["version"]
    if (
        top["schema"] != "org.topology-lantern.layout-constraints"
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != 1
    ):
        raise LayoutConstraintError("unsupported layout-constraint schema or version")
    if top["graph_id"] != graph.graph_id:
        raise LayoutConstraintError("layout constraints do not belong to this circuit graph")
    raw_constraints = top["constraints"]
    if not isinstance(raw_constraints, list):
        raise LayoutConstraintError("layout constraints must be an array")
    if len(raw_constraints) > _MAX_CONSTRAINTS:
        raise LayoutConstraintError("layout document exceeds the constraint limit")
    _validate_reference_budget(raw_constraints)
    placement_scopes = graph.placement_scope_map()
    placeable = frozenset(placement_scopes)
    net_ids = graph.net_ids()
    constraints = tuple(
        _constraint(raw, index, placeable, placement_scopes, net_ids)
        for index, raw in enumerate(raw_constraints)
    )
    validate_layout_conflicts(constraints)
    return LayoutConstraintSet(graph.graph_id, constraints)


def load_layout_constraints(path: str | Path, graph: CircuitGraph) -> LayoutConstraintSet:
    """Load strict bounded JSON and bind all references to ``graph``."""
    try:
        value = _load_json_object(path, "layout constraint document")
    except SpecError as error:
        raise LayoutConstraintError(str(error)) from error
    return parse_layout_constraints(value, graph)


def layout_constraints_json(constraints: LayoutConstraintSet, *, pretty: bool = False) -> str:
    """Serialize a validated user declaration set using the version-1 contract."""
    return (
        json.dumps(
            constraints.as_dict(),
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def _inferred_id(kind: LayoutConstraintKind, payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"tlc-inferred-{kind.value}-{sha256(canonical.encode('ascii')).hexdigest()[:16]}"


def _candidate(
    kind: LayoutConstraintKind,
    *,
    subjects: tuple[str, ...] = (),
    groups: tuple[tuple[str, ...], ...] = (),
    axis: str | None = None,
    tolerance: float | None = None,
    confidence: float,
    evidence: tuple[str, ...],
) -> LayoutConstraint:
    if not 0.0 <= confidence <= 1.0 or not evidence:
        raise LayoutConstraintError("inferred constraints require confidence and evidence")
    payload = (kind.value, subjects, groups, axis, tolerance)
    return LayoutConstraint(
        _inferred_id(kind, payload),
        kind,
        subjects=subjects,
        groups=groups,
        axis=axis,
        tolerance=tolerance,
        origin=ConstraintOrigin.INFERRED,
        confidence=confidence,
        evidence=evidence,
    )


_PreparedInference = tuple[
    tuple[CircuitDevice, ...],
    frozenset[str],
    tuple[tuple[CircuitDevice, ...], ...],
]


def _validate_inference_limits(limits: InferenceLimits) -> None:
    for name in InferenceLimits.__dataclass_fields__:
        value = getattr(limits, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LayoutConstraintError(f"{name} must be a positive integer")
        if value > getattr(_DEFAULT_INFERENCE_LIMITS, name):
            raise LayoutConstraintError(
                f"{name} cannot exceed the built-in safety ceiling "
                f"{getattr(_DEFAULT_INFERENCE_LIMITS, name)}"
            )


def _parallel_groups(mosfets: tuple[CircuitDevice, ...]) -> tuple[tuple[CircuitDevice, ...], ...]:
    parallel: dict[
        tuple[str | None, tuple[tuple[str, str], ...], str, str, str, str],
        list[CircuitDevice],
    ] = defaultdict(list)
    for device in mosfets:
        nets = device.terminal_map()
        parallel[
            (
                device.model,
                device.parameters,
                nets["s"],
                nets["b"],
                nets["g"],
                nets["d"],
            )
        ].append(device)
    return tuple(tuple(items) for items in parallel.values() if len(items) >= 2)


def _prepare_inference(
    graph: CircuitGraph, limits: InferenceLimits
) -> tuple[_PreparedInference, ...]:
    _validate_inference_limits(limits)
    prepared: list[_PreparedInference] = []
    device_count = 0
    pair_evaluations = 0
    for scope in graph.scopes:
        device_count += len(scope.devices)
        if device_count > limits.max_inference_devices:
            raise LayoutConstraintError("circuit exceeds the inference-device limit")
        mosfets = tuple(
            device for device in scope.devices if device.kind is CircuitElementKind.MOSFET
        )
        groups = _parallel_groups(mosfets)
        pair_evaluations += len(mosfets) * (len(mosfets) - 1) // 2
        pair_evaluations += len(groups) * (len(groups) - 1) // 2
        if pair_evaluations > limits.max_pair_evaluations:
            raise LayoutConstraintError("circuit exceeds the inference pair-evaluation limit")
        prepared.append((mosfets, frozenset(port.net_id for port in scope.ports), groups))
    return tuple(prepared)


def _pair_motif(left: CircuitDevice, right: CircuitDevice, port_nets: frozenset[str]) -> str | None:
    left_nets = left.terminal_map()
    right_nets = right.terminal_map()
    if (
        left.model != right.model
        or left.parameters != right.parameters
        or left_nets["s"] != right_nets["s"]
        or left_nets["b"] != right_nets["b"]
    ):
        return None
    same_gate = left_nets["g"] == right_nets["g"]
    diode_reference = left_nets["d"] == left_nets["g"] or right_nets["d"] == right_nets["g"]
    if same_gate and diode_reference:
        return "mirror"
    if (
        left_nets["g"] != right_nets["g"]
        and left_nets["g"] in port_nets
        and right_nets["g"] in port_nets
        and left_nets["d"] != right_nets["d"]
    ):
        return "differential"
    return None


def _common_centroid_motif(
    left_group: tuple[CircuitDevice, ...],
    right_group: tuple[CircuitDevice, ...],
    port_nets: frozenset[str],
) -> bool:
    left_nets = left_group[0].terminal_map()
    right_nets = right_group[0].terminal_map()
    return (
        len(left_group) == len(right_group)
        and left_group[0].model == right_group[0].model
        and left_group[0].parameters == right_group[0].parameters
        and left_nets["s"] == right_nets["s"]
        and left_nets["b"] == right_nets["b"]
        and left_nets["g"] != right_nets["g"]
        and left_nets["d"] != right_nets["d"]
        and left_nets["g"] in port_nets
        and right_nets["g"] in port_nets
    )


def _candidate_count(prepared: tuple[_PreparedInference, ...]) -> int:
    count = 0
    for mosfets, port_nets, groups in prepared:
        for left, right in itertools.combinations(mosfets, 2):
            motif = _pair_motif(left, right, port_nets)
            count += 1 if motif == "mirror" else 2 if motif == "differential" else 0
        count += sum(
            _common_centroid_motif(left, right, port_nets)
            for left, right in itertools.combinations(groups, 2)
        )
    return count


def _mos_pairs(prepared: tuple[_PreparedInference, ...]) -> list[LayoutConstraint]:
    inferred: list[LayoutConstraint] = []
    for mosfets, port_nets, groups in prepared:
        for left, right in itertools.combinations(mosfets, 2):
            motif = _pair_motif(left, right, port_nets)
            if motif is None:
                continue
            left_nets = left.terminal_map()
            subjects = tuple(sorted((left.node_id, right.node_id)))
            common_evidence = (
                f"same MOS model {left.model}",
                "identical normalized MOS parameters",
                f"shared source net {left_nets['s']}",
                f"shared bulk net {left_nets['b']}",
            )
            if motif == "mirror":
                inferred.append(
                    _candidate(
                        LayoutConstraintKind.MATCHING,
                        subjects=subjects,
                        tolerance=0.0,
                        confidence=0.98,
                        evidence=(
                            *common_evidence,
                            f"shared gate net {left_nets['g']}",
                            "one device is diode-connected",
                        ),
                    )
                )
            else:
                differential_evidence = (
                    *common_evidence,
                    "distinct gates connect to declared subcircuit ports",
                    "drain nets are distinct",
                )
                inferred.extend(
                    (
                        _candidate(
                            LayoutConstraintKind.MATCHING,
                            subjects=subjects,
                            tolerance=0.0,
                            confidence=0.92,
                            evidence=differential_evidence,
                        ),
                        _candidate(
                            LayoutConstraintKind.SYMMETRY,
                            subjects=subjects,
                            axis="vertical",
                            confidence=0.85,
                            evidence=differential_evidence,
                        ),
                    )
                )
        for left_group, right_group in itertools.combinations(groups, 2):
            if not _common_centroid_motif(left_group, right_group, port_nets):
                continue
            left_nets = left_group[0].terminal_map()
            group_ids = (
                tuple(sorted(device.node_id for device in left_group)),
                tuple(sorted(device.node_id for device in right_group)),
            )
            inferred.append(
                _candidate(
                    LayoutConstraintKind.COMMON_CENTROID,
                    groups=group_ids,
                    axis="both",
                    confidence=0.80,
                    evidence=(
                        f"two equal same-gate/same-drain groups of {len(left_group)} MOSFETs",
                        f"same MOS model {left_group[0].model}",
                        "identical normalized MOS parameters",
                        f"shared source net {left_nets['s']}",
                        "group gates connect to distinct declared ports",
                    ),
                )
            )
    unique = {_semantic_key(item): item for item in inferred}
    return sorted(unique.values(), key=lambda item: item.constraint_id)


def infer_layout_constraints(
    graph: CircuitGraph, *, limits: InferenceLimits = _DEFAULT_INFERENCE_LIMITS
) -> tuple[LayoutConstraint, ...]:
    """Return bounded conservative candidates marked inferred with evidence."""
    prepared = _prepare_inference(graph, limits)
    if _candidate_count(prepared) > limits.max_inferred_constraints:
        raise LayoutConstraintError("circuit exceeds the inferred-constraint limit")
    return tuple(_mos_pairs(prepared))


def _revalidate_declared_constraints(
    graph: CircuitGraph, constraints: LayoutConstraintSet
) -> LayoutConstraintSet:
    if constraints.graph_id != graph.graph_id:
        raise LayoutConstraintError("layout constraints do not belong to this circuit graph")
    for item in constraints.constraints:
        if not isinstance(item, LayoutConstraint):
            raise LayoutConstraintError("declared constraint set contains an invalid object")
        if not isinstance(item.kind, LayoutConstraintKind):
            raise LayoutConstraintError("declared constraint has an invalid kind")
        if item.origin is not ConstraintOrigin.USER:
            raise LayoutConstraintError("declared constraint set contains a non-user origin")
        if item.confidence is not None or item.evidence:
            raise LayoutConstraintError("user constraint contains inference-only metadata")
    try:
        value = constraints.as_dict()
    except (AttributeError, KeyError, TypeError) as error:
        raise LayoutConstraintError("declared constraint object cannot be serialized") from error
    return parse_layout_constraints(value, graph)


def layout_inference_report(
    graph: CircuitGraph,
    constraints: LayoutConstraintSet | None = None,
    *,
    limits: InferenceLimits = _DEFAULT_INFERENCE_LIMITS,
) -> LayoutInferenceReport:
    """Keep user declarations and inferred candidates in separate report arrays."""
    validated = (
        _revalidate_declared_constraints(graph, constraints) if constraints is not None else None
    )
    return LayoutInferenceReport(
        graph.graph_id,
        validated.constraints if validated is not None else (),
        infer_layout_constraints(graph, limits=limits),
    )


def layout_report_json(report: LayoutInferenceReport, *, pretty: bool = False) -> str:
    """Serialize a report without collapsing inference into user intent."""
    return (
        json.dumps(
            report.as_dict(),
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )
