from __future__ import annotations

from dataclasses import replace

import pytest

from topology_lantern.canonical import (
    canonical_encoding,
    state_signature,
    topology_signature,
)
from topology_lantern.graph import (
    GraphError,
    TopologyBuilder,
    connected_components,
    count_kinds,
    device_neighbors,
    endpoints,
    validate_topology,
)
from topology_lantern.types import (
    Device,
    DeviceKind,
    Fact,
    Obligation,
    ObligationKind,
    PortRole,
    SearchState,
    Topology,
)


def base_builder() -> TopologyBuilder:
    return (
        TopologyBuilder()
        .add_port("vdd", PortRole.SUPPLY)
        .add_port("vss", PortRole.GROUND)
        .add_port("vin", PortRole.INPUT)
        .add_port("vout", PortRole.OUTPUT)
    )


def simple_topology() -> Topology:
    return (
        base_builder()
        .add_net("middle")
        .add_device(
            "M1",
            DeviceKind.NMOS,
            {"d": "middle", "g": "vin", "s": "vss", "b": "vss"},
        )
        .add_device("R1", DeviceKind.RESISTOR, {"p": "vdd", "n": "middle"})
        .add_device("C1", DeviceKind.CAPACITOR, {"p": "middle", "n": "vout"})
        .commit()
    )


def test_builder_creates_sorted_immutable_topology() -> None:
    topology = simple_topology()
    assert [port.name for port in topology.ports] == ["vdd", "vin", "vout", "vss"]
    assert list(topology.nets) == sorted(topology.nets)
    assert [device.name for device in topology.devices] == ["C1", "M1", "R1"]
    assert topology.device_map()["M1"].net("g") == "vin"
    with pytest.raises(KeyError):
        topology.device_map()["M1"].net("x")
    validate_topology(topology)


def test_builder_copy_does_not_mutate_source() -> None:
    original = simple_topology()
    modified = (
        TopologyBuilder(original).add_device("R2", "resistor", {"p": "vout", "n": "vss"}).commit()
    )
    assert "R2" not in original.device_map()
    assert "R2" in modified.device_map()


def test_add_port_is_idempotent_for_same_role_and_rejects_change() -> None:
    builder = TopologyBuilder().add_port("x", "input").add_port("x", PortRole.INPUT)
    assert builder.commit().port_map()["x"] is PortRole.INPUT
    with pytest.raises(GraphError, match="already has role"):
        builder.add_port("x", "output")


@pytest.mark.parametrize("name", ["", " leading", "trailing ", "has space", "nul\0"])
def test_builder_rejects_invalid_identifiers(name: str) -> None:
    with pytest.raises(GraphError):
        TopologyBuilder().add_net(name)


def test_builder_rejects_duplicate_device_and_bad_terminals() -> None:
    builder = base_builder().add_device("R1", "resistor", {"p": "vdd", "n": "vss"})
    with pytest.raises(GraphError, match="duplicate"):
        builder.add_device("R1", "resistor", {"p": "vin", "n": "vss"})
    with pytest.raises(GraphError, match="missing"):
        base_builder().add_device("R1", "resistor", {"p": "vdd"})
    with pytest.raises(GraphError, match="unknown"):
        base_builder().add_device("R1", "resistor", {"p": "vdd", "n": "vss", "extra": "vin"})


def test_rename_internal_net_merges_into_existing_port() -> None:
    original = simple_topology()
    renamed = TopologyBuilder(original).rename_net("middle", "vout").commit()
    assert "middle" not in renamed.nets
    assert all(net != "middle" for device in renamed.devices for _, net in device.connections)
    assert len(endpoints(renamed)["vout"]) == 4
    with pytest.raises(GraphError, match="declared port"):
        TopologyBuilder(original).rename_net("vin", "other")
    with pytest.raises(GraphError, match="unknown net"):
        TopologyBuilder(original).rename_net("missing", "x")


def test_endpoint_neighbor_and_component_queries() -> None:
    topology = simple_topology()
    endpoint_map = endpoints(topology)
    assert endpoint_map["middle"] == (("C1", "p"), ("M1", "d"), ("R1", "n"))
    assert device_neighbors(topology, "M1") == ("C1", "R1")
    assert connected_components(topology) == (("middle", "vdd", "vin", "vout", "vss"),)
    assert count_kinds(topology, (DeviceKind.NMOS,)) == 1
    assert count_kinds(topology, (DeviceKind.RESISTOR, DeviceKind.CAPACITOR)) == 2
    with pytest.raises(GraphError, match="unknown device"):
        device_neighbors(topology, "missing")


def test_disconnected_components_are_sorted() -> None:
    topology = (
        TopologyBuilder()
        .add_net("a")
        .add_net("b")
        .add_net("x")
        .add_net("y")
        .add_device("R1", "resistor", {"p": "a", "n": "b"})
        .add_device("R2", "resistor", {"p": "x", "n": "y"})
        .commit()
    )
    assert connected_components(topology) == (("a", "b"), ("x", "y"))


def test_validate_topology_detects_manual_invariant_breaks() -> None:
    valid = simple_topology()
    with pytest.raises(GraphError, match="duplicate ports"):
        validate_topology(replace(valid, ports=(*valid.ports, valid.ports[0])))
    with pytest.raises(GraphError, match="duplicate nets"):
        validate_topology(replace(valid, nets=(*valid.nets, valid.nets[0])))
    with pytest.raises(GraphError, match="duplicate devices"):
        validate_topology(replace(valid, devices=(*valid.devices, valid.devices[0])))
    with pytest.raises(GraphError, match="port"):
        validate_topology(replace(valid, nets=tuple(net for net in valid.nets if net != "vin")))
    broken_order = Device(
        "M1", DeviceKind.NMOS, tuple(reversed(valid.device_map()["M1"].connections))
    )
    with pytest.raises(GraphError, match="terminal ordering"):
        validate_topology(replace(valid, devices=(broken_order,)))
    unknown_net = Device("R", DeviceKind.RESISTOR, (("p", "nope"), ("n", "vss")))
    with pytest.raises(GraphError, match="unknown net"):
        validate_topology(replace(valid, devices=(unknown_net,)))


def renamed_equivalent(internal: str, names: tuple[str, str, str]) -> Topology:
    mos, resistor, capacitor = names
    return (
        base_builder()
        .add_net(internal)
        .add_device(
            mos,
            "nmos",
            {"d": internal, "g": "vin", "s": "vss", "b": "vss"},
        )
        .add_device(resistor, "resistor", {"p": "vdd", "n": internal})
        .add_device(capacitor, "capacitor", {"p": internal, "n": "vout"})
        .commit()
    )


def test_exact_signature_ignores_internal_net_and_device_names() -> None:
    one = renamed_equivalent("middle", ("M1", "R1", "C1"))
    two = renamed_equivalent("renamed", ("Q", "LOAD", "COUPLE"))
    assert canonical_encoding(one).startswith("exact:")
    assert canonical_encoding(one) == canonical_encoding(two)
    assert topology_signature(one) == topology_signature(two)


def test_signature_preserves_ports_terminals_kinds_and_attributes() -> None:
    original = simple_topology()
    rewired = (
        base_builder()
        .add_net("middle")
        .add_device(
            "M1",
            "nmos",
            {"d": "middle", "g": "vout", "s": "vss", "b": "vss"},
        )
        .add_device("R1", "resistor", {"p": "vdd", "n": "middle"})
        .add_device("C1", "capacitor", {"p": "middle", "n": "vin"})
        .commit()
    )
    attributed = (
        TopologyBuilder(original)
        .add_device("R2", "resistor", {"p": "vout", "n": "vss"}, {"role": "load"})
        .commit()
    )
    assert topology_signature(original) != topology_signature(rewired)
    assert topology_signature(original) != topology_signature(attributed)


def test_large_graph_uses_bounded_refinement_deterministically() -> None:
    builder = TopologyBuilder().add_port("vss", "ground")
    previous = "vss"
    for index in range(10):
        current = f"n{index}"
        builder.add_device(f"R{index}", "resistor", {"p": previous, "n": current})
        previous = current
    topology = builder.commit()
    first = canonical_encoding(topology, max_permutations=2)
    second = canonical_encoding(topology, max_permutations=2)
    assert first.startswith("refined:")
    assert first == second


def _circulant(offsets: tuple[int, ...]) -> Topology:
    builder = TopologyBuilder()
    for index in range(6):
        builder.add_net(f"n{index}")
    edge = 0
    for source in range(6):
        for offset in offsets:
            target = (source + offset) % 6
            builder.add_device(f"R{edge}", "resistor", {"p": f"n{source}", "n": f"n{target}"})
            edge += 1
    return builder.commit()


def test_refinement_collision_is_not_treated_as_graph_equivalence() -> None:
    adjacent_chords = _circulant((1, 2))
    opposite_chords = _circulant((1, 3))
    assert canonical_encoding(adjacent_chords, max_permutations=720) != canonical_encoding(
        opposite_chords, max_permutations=720
    )
    assert canonical_encoding(adjacent_chords, max_permutations=1) != canonical_encoding(
        opposite_chords, max_permutations=1
    )


def test_state_signature_includes_facts_and_obligations_not_trace() -> None:
    topology = simple_topology()
    obligation = Obligation("load", ObligationKind.LOAD)
    base = SearchState(topology, (obligation,), (Fact("mode", "a"),))
    fact_changed = SearchState(topology, (obligation,), (Fact("mode", "b"),))
    obligation_changed = SearchState(
        topology, (Obligation("out", ObligationKind.OUTPUT),), base.facts
    )
    assert state_signature(base) != state_signature(fact_changed)
    assert state_signature(base) != state_signature(obligation_changed)
    assert state_signature(base) == state_signature(replace(base, trace=()))


def test_domain_as_dict_methods_are_json_shaped() -> None:
    topology = simple_topology()
    assert topology.as_dict()["nets"] == list(topology.nets)
    assert topology.ports[0].as_dict()["role"] in {role.value for role in PortRole}
    assert topology.devices[0].as_dict()["connections"]
