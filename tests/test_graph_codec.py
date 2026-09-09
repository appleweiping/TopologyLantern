from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from topology_lantern import (
    CompactCircuitGraph,
    ConnectivityRepresentationError,
    ConnectivityView,
    OwnerKind,
    PinCircuitGraph,
    assert_lossless_round_trip,
    circuit_graph_identity,
    compact_graph,
    compact_to_circuit,
    compact_to_pin,
    connectivity_graph_json,
    load_connectivity_graph,
    parse_connectivity_graph,
    parse_spice,
    pin_to_compact,
    validate_compact_graph,
    validate_pin_graph,
)

HIERARCHICAL = """
.global 0 supply
.subckt leaf p n params: ratio=2 bias=1
m1 p p n 0 nch w={ratio*1u} l=180n
r1 p supply 2k
.ends leaf
.subckt top in out
xgain in out leaf ratio=3
c1 out 0 1p
rload out supply 10k
.ends top
"""


def _views() -> tuple[CompactCircuitGraph, PinCircuitGraph]:
    return assert_lossless_round_trip(
        parse_spice(HIERARCHICAL, top="top", source="clean room/café fixture.sp")
    )


def test_compact_and_pin_views_are_lossless_content_addressed_and_deterministic() -> None:
    source = parse_spice(HIERARCHICAL, top="top", source="clean room/café fixture.sp")
    compact, pin = assert_lossless_round_trip(source)
    again_compact, again_pin = assert_lossless_round_trip(source)

    assert compact == again_compact
    assert pin == again_pin
    assert compact.representation_id.startswith("sha256:")
    assert pin.representation_id.startswith("sha256:")
    assert compact.source_graph_id == pin.source_graph_id == source.graph_id
    assert compact_to_circuit(compact) == source
    assert compact_to_circuit(pin_to_compact(pin)) == source
    assert compact_to_pin(compact) == pin
    compact_document = json.loads(connectivity_graph_json(compact))
    pin_document = json.loads(connectivity_graph_json(pin, pretty=True))
    assert compact_document["schema"] == "org.topology-lantern.connectivity-graph"
    assert compact_document["view"] == ConnectivityView.COMPACT
    assert pin_document["view"] == ConnectivityView.PIN_LEVEL
    assert compact_document["representation_id"] == compact.representation_id
    assert pin_document["representation_id"] == pin.representation_id


def test_terminal_labels_global_identity_and_instance_metadata_survive_both_views() -> None:
    compact, pin = _views()
    leaf, top = compact.scopes
    instance = next(owner for owner in top.owners if owner.kind is OwnerKind.INSTANCE)
    instance_edges = sorted(
        (edge for edge in top.edges if edge.owner_id == instance.owner_id),
        key=lambda edge: edge.ordinal,
    )
    assert [edge.terminal for edge in instance_edges] == ["p", "n"]
    assert dict(instance.parameters) == {"ratio": "3"}
    assert dict(instance.effective_parameters) == {"bias": "1", "ratio": "3"}

    leaf_globals = {net.name: net.node_id for net in leaf.nets if net.is_global}
    top_globals = {net.name: net.node_id for net in top.nets if net.is_global}
    global_ids = {
        name: node_id for name, node_id in leaf_globals.items() if top_globals.get(name) == node_id
    }
    assert global_ids == {
        "0": next(net.node_id for net in top.nets if net.name == "0"),
        "supply": next(net.node_id for net in top.nets if net.name == "supply"),
    }
    assert len(pin.scopes[1].pins) == len(pin.scopes[1].links) == len(top.edges)


def test_representation_identity_binds_view_body_but_not_the_other_view() -> None:
    compact, pin = _views()
    altered = replace(compact, sources=(*compact.sources, "other.sp"))
    with pytest.raises(ConnectivityRepresentationError, match="representation identity"):
        validate_compact_graph(altered)
    assert compact.representation_id != pin.representation_id


def test_source_graph_identity_is_recomputed_from_each_view_semantics() -> None:
    source = parse_spice(HIERARCHICAL, top="top", source="fixture.sp")
    compact, pin = assert_lossless_round_trip(source)
    assert circuit_graph_identity(source.top, source.scopes) == source.graph_id

    for graph, validator in ((compact, validate_compact_graph), (pin, validate_pin_graph)):
        tampered = replace(graph, source_graph_id="sha256:" + "f" * 64)
        identity = (
            "sha256:"
            + sha256(
                json.dumps(
                    tampered.body_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest()
        )
        tampered = replace(tampered, representation_id=identity)
        with pytest.raises(ConnectivityRepresentationError, match="source graph identity"):
            validator(tampered)  # type: ignore[arg-type]


def test_canonical_node_id_and_sequence_order_cannot_be_relabelled() -> None:
    compact, pin = _views()
    scope = compact.scopes[1]
    with pytest.raises(ConnectivityRepresentationError, match=r"scope\[1\] identity"):
        validate_compact_graph(
            replace(compact, scopes=(*compact.scopes[:1], replace(scope, scope_id="tlg-scope-bad")))
        )

    owner = scope.owners[0]
    with pytest.raises(ConnectivityRepresentationError, match=r"owner\[0\] identity"):
        validate_compact_graph(
            replace(
                compact,
                scopes=(
                    *compact.scopes[:1],
                    replace(
                        scope, owners=(replace(owner, owner_id="tlg-port-bad"), *scope.owners[1:])
                    ),
                ),
            )
        )
    with pytest.raises(ConnectivityRepresentationError, match="net order"):
        validate_compact_graph(
            replace(compact, scopes=(*compact.scopes[:1], replace(scope, nets=scope.nets[::-1])))
        )
    with pytest.raises(ConnectivityRepresentationError, match="edge order"):
        validate_compact_graph(
            replace(compact, scopes=(*compact.scopes[:1], replace(scope, edges=scope.edges[::-1])))
        )

    pin_scope = pin.scopes[1]
    with pytest.raises(ConnectivityRepresentationError, match="pin order"):
        validate_pin_graph(
            replace(pin, scopes=(*pin.scopes[:1], replace(pin_scope, pins=pin_scope.pins[::-1])))
        )
    with pytest.raises(ConnectivityRepresentationError, match="link order"):
        validate_pin_graph(
            replace(pin, scopes=(*pin.scopes[:1], replace(pin_scope, links=pin_scope.links[::-1])))
        )


def test_compact_validator_rejects_unknown_vertices_and_electrical_contract_drift() -> None:
    compact, _pin = _views()
    scope = compact.scopes[1]
    edge = scope.edges[0]
    unknown = replace(edge, net_id="tlg-net-does-not-exist")
    with pytest.raises(ConnectivityRepresentationError, match="unknown vertex"):
        validate_compact_graph(
            replace(
                compact,
                scopes=(*compact.scopes[:1], replace(scope, edges=(unknown, *scope.edges[1:]))),
            )
        )

    device_index = next(
        index for index, owner in enumerate(scope.owners) if owner.kind is OwnerKind.DEVICE
    )
    device = scope.owners[device_index]
    bad_device = replace(device, model="unexpected")
    owners = (*scope.owners[:device_index], bad_device, *scope.owners[device_index + 1 :])
    with pytest.raises(ConnectivityRepresentationError, match="model metadata"):
        validate_compact_graph(
            replace(compact, scopes=(*compact.scopes[:1], replace(scope, owners=owners)))
        )

    instance = next(owner for owner in scope.owners if owner.kind is OwnerKind.INSTANCE)
    instance_edges = [edge for edge in scope.edges if edge.owner_id == instance.owner_id]
    changed = replace(instance_edges[0], terminal="wrong")
    edges = tuple(changed if edge is instance_edges[0] else edge for edge in scope.edges)
    with pytest.raises(ConnectivityRepresentationError, match="identity does not match"):
        validate_compact_graph(
            replace(compact, scopes=(*compact.scopes[:1], replace(scope, edges=edges)))
        )


def test_port_flags_and_noncanonical_aliases_are_rejected() -> None:
    compact, _pin = _views()
    scope = compact.scopes[1]
    port_net_index = next(index for index, net in enumerate(scope.nets) if net.is_port)
    port_net = scope.nets[port_net_index]
    bad_nets = (
        *scope.nets[:port_net_index],
        replace(port_net, is_port=False),
        *scope.nets[port_net_index + 1 :],
    )
    with pytest.raises(ConnectivityRepresentationError, match="port owners and net flags"):
        validate_compact_graph(
            replace(compact, scopes=(*compact.scopes[:1], replace(scope, nets=bad_nets)))
        )

    leaf = compact.scopes[0]
    local_leaf = next(net for net in leaf.nets if not net.is_global)
    local_top_index = next(index for index, net in enumerate(scope.nets) if not net.is_global)
    aliased_top_nets = (
        *scope.nets[:local_top_index],
        replace(scope.nets[local_top_index], node_id=local_leaf.node_id),
        *scope.nets[local_top_index + 1 :],
    )
    with pytest.raises(ConnectivityRepresentationError, match="net identity is not canonical"):
        validate_compact_graph(
            replace(compact, scopes=(leaf, replace(scope, nets=aliased_top_nets)))
        )


def test_port_owner_must_connect_to_its_same_named_local_net() -> None:
    source = parse_spice(".subckt top a b\nd1 a b dmod\n.ends\n", top="top")
    scope = source.scopes[0]
    left, right = scope.ports
    swapped = (
        replace(left, net_id=right.net_id),
        replace(right, net_id=left.net_id),
    )
    malformed = replace(source, scopes=(replace(scope, ports=swapped),))
    assert circuit_graph_identity(malformed.top, malformed.scopes) == source.graph_id
    with pytest.raises(ConnectivityRepresentationError, match="same-named net"):
        compact_graph(malformed)


def test_pin_validator_rejects_missing_links_and_unsafe_metadata() -> None:
    _compact, pin = _views()
    scope = pin.scopes[1]
    with pytest.raises(ConnectivityRepresentationError, match="exactly one link"):
        validate_pin_graph(
            replace(pin, scopes=(*pin.scopes[:1], replace(scope, links=scope.links[1:])))
        )

    owner = scope.owners[0]
    unsafe_owner = replace(owner, name="safe\u202ehidden")
    with pytest.raises(ConnectivityRepresentationError, match="control or separator"):
        validate_pin_graph(
            replace(
                pin,
                scopes=(*pin.scopes[:1], replace(scope, owners=(unsafe_owner, *scope.owners[1:]))),
            )
        )


def test_public_type_checks_fail_closed() -> None:
    with pytest.raises(ConnectivityRepresentationError, match="CircuitGraph"):
        compact_graph(object())  # type: ignore[arg-type]
    with pytest.raises(ConnectivityRepresentationError, match="wrong object type"):
        validate_compact_graph(object())  # type: ignore[arg-type]
    with pytest.raises(ConnectivityRepresentationError, match="wrong object type"):
        validate_pin_graph(object())  # type: ignore[arg-type]
    with pytest.raises(ConnectivityRepresentationError, match="object type"):
        connectivity_graph_json(object())  # type: ignore[arg-type]


def test_published_schema_accepts_both_views_and_rejects_cross_view_shape() -> None:
    schema_path = (
        Path(__file__).parents[1] / "docs" / "schemas" / "connectivity-graph-1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    compact, pin = _views()
    validator.validate(compact.as_dict())
    validator.validate(pin.as_dict())

    wrong_view = {**compact.as_dict(), "view": "pin-level"}
    assert list(validator.iter_errors(wrong_view))
    extra_field = {**pin.as_dict(), "unexpected": True}
    assert list(validator.iter_errors(extra_field))


def test_serialized_views_strictly_parse_and_load_without_losing_information(
    tmp_path: Path,
) -> None:
    compact, pin = _views()
    assert parse_connectivity_graph(connectivity_graph_json(compact)) == compact
    assert parse_connectivity_graph(connectivity_graph_json(pin, pretty=True).encode()) == pin

    path = tmp_path / "pin.json"
    payload = connectivity_graph_json(pin).encode()
    path.write_bytes(payload)
    assert load_connectivity_graph(path) == pin
    with pytest.raises(ConnectivityRepresentationError, match="byte limit"):
        load_connectivity_graph(path, maximum_bytes=len(payload) - 1)
    with pytest.raises(ConnectivityRepresentationError, match="maximum_bytes"):
        load_connectivity_graph(path, maximum_bytes=True)


def test_parser_rejects_ambiguous_or_mixed_view_documents() -> None:
    compact, _pin = _views()
    canonical = connectivity_graph_json(compact)
    duplicate = canonical.replace('"top":', '"top":"forged","top":', 1)
    with pytest.raises(ConnectivityRepresentationError, match="duplicate JSON member 'top'"):
        parse_connectivity_graph(duplicate)

    mixed = compact.as_dict()
    mixed["view"] = "pin-level"
    with pytest.raises(ConnectivityRepresentationError, match="invalid shape"):
        parse_connectivity_graph(json.dumps(mixed))

    extra = {**compact.as_dict(), "untrusted": True}
    with pytest.raises(ConnectivityRepresentationError, match="invalid shape"):
        parse_connectivity_graph(json.dumps(extra))


def test_parser_rejects_non_finite_invalid_utf8_and_tampered_identity() -> None:
    compact, _pin = _views()
    with pytest.raises(ConnectivityRepresentationError, match="non-finite"):
        parse_connectivity_graph('{"value":NaN}')
    with pytest.raises(ConnectivityRepresentationError, match="valid UTF-8"):
        parse_connectivity_graph(b"\xff")
    with pytest.raises(ConnectivityRepresentationError, match="text or bytes"):
        parse_connectivity_graph(bytearray(b"{}"))  # type: ignore[arg-type]

    tampered = compact.as_dict()
    tampered["top"] = "leaf"
    with pytest.raises(ConnectivityRepresentationError, match="representation identity"):
        parse_connectivity_graph(json.dumps(tampered))
