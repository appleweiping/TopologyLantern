from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from topology_lantern import (
    DatasetError,
    DatasetPartition,
    OwnerKind,
    circuit_graph_identity,
    compact_graph,
    conservative_leakage_bucket,
    encode_graph_sequence,
    load_spice,
    parse_spice,
    rename_devices,
    renamed_dataset_record,
    root_dataset_record,
    split_graph_dataset,
    stack_dataset_splits,
    traversal_dataset_record,
    validate_dataset_split,
    validate_graph_dataset,
)
from topology_lantern import graph_dataset as dataset_module
from topology_lantern.circuit import stable_node_id


def _graph():
    return parse_spice(
        ".subckt leaf a b params: gain=2\n"
        "m1 a a mid b nch w=1u l=180n\nr1 mid b 1k\n.ends\n"
        ".subckt top in out\nx1 in out leaf gain=3\nc1 out 0 1p\n.ends\n",
        top="top",
        source="dataset-fixture.sp",
    )


def _rename_internal_net(graph):
    scope_index = next(
        index
        for index, scope in enumerate(graph.scopes)
        if any(not net.is_port and not net.is_global for net in scope.nets)
    )
    scope = graph.scopes[scope_index]
    old = next(net for net in scope.nets if not net.is_port and not net.is_global)
    new_name = "renamed_internal"
    new_id = stable_node_id("net", scope.name, new_name)
    nets = tuple(
        replace(net, node_id=new_id, name=new_name) if net.node_id == old.node_id else net
        for net in scope.nets
    )
    nets = tuple(sorted(nets, key=lambda net: net.name))
    devices = tuple(
        replace(
            device,
            terminals=tuple(
                (terminal, new_id if net_id == old.node_id else net_id)
                for terminal, net_id in device.terminals
            ),
        )
        for device in scope.devices
    )
    scopes = (
        *graph.scopes[:scope_index],
        replace(scope, nets=nets, devices=devices),
        *graph.scopes[scope_index + 1 :],
    )
    return replace(graph, graph_id=circuit_graph_identity(graph.top, scopes), scopes=scopes)


def _partition_of(split, record_id):
    for partition in DatasetPartition:
        if any(record.record_id == record_id for record in getattr(split, partition.value)):
            return partition
    raise AssertionError(f"record {record_id} was not assigned")


def _rehash_record(record):
    return replace(record, record_id=dataset_module._identity(dataset_module._record_body(record)))


def test_root_rename_and_traversal_records_form_one_valid_leakage_group():
    graph = _graph()
    root = root_dataset_record(graph)
    augmented = rename_devices(graph, seed=7)
    renamed = renamed_dataset_record(root, augmented)
    sequence = encode_graph_sequence(compact_graph(augmented.graph), seed=9)
    traversed = traversal_dataset_record(renamed, sequence)

    records = validate_graph_dataset((traversed, root, renamed))
    assert {record.root_lineage_id for record in records} == {graph.graph_id}
    assert root.root_source_evidence_id == augmented.source_evidence_id
    assert {record.leakage_bucket.bucket_id for record in records} == {
        root.leakage_bucket.bucket_id
    }
    assert renamed.immediate_graph_id == augmented.graph.graph_id
    assert traversed.immediate_graph_id == renamed.immediate_graph_id

    split = split_graph_dataset(records, seed=11, weights=(8, 1, 1))
    assert {_partition_of(split, record.record_id) for record in records} == {
        _partition_of(split, root.record_id)
    }


def test_conservative_bucket_ignores_supported_names_models_and_sizing():
    graph = _graph()
    renamed_net = _rename_internal_net(graph)
    renamed_devices = rename_devices(graph, seed=19).graph
    scope = graph.scopes[0]
    mosfet = scope.devices[0]
    changed_mosfet = replace(
        mosfet,
        model="pdk_model_alias",
        parameters=(("l", "360n"), ("w", "9u")),
    )
    changed_scope = replace(scope, devices=(changed_mosfet, *scope.devices[1:]))
    sized_scopes = (changed_scope, *graph.scopes[1:])
    sized = replace(
        graph,
        scopes=sized_scopes,
        graph_id=circuit_graph_identity(graph.top, sized_scopes),
    )

    buckets = {
        conservative_leakage_bucket(candidate).bucket_id
        for candidate in (graph, renamed_net, renamed_devices, sized)
    }
    assert len(buckets) == 1


def test_conservative_bucket_ignores_all_structural_identifier_names():
    left = parse_spice(
        ".global vdd\n"
        ".subckt leaf a b\nr1 a mid 1k\nc1 mid b 1p\nr2 mid vdd 2k\n.ends\n"
        ".subckt top in out\nx1 in out leaf\nr3 out vdd 3k\n.ends\n",
        top="top",
        source="left.sp",
    )
    right = parse_spice(
        ".global supply\n"
        ".subckt cell x y\nrfoo x hidden 9k\ncbar hidden y 7p\nrbaz hidden supply 8k\n.ends\n"
        ".subckt wrapper p q\nxother p q cell\nrqux q supply 4k\n.ends\n",
        top="wrapper",
        source="right.sp",
    )
    assert left.graph_id != right.graph_id
    assert conservative_leakage_bucket(left) == conservative_leakage_bucket(right)


def test_global_net_port_marker_is_invariant_to_definition_renaming_order():
    left = parse_spice(
        ".global vdd\n"
        ".subckt leaf a\nr1 a vdd 1k\n.ends\n"
        ".subckt top vdd out\nx1 out leaf\n.ends\n",
        top="top",
    )
    right = parse_spice(
        ".global vdd\n"
        ".subckt zleaf a\nr1 a vdd 1k\n.ends\n"
        ".subckt atop vdd out\nx1 out zleaf\n.ends\n",
        top="atop",
    )
    assert left.graph_id != right.graph_id
    assert conservative_leakage_bucket(left) == conservative_leakage_bucket(right)

    left_record = root_dataset_record(left)
    right_record = root_dataset_record(right)
    split = split_graph_dataset((left_record, right_record), seed=29, weights=(1, 1, 1))
    assert _partition_of(split, left_record.record_id) == _partition_of(
        split, right_record.record_id
    )


def test_hierarchy_port_indexing_is_cached_per_definition_not_per_instance():
    class CountingName(str):
        hashes = 0

        def __hash__(self):
            type(self).hashes += 1
            return super().__hash__()

    instances = "".join(f"x{index} a b c d leaf\n" for index in range(32))
    graph = parse_spice(
        ".subckt leaf p0 p1 p2 p3\nr1 p0 p1 1k\nr2 p2 p3 1k\n.ends\n"
        f".subckt top a b c d\n{instances}.ends\n",
        top="top",
    )
    compact = compact_graph(graph)
    leaf_index = next(index for index, scope in enumerate(compact.scopes) if scope.name == "leaf")
    leaf = compact.scopes[leaf_index]
    owners = tuple(
        replace(owner, name=CountingName(owner.name)) if owner.kind is OwnerKind.PORT else owner
        for owner in leaf.owners
    )
    instrumented = replace(
        compact,
        scopes=(
            *compact.scopes[:leaf_index],
            replace(leaf, owners=owners),
            *compact.scopes[leaf_index + 1 :],
        ),
    )

    CountingName.hashes = 0
    dataset_module._bucket_from_compact(instrumented)
    assert CountingName.hashes <= 2 * len(owners)


def test_conservative_bucket_uses_terminal_labeled_connectivity():
    graph = _graph()
    scope = graph.scopes[0]
    mosfet = scope.devices[0]
    terminals = dict(mosfet.terminals)
    terminals["d"], terminals["s"] = terminals["s"], terminals["d"]
    changed_mosfet = replace(
        mosfet,
        terminals=tuple((name, terminals[name]) for name, _value in mosfet.terminals),
    )
    changed_scope = replace(scope, devices=(changed_mosfet, *scope.devices[1:]))
    changed_scopes = (changed_scope, *graph.scopes[1:])
    changed = replace(
        graph,
        scopes=changed_scopes,
        graph_id=circuit_graph_identity(graph.top, changed_scopes),
    )
    assert (
        conservative_leakage_bucket(changed).bucket_id
        != conservative_leakage_bucket(graph).bucket_id
    )


def test_conservative_bucket_preserves_ordered_ports_and_global_marking():
    ordered = parse_spice(".subckt top a b\nd1 a b dmod\n.ends\n", top="top")
    reordered = parse_spice(".subckt top b a\nd1 a b dmod\n.ends\n", top="top")
    assert (
        conservative_leakage_bucket(ordered).bucket_id
        != conservative_leakage_bucket(reordered).bucket_id
    )

    global_net = parse_spice(".global supply\n.subckt top a\nr1 a supply 1k\n.ends\n", top="top")
    local_net = parse_spice(".subckt top a\nr1 a supply 1k\n.ends\n", top="top")
    assert (
        conservative_leakage_bucket(global_net).bucket_id
        != conservative_leakage_bucket(local_net).bucket_id
    )


def test_conservative_bucket_deliberately_overgroups_a_known_nonisomorphic_pair():
    cycle = parse_spice(
        ".subckt top p\nrp p p 1k\n"
        "r1 n1 n2 1k\nr2 n2 n3 1k\nr3 n3 n4 1k\n"
        "r4 n4 n5 1k\nr5 n5 n6 1k\nr6 n6 n1 1k\n.ends\n",
        top="top",
    )
    two_cycles = parse_spice(
        ".subckt top p\nrp p p 2k\n"
        "r1 n1 n2 1k\nr2 n2 n3 1k\nr3 n3 n1 1k\n"
        "r4 n4 n5 1k\nr5 n5 n6 1k\nr6 n6 n4 1k\n.ends\n",
        top="top",
    )
    assert cycle.graph_id != two_cycles.graph_id
    assert (
        conservative_leakage_bucket(cycle).bucket_id
        == conservative_leakage_bucket(two_cycles).bucket_id
    )


def test_split_is_order_independent_and_repeatable():
    roots = []
    for count in range(1, 7):
        nodes = ("a", *(f"n{index}" for index in range(1, count)), "b")
        devices = "".join(
            f"r{index} {nodes[index]} {nodes[index + 1]} 1k\n" for index in range(count)
        )
        roots.append(
            root_dataset_record(
                parse_spice(
                    f".subckt top a b\n{devices}.ends\n",
                    top="top",
                    source=f"root-{count}.sp",
                )
            )
        )
    root_tuple = tuple(roots)
    assert len({root.leakage_bucket.bucket_id for root in root_tuple}) == len(root_tuple)
    first = split_graph_dataset(root_tuple, seed=23, weights=(2, 1, 1))
    second = split_graph_dataset(tuple(reversed(root_tuple)), seed=23, weights=(2, 1, 1))
    assert first == second
    assert first.split_id == split_graph_dataset(root_tuple, seed=23, weights=(2, 1, 1)).split_id


def test_stack_rejects_name_independent_leakage_across_partitions():
    graph = _graph()
    renamed_net = _rename_internal_net(graph)
    left = root_dataset_record(graph)
    right = root_dataset_record(renamed_net)
    assert left.root_lineage_id != right.root_lineage_id
    assert left.leakage_bucket.bucket_id == right.leakage_bucket.bucket_id

    pair = split_graph_dataset((left, right), seed=5, weights=(1, 1, 1))
    assert _partition_of(pair, left.record_id) == _partition_of(pair, right.record_id)

    separate = None
    for seed in range(1_000):
        left_split = split_graph_dataset((left,), seed=seed, weights=(1, 1, 1))
        right_split = split_graph_dataset((right,), seed=seed, weights=(1, 1, 1))
        if _partition_of(left_split, left.record_id) != _partition_of(right_split, right.record_id):
            separate = (left_split, right_split)
            break
    assert separate is not None
    with pytest.raises(DatasetError, match="leakage group spans partitions"):
        stack_dataset_splits(*separate)


def test_missing_parent_and_wrong_derivation_parent_fail_closed():
    graph = _graph()
    root = root_dataset_record(graph)
    renamed = renamed_dataset_record(root, rename_devices(graph, seed=3))
    with pytest.raises(DatasetError, match="missing parent"):
        validate_graph_dataset((renamed,))

    other = root_dataset_record(
        parse_spice(".subckt top a b\nr1 a b 2k\n.ends\n", top="top", source="other.sp")
    )
    with pytest.raises(DatasetError, match="parent graph"):
        renamed_dataset_record(other, rename_devices(graph, seed=3))
    sequence = encode_graph_sequence(compact_graph(graph), seed=4)
    with pytest.raises(DatasetError, match="parent graph"):
        traversal_dataset_record(other, sequence)


def test_rehashed_child_cannot_forge_a_different_root_lineage():
    root = root_dataset_record(_graph())
    renamed = renamed_dataset_record(root, rename_devices(_graph(), seed=3))
    forged = replace(renamed, root_lineage_id="sha256:" + "0" * 64)
    forged = _rehash_record(forged)
    with pytest.raises(DatasetError, match="preserve its root lineage"):
        validate_graph_dataset((root, forged))


def test_rehashed_bucket_and_derivation_tampering_are_independently_rejected():
    root = root_dataset_record(_graph())
    bad_bucket = replace(root.leakage_bucket, bucket_id="sha256:" + "0" * 64)
    forged_bucket = replace(root, leakage_bucket=bad_bucket)
    forged_bucket = _rehash_record(forged_bucket)
    with pytest.raises(DatasetError, match="leakage bucket"):
        validate_graph_dataset((forged_bucket,))

    renamed = renamed_dataset_record(root, rename_devices(_graph(), seed=3))
    forged_seed = replace(renamed, seed=4)
    forged_seed = _rehash_record(forged_seed)
    with pytest.raises(DatasetError, match="derivation evidence"):
        validate_graph_dataset((root, forged_seed))


def test_bucket_metadata_uses_exact_runtime_types_not_numeric_equality():
    root = root_dataset_record(_graph())
    forged_buckets = (
        replace(root.leakage_bucket, rounds=float(root.leakage_bucket.rounds)),
        replace(root.leakage_bucket, vertex_count=float(root.leakage_bucket.vertex_count)),
        replace(root.leakage_bucket, refinement_truncated=0),
    )
    for bucket in forged_buckets:
        forged = replace(root, leakage_bucket=bucket)
        forged = _rehash_record(forged)
        with pytest.raises(DatasetError, match="leakage bucket"):
            validate_graph_dataset((forged,))


def test_record_metadata_and_root_semantics_are_content_checked():
    root = root_dataset_record(_graph())
    bad_graph = _rehash_record(replace(root, immediate_graph_id="sha256:" + "0" * 64))
    with pytest.raises(DatasetError, match="immediate graph identity"):
        validate_graph_dataset((bad_graph,))

    bad_evidence = _rehash_record(replace(root, immediate_source_evidence_id="sha256:" + "0" * 64))
    with pytest.raises(DatasetError, match="immediate source evidence"):
        validate_graph_dataset((bad_evidence,))

    bad_parent = _rehash_record(replace(root, parent_record_id=root.record_id))
    with pytest.raises(DatasetError, match="root record cannot"):
        validate_graph_dataset((bad_parent,))

    bad_seed = _rehash_record(replace(root, seed=0))
    with pytest.raises(DatasetError, match="derivation evidence"):
        validate_graph_dataset((bad_seed,))

    bad_lineage = _rehash_record(replace(root, root_lineage_id="sha256:" + "0" * 64))
    with pytest.raises(DatasetError, match="root record lineage"):
        validate_graph_dataset((bad_lineage,))


def test_record_kind_payload_and_parent_derivation_cannot_be_relabelled():
    graph = _graph()
    root = root_dataset_record(graph)
    sequence = encode_graph_sequence(compact_graph(graph), seed=3)
    wrong_payload = replace(root, payload=sequence)
    with pytest.raises(DatasetError, match="root record payload"):
        validate_graph_dataset((wrong_payload,))

    wrong_kind = replace(root, kind="root")
    with pytest.raises(DatasetError, match="typed shape"):
        validate_graph_dataset((wrong_kind,))

    renamed = renamed_dataset_record(root, rename_devices(graph, seed=4))
    other = root_dataset_record(
        parse_spice(".subckt top a b\nr1 a b 2k\n.ends\n", top="top", source="other.sp")
    )
    forged_parent = replace(
        renamed,
        parent_record_id=other.record_id,
        root_lineage_id=other.root_lineage_id,
        root_source_evidence_id=other.root_source_evidence_id,
    )
    forged_parent = _rehash_record(forged_parent)
    with pytest.raises(DatasetError, match="evidence does not match its parent"):
        validate_graph_dataset((other, forged_parent))


def test_derivation_depth_is_bounded():
    graph = parse_spice(".subckt top a b\nr1 a b 1k\n.ends\n", top="top")
    records = [root_dataset_record(graph)]
    parent = records[0]
    for seed in range(65):
        sequence = encode_graph_sequence(compact_graph(graph), seed=seed)
        parent = traversal_dataset_record(parent, sequence)
        records.append(parent)
    with pytest.raises(DatasetError, match="ancestry exceeds"):
        validate_graph_dataset(tuple(records))


@pytest.mark.parametrize(
    ("seed", "weights"),
    [
        (True, (1, 1, 1)),
        (-1, (1, 1, 1)),
        (2**64, (1, 1, 1)),
        (0, (0, 0, 0)),
        (0, (1.0, 1, 1)),
        (0, (-1, 1, 1)),
    ],
)
def test_split_seed_and_weights_are_exact_bounded_integers(seed, weights):
    with pytest.raises(DatasetError):
        split_graph_dataset((root_dataset_record(_graph()),), seed=seed, weights=weights)


def test_dataset_container_and_utf8_work_are_bounded_before_validation(monkeypatch):
    root = root_dataset_record(_graph())
    with pytest.raises(DatasetError, match="immutable bounded tuple"):
        validate_graph_dataset([root])
    with pytest.raises(DatasetError, match="record count"):
        validate_graph_dataset((root,) * 10_001)

    graph = _graph()
    forged = replace(graph, sources=("é" * 4_097,))
    with pytest.raises(DatasetError, match="bounded text"):
        root_dataset_record(forged)

    forged = replace(graph, sources=("bad\ud800source",))
    with pytest.raises(DatasetError, match="valid UTF-8"):
        root_dataset_record(forged)

    monkeypatch.setattr(dataset_module, "_MAX_TEXT_BYTES", 1)
    with pytest.raises(DatasetError, match="UTF-8 byte limit"):
        root_dataset_record(graph)


def test_dataset_aggregate_pairs_and_sequence_trails_have_early_limits(monkeypatch):
    root = root_dataset_record(_graph())
    monkeypatch.setattr(dataset_module, "_MAX_DATASET_NESTED_PAIRS", 0)
    with pytest.raises(DatasetError, match="aggregate payload"):
        validate_graph_dataset((root,))

    monkeypatch.setattr(dataset_module, "_MAX_DATASET_NESTED_PAIRS", 4_000_000)
    sequence = encode_graph_sequence(compact_graph(_graph()), seed=5)
    scope = sequence.scopes[0]
    oversized = replace(
        sequence,
        scopes=(replace(scope, trails=(scope.trails[0],) * 500_001), *sequence.scopes[1:]),
    )
    with pytest.raises(DatasetError, match="trail count"):
        traversal_dataset_record(root, oversized)

    trails = sum(len(candidate.trails) for candidate in sequence.scopes)
    steps = sum(len(trail.steps) for candidate in sequence.scopes for trail in candidate.trails)
    assert trails > 0 and steps > 0
    monkeypatch.setattr(dataset_module, "_MAX_EDGES", max(trails, steps))
    with pytest.raises(DatasetError, match="traversal work"):
        dataset_module._sequence_metrics(sequence)


def test_scope_parameter_totals_are_bounded_without_devices_or_sequence_owners(monkeypatch):
    graph = parse_spice(
        ".subckt top a params: gain=1\nr1 a 0 1k\n.ends\n",
        top="top",
    )
    graph_scope = graph.scopes[0]
    parameter_only_graph = replace(graph, scopes=(replace(graph_scope, devices=()),))
    sequence = encode_graph_sequence(compact_graph(graph), seed=0)
    scope = sequence.scopes[0]
    parameter_only = replace(
        sequence,
        scopes=(replace(scope, nets=(), owners=(), trails=()),),
    )

    monkeypatch.setattr(dataset_module, "_MAX_NESTED_PAIRS", 0)
    with pytest.raises(DatasetError, match="graph nested data"):
        dataset_module._graph_metrics(parameter_only_graph)
    with pytest.raises(DatasetError, match="sequence nested data"):
        dataset_module._sequence_metrics(parameter_only)


def test_record_and_split_identity_tampering_is_rejected():
    root = root_dataset_record(_graph())
    with pytest.raises(DatasetError, match="record identity"):
        validate_graph_dataset((replace(root, record_id="sha256:" + "0" * 64),))
    split = split_graph_dataset((root,), seed=1)
    with pytest.raises(DatasetError, match="split identity"):
        stack_dataset_splits(replace(split, split_id="sha256:" + "0" * 64))


def test_duplicate_records_and_incompatible_stacks_are_rejected():
    root = root_dataset_record(_graph())
    with pytest.raises(DatasetError, match="duplicate record"):
        validate_graph_dataset((root, root))
    first = split_graph_dataset((root,), seed=1)
    second = split_graph_dataset((root,), seed=2)
    with pytest.raises(DatasetError, match="identical seed and weights"):
        stack_dataset_splits(first, second)
    with pytest.raises(DatasetError, match="duplicate record"):
        stack_dataset_splits(first, first)


def test_stack_checks_aggregate_count_before_validating_payloads(monkeypatch):
    graph = _graph()
    root = root_dataset_record(graph)
    traversal = traversal_dataset_record(
        root,
        encode_graph_sequence(compact_graph(graph), seed=1),
    )
    split = split_graph_dataset((root, traversal), seed=1)
    calls = 0

    def counted_validate(candidate):
        nonlocal calls
        calls += 1
        return candidate

    monkeypatch.setattr(dataset_module, "_MAX_RECORDS", 2)
    monkeypatch.setattr(dataset_module, "_validate_split", counted_validate)
    with pytest.raises(DatasetError, match="stacked dataset record count"):
        stack_dataset_splits(split, split)
    assert calls == 0


def test_stack_checks_aggregate_payload_before_validating_any_split(monkeypatch):
    left = root_dataset_record(
        parse_spice(".subckt top a b\nr1 a b 1k\n.ends\n", top="top", source="a.sp")
    )
    right = root_dataset_record(
        parse_spice(
            ".subckt top a b\nr1 a mid 1k\nr2 mid b 1k\n.ends\n",
            top="top",
            source="b.sp",
        )
    )
    splits = (
        split_graph_dataset((left,), seed=7),
        split_graph_dataset((right,), seed=7),
    )
    vertex_limit = max(
        dataset_module._payload_metrics(record.payload).vertices for record in (left, right)
    )
    calls = 0

    def counted_validate(candidate):
        nonlocal calls
        calls += 1
        return candidate

    monkeypatch.setattr(dataset_module, "_MAX_DATASET_VERTICES", vertex_limit)
    monkeypatch.setattr(dataset_module, "_validate_split", counted_validate)
    with pytest.raises(DatasetError, match="aggregate payload"):
        stack_dataset_splits(*splits)
    assert calls == 0


def test_validate_dataset_split_accepts_the_canonical_builder_result():
    root = root_dataset_record(_graph())
    split = split_graph_dataset((root,), seed=17, weights=(0, 1, 0))
    assert validate_dataset_split(split) == split
    assert split.validation == (root,)


def test_validate_dataset_split_rejects_mutable_empty_and_noncanonical_partitions():
    graph = _graph()
    root = root_dataset_record(graph)
    traversal = traversal_dataset_record(
        root,
        encode_graph_sequence(compact_graph(graph), seed=2),
    )
    split = split_graph_dataset((root, traversal), seed=3)
    partition = _partition_of(split, root.record_id)
    records = getattr(split, partition.value)
    assert len(records) == 2

    mutable = replace(split, **{partition.value: list(records)})
    with pytest.raises(DatasetError, match="immutable tuples"):
        validate_dataset_split(mutable)

    reversed_partition = replace(split, **{partition.value: tuple(reversed(records))})
    reversed_partition = replace(
        reversed_partition,
        split_id=dataset_module._identity(dataset_module._split_body(reversed_partition)),
    )
    with pytest.raises(DatasetError, match="order is not canonical"):
        validate_dataset_split(reversed_partition)

    empty = dataset_module._build_split(split.seed, split.weights, (), (), ())
    with pytest.raises(DatasetError, match="at least one record"):
        validate_dataset_split(empty)


def test_public_dataset_entry_points_reject_wrong_runtime_shapes():
    with pytest.raises(DatasetError, match="CircuitGraph"):
        root_dataset_record(None)
    with pytest.raises(DatasetError, match="invalid type"):
        validate_dataset_split(None)
    with pytest.raises(DatasetError, match="non-empty"):
        stack_dataset_splits()


def test_valid_corpus_derivations_preserve_bucket_and_exact_lineage():
    circuits = Path(__file__).parents[1] / "examples" / "circuits"
    cases = (
        (circuits / "current_mirror.sp", "current_mirror"),
        (circuits / "diff_pair.sp", "diff_pair"),
        (circuits / "device_families.sp", "device_families"),
        (circuits / "ota.sp", "ota"),
    )
    for path, top in cases:
        graph = load_spice(path, top=top)
        root = root_dataset_record(graph)
        for seed in (0, 1, 2**64 - 1):
            renamed = renamed_dataset_record(root, rename_devices(graph, seed=seed))
            traversed = traversal_dataset_record(
                renamed,
                encode_graph_sequence(compact_graph(renamed.payload.graph), seed=seed),
            )
            assert renamed.leakage_bucket == root.leakage_bucket
            assert traversed.leakage_bucket == root.leakage_bucket
            validate_graph_dataset((traversed, renamed, root))


def test_zero_round_wl_budget_is_explicitly_marked_truncated(monkeypatch):
    monkeypatch.setattr(dataset_module, "_MAX_WL_ROUNDS", 0)
    bucket = conservative_leakage_bucket(_graph())
    assert bucket.rounds == 0
    assert bucket.refinement_truncated is True
