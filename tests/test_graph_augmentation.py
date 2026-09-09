from __future__ import annotations

from dataclasses import replace

import pytest

from topology_lantern import circuit_graph_identity, compact_graph, parse_spice
from topology_lantern.graph_augmentation import (
    GraphAugmentationError,
    rename_devices,
    restore_device_names,
)


def _graph():
    return parse_spice(
        ".subckt leaf a b params: gain=2\n"
        "m1 a a b b nch w=1u\nr1 a b 1k\nr2 b a 2k\n.ends\n"
        ".subckt top in out\nx1 in out leaf gain=3\nr1 in out 3k\n.ends\n",
        top="top",
    )


def test_seeded_device_renaming_is_reversible_and_preserves_all_other_fields():
    original = _graph()
    augmented = rename_devices(original, seed=7)
    assert augmented.source_graph_id == original.graph_id
    assert augmented.graph.graph_id != original.graph_id
    assert augmented == rename_devices(original, seed=7)
    assert restore_device_names(augmented) == original
    assert len(augmented.renames) == 4
    assert compact_graph(augmented.graph)
    old_devices = {device.node_id: device for scope in original.scopes for device in scope.devices}
    new_devices = {
        device.node_id: device for scope in augmented.graph.scopes for device in scope.devices
    }
    for entry in augmented.renames:
        old, new = old_devices[entry.original_id], new_devices[entry.renamed_id]
        assert replace(new, node_id=old.node_id, name=old.name) == old
        assert new.name[0] == old.name[0]
    for old, new in zip(original.scopes, augmented.graph.scopes, strict=True):
        assert replace(new, devices=old.devices) == old


def test_seed_changes_names_without_mutating_the_input_graph():
    graph = _graph()
    before = graph.as_dict()
    outputs = [rename_devices(graph, seed=seed) for seed in range(8)]
    assert len({output.graph.graph_id for output in outputs}) == 8
    assert all(restore_device_names(output) == graph for output in outputs)
    assert graph.as_dict() == before


@pytest.mark.parametrize("seed", [True, -1, 2**64, 0.5, "7", None])
def test_bad_seed_is_rejected(seed):
    with pytest.raises(GraphAugmentationError, match="seed"):
        rename_devices(_graph(), seed=seed)


def test_manifest_cannot_omit_duplicate_or_reorder_device_mappings():
    augmented = rename_devices(_graph(), seed=7)
    for entries in (
        augmented.renames[:-1],
        (*augmented.renames, augmented.renames[0]),
        tuple(reversed(augmented.renames)),
    ):
        with pytest.raises(GraphAugmentationError):
            restore_device_names(replace(augmented, renames=entries))


def test_manifest_tampering_cannot_restore_a_different_graph():
    augmented = rename_devices(_graph(), seed=7)
    entry = augmented.renames[0]
    for field, value in (
        ("scope_id", "unknown"),
        ("original_name", "r_tampered"),
        ("original_id", "unknown"),
        ("renamed_name", "r_tampered"),
        ("renamed_id", "unknown"),
    ):
        entries = (replace(entry, **{field: value}), *augmented.renames[1:])
        with pytest.raises(GraphAugmentationError):
            restore_device_names(replace(augmented, renames=entries))
    with pytest.raises(GraphAugmentationError):
        restore_device_names(replace(augmented, source_graph_id="sha256:" + "0" * 64))
    with pytest.raises(GraphAugmentationError, match="source evidence"):
        restore_device_names(replace(augmented, source_evidence_id="sha256:" + "0" * 64))
    for bad_evidence_id in (None, "SHA256:" + "0" * 64, "sha256:0"):
        with pytest.raises(GraphAugmentationError, match="source evidence id"):
            restore_device_names(replace(augmented, source_evidence_id=bad_evidence_id))
    with pytest.raises(GraphAugmentationError):
        restore_device_names(replace(augmented, seed=8))


def test_restoration_rejects_source_provenance_tampering():
    augmented = rename_devices(_graph(), seed=7)
    scope = augmented.graph.scopes[0]
    device = scope.devices[0]
    for forged_graph in (
        replace(augmented.graph, sources=("forged.sp",)),
        replace(
            augmented.graph,
            scopes=(
                replace(
                    scope,
                    devices=(
                        replace(device, source=replace(device.source, file="forged.sp")),
                        *scope.devices[1:],
                    ),
                ),
                *augmented.graph.scopes[1:],
            ),
        ),
        replace(
            augmented.graph,
            scopes=(
                replace(
                    scope,
                    devices=(
                        replace(device, source=replace(device.source, line=999)),
                        *scope.devices[1:],
                    ),
                ),
                *augmented.graph.scopes[1:],
            ),
        ),
    ):
        with pytest.raises(GraphAugmentationError, match="source evidence"):
            restore_device_names(replace(augmented, graph=forged_graph))


def test_device_free_graph_has_an_explicit_empty_reversible_augmentation():
    # The ingest front end rejects a whole empty source; the graph contract can
    # still represent a valid definition with ports and no primitive devices.
    graph = parse_spice(".subckt empty a b\nr1 a b 1k\n.ends\n", top="empty")
    scopes = tuple(replace(scope, devices=()) for scope in graph.scopes)
    graph = replace(graph, scopes=scopes, graph_id=circuit_graph_identity(graph.top, scopes))
    result = rename_devices(graph)
    assert result.renames == ()
    assert result.graph == graph
    assert restore_device_names(result) == graph


def test_wrong_public_types_are_typed_errors():
    with pytest.raises(GraphAugmentationError):
        rename_devices(None)
    with pytest.raises(GraphAugmentationError):
        restore_device_names(None)


def test_graph_preflight_rejects_mutable_scopes_and_excess_owners():
    graph = _graph()
    with pytest.raises(GraphAugmentationError, match="scope count"):
        rename_devices(replace(graph, scopes=list(graph.scopes)))
    scope = graph.scopes[0]
    oversized = replace(scope, devices=(scope.devices[0],) * 100_001)
    with pytest.raises(GraphAugmentationError, match="owner count"):
        rename_devices(replace(graph, scopes=(oversized,)))
    # Frozen dataclasses can still be manually populated with mutable values;
    # augmentation must not share such a source list in a supposedly immutable result.
    with pytest.raises(GraphAugmentationError, match="immutable canonical"):
        rename_devices(replace(graph, sources=list(graph.sources)))


def test_graph_preflight_bounds_sources_nets_edges_and_nested_parameters():
    graph = _graph()
    scope = graph.scopes[0]
    device = scope.devices[0]
    with pytest.raises(GraphAugmentationError, match="source count"):
        rename_devices(replace(graph, sources=(graph.sources[0],) * 33))
    with pytest.raises(GraphAugmentationError, match="net count"):
        rename_devices(replace(graph, scopes=(replace(scope, nets=(scope.nets[0],) * 100_001),)))
    with pytest.raises(GraphAugmentationError, match="owner count"):
        rename_devices(replace(graph, scopes=(replace(scope, ports=(scope.ports[0],) * 100_001),)))
    with pytest.raises(GraphAugmentationError, match="nested data"):
        rename_devices(
            replace(
                graph,
                scopes=(
                    replace(
                        scope,
                        devices=(
                            replace(device, terminals=(device.terminals[0],) * 500_001),
                            *scope.devices[1:],
                        ),
                    ),
                ),
            )
        )
    with pytest.raises(GraphAugmentationError, match="parameter count"):
        rename_devices(
            replace(
                graph,
                scopes=(
                    replace(
                        scope,
                        devices=(
                            replace(device, parameters=(device.parameters[0],) * 4_097),
                            *scope.devices[1:],
                        ),
                    ),
                ),
            )
        )
    many_parameters = replace(device, parameters=(device.parameters[0],) * 4_096)
    with pytest.raises(GraphAugmentationError, match="nested data"):
        rename_devices(replace(graph, scopes=(replace(scope, devices=(many_parameters,) * 245),)))


def test_graph_preflight_rejects_mutable_nested_collections():
    graph = _graph()
    scope = graph.scopes[0]
    device = scope.devices[0]
    with pytest.raises(GraphAugmentationError, match="immutable canonical"):
        rename_devices(
            replace(
                graph,
                scopes=(
                    replace(
                        scope,
                        devices=(replace(device, terminals=list(device.terminals)),),
                    ),
                ),
            )
        )
    mutable_pair = list(device.parameters[0])
    with pytest.raises(GraphAugmentationError, match="immutable canonical"):
        rename_devices(
            replace(
                graph,
                scopes=(
                    replace(
                        scope,
                        devices=(replace(device, parameters=(mutable_pair,)),),
                    ),
                ),
            )
        )


def test_invalid_and_duplicate_manifest_entries_are_rejected_before_restoration():
    augmented = rename_devices(_graph())
    for bad in (
        None,
        replace(augmented.renames[0], original_name=""),
        replace(augmented.renames[0], original_name="a" * 257),
    ):
        with pytest.raises(GraphAugmentationError, match="entry is invalid"):
            restore_device_names(replace(augmented, renames=(bad, *augmented.renames[1:])))
    entries = (augmented.renames[1], *augmented.renames[1:])
    with pytest.raises(GraphAugmentationError, match="duplicate device targets"):
        restore_device_names(replace(augmented, renames=entries))
