"""Identifier-independent canonical encodings for topology de-duplication."""

from __future__ import annotations

import itertools
import json
import math
from hashlib import sha256

from topology_lantern.types import SearchState, Topology


def candidate_id(signature: str) -> str:
    """Derive the stable public identifier for one canonical signature."""

    return f"TL-{sha256(signature.encode()).hexdigest()[:12]}"


def _port_labels(topology: Topology) -> dict[str, str]:
    return {port.name: f"port:{port.role.value}:{port.name}" for port in topology.ports}


def _encode_with_mapping(topology: Topology, mapping: dict[str, str]) -> str:
    devices: list[tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]] = []
    for device in topology.devices:
        terminals = tuple(f"{terminal}={mapping[net]}" for terminal, net in device.connections)
        devices.append((device.kind.value, terminals, device.attributes))
    payload = {
        "ports": sorted(
            (port.name, port.role.value, mapping[port.name]) for port in topology.ports
        ),
        "devices": sorted(devices),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _exact_encoding(topology: Topology, internal: tuple[str, ...]) -> str:
    port_mapping = _port_labels(topology)
    canonical_names = tuple(f"n{index}" for index in range(len(internal)))
    best: str | None = None
    for assignment in itertools.permutations(canonical_names):
        mapping = dict(port_mapping)
        mapping.update(zip(internal, assignment, strict=True))
        encoded = _encode_with_mapping(topology, mapping)
        if best is None or encoded < best:
            best = encoded
    if best is None:
        return _encode_with_mapping(topology, port_mapping)
    return best


def _refined_encoding(topology: Topology) -> str:
    """Deterministic color refinement fallback for larger internal graphs."""

    port_map = topology.port_map()
    net_colors = {
        net: (f"port:{port_map[net].value}:{net}" if net in port_map else "internal")
        for net in topology.nets
    }
    device_colors = {
        device.name: f"device:{device.kind.value}:{device.attributes}"
        for device in topology.devices
    }
    for _ in range(len(topology.nets) + len(topology.devices) + 1):
        next_net: dict[str, str] = {}
        for net in topology.nets:
            incident = sorted(
                f"{terminal}:{device_colors[device.name]}"
                for device in topology.devices
                for terminal, connected in device.connections
                if connected == net
            )
            next_net[net] = sha256(
                (net_colors[net] + "|" + "|".join(incident)).encode()
            ).hexdigest()[:24]
        next_device: dict[str, str] = {}
        for device in topology.devices:
            incident = [f"{terminal}:{next_net[net]}" for terminal, net in device.connections]
            next_device[device.name] = sha256(
                (device_colors[device.name] + "|" + "|".join(incident)).encode()
            ).hexdigest()[:24]
        if next_net == net_colors and next_device == device_colors:
            break
        net_colors = next_net
        device_colors = next_device
    payload = {
        "method": "color_refinement",
        "nets": sorted(net_colors.values()),
        "devices": sorted(device_colors.values()),
        "ports": sorted((name, role.value) for name, role in port_map.items()),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _labeled_collision_guard(topology: Topology) -> str:
    """Preserve distinct refined graphs without claiming label-independent equivalence."""

    mapping = _port_labels(topology)
    mapping.update(
        {net: f"internal:{net}" for net in topology.nets if net not in topology.port_map()}
    )
    return _encode_with_mapping(topology, mapping)


def canonical_encoding(topology: Topology, *, max_permutations: int = 40_320) -> str:
    """Return an exact small-graph encoding and a bounded refined fallback."""

    ports = set(topology.port_map())
    internal = tuple(sorted(set(topology.nets) - ports))
    permutations = math.factorial(len(internal))
    if permutations <= max_permutations:
        return "exact:" + _exact_encoding(topology, internal)
    bucket = _refined_encoding(topology)
    guard = _labeled_collision_guard(topology)
    return "refined:" + json.dumps(
        {"bucket": bucket, "collision_guard": guard},
        sort_keys=True,
        separators=(",", ":"),
    )


def topology_signature(topology: Topology, *, max_permutations: int = 40_320) -> str:
    encoding = canonical_encoding(topology, max_permutations=max_permutations)
    return sha256(encoding.encode()).hexdigest()


def state_signature(state: SearchState, *, max_permutations: int = 40_320) -> str:
    payload = {
        "topology": canonical_encoding(state.topology, max_permutations=max_permutations),
        "obligations": [item.as_dict() for item in state.obligations],
        "facts": [item.as_dict() for item in state.facts],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode()).hexdigest()
