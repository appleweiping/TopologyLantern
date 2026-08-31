"""Incremental structural constraints and final candidate review notes."""

from __future__ import annotations

from collections import Counter

from topology_lantern.graph import connected_components, endpoints
from topology_lantern.spec import DesignSpec
from topology_lantern.types import (
    ConstraintViolation,
    DeviceKind,
    PortRole,
    SearchState,
    Severity,
)


def partial_violations(spec: DesignSpec, state: SearchState) -> tuple[ConstraintViolation, ...]:
    """Return violations that are already conclusive during search."""

    result: list[ConstraintViolation] = []
    topology = state.topology
    if len(topology.devices) > spec.limits.max_devices:
        result.append(
            ConstraintViolation(
                "LIM001",
                Severity.ERROR,
                f"device count exceeds configured maximum {spec.limits.max_devices}",
            )
        )
    disallowed = sorted(
        device.name for device in topology.devices if device.kind not in spec.allowed_devices
    )
    if disallowed:
        result.append(
            ConstraintViolation(
                "DEV001",
                Severity.ERROR,
                "topology uses a device family excluded by the specification",
                tuple(disallowed),
            )
        )
    for device in topology.devices:
        if device.kind in {DeviceKind.NMOS, DeviceKind.PMOS}:
            if device.net("d") == device.net("s"):
                result.append(
                    ConstraintViolation(
                        "MOS001",
                        Severity.ERROR,
                        "MOS drain and source are shorted in the conceptual graph",
                        (device.name,),
                    )
                )
            expected_bulk = "vss" if device.kind is DeviceKind.NMOS else "vdd"
            if device.net("b") != expected_bulk:
                result.append(
                    ConstraintViolation(
                        "MOS002",
                        Severity.ERROR,
                        f"{device.kind.value} bulk is not tied to {expected_bulk}",
                        (device.name,),
                    )
                )
    return tuple(result)


def final_violations(spec: DesignSpec, state: SearchState) -> tuple[ConstraintViolation, ...]:
    """Evaluate complete topology invariants and non-fatal review notes."""

    result = list(partial_violations(spec, state))
    topology = state.topology
    port_map = topology.port_map()
    endpoint_map = endpoints(topology)
    for port, role in sorted(port_map.items()):
        if not endpoint_map.get(port):
            result.append(
                ConstraintViolation(
                    "PORT001",
                    Severity.ERROR,
                    f"{role.value} port {port!r} has no device endpoint",
                    (port,),
                )
            )
    for net in topology.nets:
        if net in port_map:
            continue
        count = len(endpoint_map.get(net, ()))
        if count < 2:
            result.append(
                ConstraintViolation(
                    "NET001",
                    Severity.ERROR,
                    f"internal net {net!r} has fewer than two endpoints",
                    (net,),
                )
            )
    if state.obligations:
        result.append(
            ConstraintViolation(
                "OBL001",
                Severity.ERROR,
                "candidate still has unresolved obligations",
                tuple(item.key for item in state.obligations),
            )
        )
    roles = Counter(port_map.values())
    expected_inputs = 2 if spec.input_mode == "differential" else 1
    expected_outputs = 2 if spec.output_mode == "differential" else 1
    if roles[PortRole.INPUT] != expected_inputs:
        result.append(
            ConstraintViolation(
                "PORT002", Severity.ERROR, "input port count does not match the specification"
            )
        )
    if roles[PortRole.OUTPUT] != expected_outputs:
        result.append(
            ConstraintViolation(
                "PORT003", Severity.ERROR, "output port count does not match the specification"
            )
        )
    if len(connected_components(topology)) > 1:
        result.append(
            ConstraintViolation(
                "CONN001",
                Severity.ERROR,
                "topology contains disconnected net components",
            )
        )
    facts = state.fact_map()
    if spec.input_mode == "differential" and facts.get("symmetry") != "paired":
        result.append(
            ConstraintViolation(
                "SYM001",
                Severity.WARNING,
                "differential intent lacks an explicit paired symmetry fact",
            )
        )
    if facts.get("tail_structure") == "resistor":
        result.append(
            ConstraintViolation(
                "BIAS001",
                Severity.WARNING,
                "resistive tail bias is sensitive to common-mode and process variation",
                ("R_TAIL",),
            )
        )
    if facts.get("output_structure") == "source_follower":
        result.append(
            ConstraintViolation(
                "OUT001",
                Severity.NOTE,
                "buffered output adds a stage whose bias and swing still require sizing",
                ("M_BUFFER", "I_BUFFER"),
            )
        )
    if spec.require_compensation and facts.get("compensation") != "explicit_capacitor":
        result.append(
            ConstraintViolation(
                "COMP001",
                Severity.ERROR,
                "required compensation intent was not realized",
            )
        )
    return tuple(sorted(result, key=lambda item: (item.severity.value, item.code, item.subjects)))


def has_error(violations: tuple[ConstraintViolation, ...]) -> bool:
    return any(item.severity is Severity.ERROR for item in violations)
