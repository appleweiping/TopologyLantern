"""Validated topology construction and graph inspection helpers."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping

from topology_lantern.types import TERMINALS, Device, DeviceKind, Port, PortRole, Topology


class GraphError(ValueError):
    """A rewrite attempted to construct an invalid graph."""


class TopologyBuilder:
    """Copy-on-write builder used by independent rewrite rules."""

    def __init__(self, topology: Topology | None = None) -> None:
        source = topology or Topology((), (), ())
        self._ports: dict[str, PortRole] = source.port_map()
        self._nets: set[str] = set(source.nets)
        self._devices: dict[str, Device] = source.device_map()

    def add_net(self, name: str) -> TopologyBuilder:
        _validate_identifier(name, "net")
        self._nets.add(name)
        return self

    def add_port(self, name: str, role: PortRole | str) -> TopologyBuilder:
        _validate_identifier(name, "port")
        selected = role if isinstance(role, PortRole) else PortRole(role)
        existing = self._ports.get(name)
        if existing is not None and existing is not selected:
            raise GraphError(f"port {name!r} already has role {existing.value}")
        self._ports[name] = selected
        self._nets.add(name)
        return self

    def rename_net(self, old: str, new: str) -> TopologyBuilder:
        """Merge ``old`` into ``new`` while preserving port identity."""

        if old not in self._nets:
            raise GraphError(f"unknown net {old!r}")
        _validate_identifier(new, "net")
        if old in self._ports and old != new:
            raise GraphError(f"cannot rename declared port {old!r}")
        self._nets.discard(old)
        self._nets.add(new)
        replaced: dict[str, Device] = {}
        for name, device in self._devices.items():
            connections = tuple(
                (terminal, new if net == old else net) for terminal, net in device.connections
            )
            replaced[name] = Device(
                name=device.name,
                kind=device.kind,
                connections=connections,
                attributes=device.attributes,
            )
        self._devices = replaced
        return self

    def add_device(
        self,
        name: str,
        kind: DeviceKind | str,
        connections: Mapping[str, str],
        attributes: Mapping[str, str] | None = None,
    ) -> TopologyBuilder:
        _validate_identifier(name, "device")
        if name in self._devices:
            raise GraphError(f"duplicate device name {name!r}")
        selected = kind if isinstance(kind, DeviceKind) else DeviceKind(kind)
        required = TERMINALS[selected]
        unknown = sorted(set(connections) - set(required))
        missing = [terminal for terminal in required if terminal not in connections]
        if unknown or missing:
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if unknown:
                details.append(f"unknown {', '.join(unknown)}")
            raise GraphError(f"invalid terminals for {selected.value}: {'; '.join(details)}")
        normalized: list[tuple[str, str]] = []
        for terminal in required:
            net = connections[terminal]
            _validate_identifier(net, "net")
            self._nets.add(net)
            normalized.append((terminal, net))
        normalized_attributes = tuple(
            sorted((str(key), str(value)) for key, value in (attributes or {}).items())
        )
        self._devices[name] = Device(
            name=name,
            kind=selected,
            connections=tuple(normalized),
            attributes=normalized_attributes,
        )
        return self

    def commit(self) -> Topology:
        topology = Topology(
            ports=tuple(Port(name, role) for name, role in sorted(self._ports.items())),
            nets=tuple(sorted(self._nets)),
            devices=tuple(self._devices[name] for name in sorted(self._devices)),
        )
        validate_topology(topology)
        return topology


def _validate_identifier(value: str, context: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise GraphError(f"{context} name must be a non-empty trimmed string")
    if any(character.isspace() for character in value) or "\0" in value:
        raise GraphError(f"{context} name contains unsupported characters: {value!r}")


def validate_topology(topology: Topology) -> None:
    port_names = [port.name for port in topology.ports]
    net_names = list(topology.nets)
    device_names = [device.name for device in topology.devices]
    if len(port_names) != len(set(port_names)):
        raise GraphError("topology contains duplicate ports")
    if len(net_names) != len(set(net_names)):
        raise GraphError("topology contains duplicate nets")
    if len(device_names) != len(set(device_names)):
        raise GraphError("topology contains duplicate devices")
    if not set(port_names).issubset(net_names):
        raise GraphError("every port must also be a net")
    known_nets = set(net_names)
    for device in topology.devices:
        if tuple(key for key, _ in device.connections) != TERMINALS[device.kind]:
            raise GraphError(f"device {device.name!r} has invalid terminal ordering")
        if any(net not in known_nets for _, net in device.connections):
            raise GraphError(f"device {device.name!r} references an unknown net")


def endpoints(topology: Topology) -> dict[str, tuple[tuple[str, str], ...]]:
    result: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for device in topology.devices:
        for terminal, net in device.connections:
            result[net].append((device.name, terminal))
    return {net: tuple(sorted(values)) for net, values in sorted(result.items())}


def device_neighbors(topology: Topology, device_name: str) -> tuple[str, ...]:
    selected = topology.device_map().get(device_name)
    if selected is None:
        raise GraphError(f"unknown device {device_name!r}")
    touched = {net for _, net in selected.connections}
    neighbors = {
        device.name
        for device in topology.devices
        if device.name != device_name and touched & {net for _, net in device.connections}
    }
    return tuple(sorted(neighbors))


def connected_components(topology: Topology) -> tuple[tuple[str, ...], ...]:
    adjacency: dict[str, set[str]] = {net: set() for net in topology.nets}
    for device in topology.devices:
        nets = {net for _, net in device.connections}
        for left in nets:
            adjacency[left].update(nets - {left})
    unseen = set(topology.nets)
    components: list[tuple[str, ...]] = []
    while unseen:
        start = min(unseen)
        queue = deque([start])
        visited: set[str] = set()
        while queue:
            net = queue.popleft()
            if net in visited:
                continue
            visited.add(net)
            queue.extend(sorted(adjacency[net] - visited))
        unseen -= visited
        components.append(tuple(sorted(visited)))
    return tuple(sorted(components))


def count_kinds(topology: Topology, kinds: Iterable[DeviceKind]) -> int:
    selected = set(kinds)
    return sum(device.kind in selected for device in topology.devices)
