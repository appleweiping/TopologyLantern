from __future__ import annotations

import io
import json
import runpy
from collections import Counter, defaultdict
from dataclasses import replace
from hashlib import sha256
from itertools import product
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from topology_lantern import (
    GraphSequenceError,
    compact_graph,
    connectivity_graph_json,
    decode_graph_sequence,
    encode_graph_sequence,
    graph_sequence_json,
    load_graph_sequence,
    parse_graph_sequence,
    parse_spice,
)
from topology_lantern import graph_sequence as module
from topology_lantern.cli import EXIT_INPUT, EXIT_OK, main
from topology_lantern.graph_codec import CompactCircuitGraph, ConnectivityRepresentationError


def _graph(cards: str = "r1 a b 1k\nr2 b c 2k\nr3 c a 3k\n") -> CompactCircuitGraph:
    return compact_graph(parse_spice(f".subckt cell a b c\n{cards}.ends\n", top="cell"))


def _seal(document: dict[str, object]) -> str:
    body = {key: value for key, value in document.items() if key != "sequence_id"}
    document["sequence_id"] = (
        "sha256:"
        + sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "ascii"
            )
        ).hexdigest()
    )
    return json.dumps(document)


def _minimum_trails(graph: CompactCircuitGraph) -> int:
    """Independent degree/connectivity oracle, without Euler traversal."""
    total = 0
    for scope in graph.scopes:
        neighbors: dict[str, set[str]] = defaultdict(set)
        degree: Counter[str] = Counter()
        for edge in scope.edges:
            neighbors[edge.owner_id].add(edge.net_id)
            neighbors[edge.net_id].add(edge.owner_id)
            degree.update((edge.owner_id, edge.net_id))
        remaining = set(degree)
        while remaining:
            component = {remaining.pop()}
            previous = set()
            while previous != component:
                previous = set(component)
                component.update(other for node in previous for other in neighbors[node])
            remaining.difference_update(component)
            total += max(1, sum(degree[node] % 2 for node in component) // 2)
    return total


def test_minimum_euler_trails_preserve_exact_parallel_terminal_incidence() -> None:
    graph = _graph("m1 a a b b nch w=1u\nr1 a b 1k\nc1 c c 1p\n")
    sequence = encode_graph_sequence(graph)
    assert decode_graph_sequence(sequence) == graph
    assert sum(len(scope.trails) for scope in sequence.scopes) == _minimum_trails(graph)
    original = Counter(edge.edge_id for scope in graph.scopes for edge in scope.edges)
    recovered = Counter(
        step.edge_id for scope in sequence.scopes for trail in scope.trails for step in trail.steps
    )
    assert recovered == original
    assert "edges" not in sequence.scopes[0].as_dict()
    assert parse_graph_sequence(graph_sequence_json(sequence, pretty=True)) == sequence


def test_small_multigraphs_have_minimum_trail_count_and_exact_round_trip() -> None:
    for endpoints in product(("a a", "a b", "b c", "c a"), repeat=3):
        graph = _graph("".join(f"r{i} {nodes} 1k\n" for i, nodes in enumerate(endpoints)))
        for seed in (0, 7):
            sequence = encode_graph_sequence(graph, seed=seed)
            assert decode_graph_sequence(sequence) == graph
            assert sum(len(scope.trails) for scope in sequence.scopes) == _minimum_trails(graph)


def test_traversal_augmentation_is_seeded_lossless_and_reproducible() -> None:
    graph = _graph()
    sequences = [encode_graph_sequence(graph, seed=seed) for seed in range(12)]
    assert len({sequence.sequence_id for sequence in sequences}) == 12
    assert len({repr(sequence.scopes) for sequence in sequences}) > 1
    for sequence in sequences:
        assert sequence == encode_graph_sequence(graph, seed=sequence.seed)
        assert decode_graph_sequence(sequence) == graph
        assert sequence.source_graph_id == graph.source_graph_id


def test_hierarchy_defaults_and_source_metadata_are_reconstructed() -> None:
    graph = compact_graph(
        parse_spice(
            ".global supply\n.subckt leaf p n params: w=1u\nm1 p p n 0 nch w=w\n.ends\n"
            ".subckt top a b\nx1 a b leaf w=2u\nr1 b supply 1k\n.ends\n",
            top="top",
            source="café.sp",
        )
    )
    sequence = encode_graph_sequence(graph, seed=2**64 - 1)
    assert decode_graph_sequence(sequence) == graph
    assert len(sequence.scopes) == 2


def test_even_components_disconnected_components_and_isolated_nets() -> None:
    graph = compact_graph(
        parse_spice(
            ".global unused\nr1 a b 1k\nr2 b c 1k\nr3 c a 1k\nc1 x y 1p\nc2 x y 2p\n.end\n",
        )
    )
    sequence = encode_graph_sequence(graph)
    assert len(sequence.scopes[0].trails) == 2
    assert all(trail.start == trail.steps[-1].target for trail in sequence.scopes[0].trails)
    assert decode_graph_sequence(sequence) == graph


def test_public_schema_resolves_metadata_locally_and_accepts_the_codec() -> None:
    schemas = Path(__file__).parents[1] / "docs" / "schemas"
    graph_schema = json.loads((schemas / "connectivity-graph-1.schema.json").read_text())
    sequence_schema = json.loads((schemas / "euler-sequence-1.schema.json").read_text())
    registry = Registry().with_resource(graph_schema["$id"], Resource.from_contents(graph_schema))
    Draft202012Validator.check_schema(sequence_schema)
    validator = Draft202012Validator(sequence_schema, registry=registry)
    validator.validate(encode_graph_sequence(_graph()).as_dict())


def test_aggregate_step_limit_is_distinct_from_per_trail_limit(monkeypatch) -> None:
    sequence = encode_graph_sequence(_graph())
    text = graph_sequence_json(sequence)
    maximum = max(len(trail.steps) for trail in sequence.scopes[0].trails)
    monkeypatch.setattr(module, "_MAX_STEPS", maximum)
    with pytest.raises(GraphSequenceError, match="total step"):
        parse_graph_sequence(text)


@pytest.mark.parametrize("seed", [True, -1, 2**64, 1.2, "0"])
def test_seed_bounds(seed: object) -> None:
    with pytest.raises(GraphSequenceError, match="64-bit"):
        encode_graph_sequence(_graph(), seed=seed)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["{}", "[]", "{", '{"x":1,"x":2}', "NaN", b"\xff"])
def test_json_failures_are_typed(text: str | bytes) -> None:
    with pytest.raises(GraphSequenceError):
        parse_graph_sequence(text)


def test_sequence_identity_and_public_dataclass_mutation_are_rejected() -> None:
    sequence = encode_graph_sequence(_graph())
    altered = replace(sequence, top="other")
    with pytest.raises(GraphSequenceError, match="identity"):
        decode_graph_sequence(altered)
    with pytest.raises(GraphSequenceError):
        decode_graph_sequence(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "seed",
        "empty",
        "target",
        "ordinal",
        "edge",
        "terminal",
        "missing",
        "duplicate",
        "scope-shape",
        "scopes-array",
        "owners-array",
        "owner-shape",
        "net-shape",
        "start",
        "owner-id",
        "net-id",
        "steps-array",
        "step-shape",
        "extra",
    ],
)
def test_resealed_tampering_cannot_bypass_reconstruction(change: str) -> None:
    raw = json.loads(graph_sequence_json(encode_graph_sequence(_graph())))
    scope = raw["scopes"][0]
    trail = scope["trails"][0]
    step = trail["steps"][0]
    if change == "version":
        raw["version"] = True
    elif change == "seed":
        raw["seed"] = -1
    elif change == "empty":
        trail["steps"] = []
    elif change == "target":
        step["target"] = trail["start"]
    elif change == "ordinal":
        step["ordinal"] = True
    elif change == "edge":
        step["edge_id"] = "wrong"
    elif change == "terminal":
        step["terminal"] = "wrong"
    elif change == "missing":
        scope["trails"].pop()
    elif change == "duplicate":
        scope["trails"].append(trail)
    elif change == "scope-shape":
        scope["bad"] = 1
    elif change == "scopes-array":
        raw["scopes"] = {}
    elif change == "owners-array":
        scope["owners"] = {}
    elif change == "owner-shape":
        scope["owners"][0] = 1
    elif change == "net-shape":
        scope["nets"][0] = 1
    elif change == "start":
        trail["start"] = ""
    elif change == "owner-id":
        scope["owners"][0]["id"] = None
    elif change == "net-id":
        scope["nets"][0]["id"] = None
    elif change == "steps-array":
        trail["steps"] = {}
    elif change == "step-shape":
        step["extra"] = 1
    else:
        raw["extra"] = 1
    with pytest.raises(ConnectivityRepresentationError):
        parse_graph_sequence(_seal(raw))


def test_bounds_apply_to_reads_encoding_and_decoding(tmp_path: Path, monkeypatch) -> None:
    sequence = encode_graph_sequence(_graph())
    path = tmp_path / "graph-sequence.json"
    text = graph_sequence_json(sequence)
    path.write_text(text, encoding="utf-8")
    assert load_graph_sequence(path) == sequence
    assert parse_graph_sequence(text.encode()) == sequence
    with pytest.raises(GraphSequenceError, match="cannot read"):
        load_graph_sequence(tmp_path / "absent")
    monkeypatch.setattr(module, "_MAX_BYTES", 10)
    with pytest.raises(GraphSequenceError, match="bounded"):
        load_graph_sequence(path)
    with pytest.raises(GraphSequenceError, match="byte limit"):
        encode_graph_sequence(_graph())
    monkeypatch.setattr(module, "_MAX_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(module, "_MAX_STEPS", 3)
    with pytest.raises(GraphSequenceError, match=r"bounded array|total step"):
        parse_graph_sequence(text)


def test_cli_round_trip_and_output_input_protection(tmp_path: Path) -> None:
    graph = _graph()
    source = tmp_path / "compact.json"
    source.write_text(connectivity_graph_json(graph), encoding="utf-8")
    sequence = tmp_path / "sequence.json"
    reconstructed = tmp_path / "reconstructed.json"
    assert (
        main(["encode-graph-sequence", str(source), "--seed", "17", "--output", str(sequence)])
        == EXIT_OK
    )
    assert main(["decode-graph-sequence", str(sequence), "--output", str(reconstructed)]) == EXIT_OK
    assert json.loads(reconstructed.read_text()) == graph.as_dict()
    original = sequence.read_bytes()
    errors = io.StringIO()
    assert (
        main(
            ["decode-graph-sequence", str(sequence), "--output", str(sequence), "--force"],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert sequence.read_bytes() == original
    assert "aliases protected input" in errors.getvalue()


def test_large_hierarchical_port_ordinal_round_trip() -> None:
    count = 4_097
    # Hexadecimal net names keep the logical line below the ingest byte ceiling.
    ports = " ".join(f"{index + 1:x}" for index in range(count))
    connected = " ".join("a" for _ in range(count))
    graph = compact_graph(
        parse_spice(
            f".subckt child {ports}\n.ends child\n.subckt top a\nx1 {connected} child\n.ends top\n",
            top="top",
        )
    )
    assert max(edge.ordinal for scope in graph.scopes for edge in scope.edges) == count - 1
    sequence = encode_graph_sequence(graph)
    assert decode_graph_sequence(sequence) == graph
    assert parse_graph_sequence(graph_sequence_json(sequence)) == sequence


def test_decoder_rejects_resealed_nonminimum_trail_cover() -> None:
    raw = encode_graph_sequence(_graph()).as_dict()
    scope = next(scope for scope in raw["scopes"] if scope["trails"])
    trails = scope["trails"]
    index = next(index for index, trail in enumerate(trails) if len(trail["steps"]) > 1)
    trail = trails[index]
    trails[index : index + 1] = [
        {"start": trail["start"], "steps": trail["steps"][:1]},
        {"start": trail["steps"][0]["target"], "steps": trail["steps"][1:]},
    ]
    with pytest.raises(GraphSequenceError, match="minimum trail count"):
        parse_graph_sequence(_seal(raw))


def test_benchmark_covers_real_codec_and_binds_source_and_workload(tmp_path: Path) -> None:
    harness = Path(__file__).parents[1] / "benchmarks" / "graph_sequence.py"
    measure = runpy.run_path(str(harness))["measure"]
    netlist = tmp_path / "original.sp"
    netlist.write_text("r1 a b 1k\nr2 b a 2k\n.end\n", encoding="utf-8")
    result = measure(netlist, repetitions=1)
    assert result["exact_reconstruction"] is True
    assert (result["owners"], result["incidences"], result["trails"]) == (2, 4, 1)
    assert result["harness_sha256"] == sha256(harness.read_bytes()).hexdigest()
    assert len(result["implementation_sha256"]) == 64
    for invalid in (True, 0, 101):
        with pytest.raises(ValueError, match="repetitions"):
            measure(netlist, repetitions=invalid)
    measure_chain = runpy.run_path(str(harness))["measure_chain"]
    chain = measure_chain(10, repetitions=1)
    assert (chain["owners"], chain["incidences"], chain["trails"]) == (10, 20, 1)
    assert chain["exact_reconstruction"] is True
    assert chain["workload"] == "original-resistor-chain"
    assert chain["chain_owners"] == 10
    for invalid in (True, 0, 10_001):
        with pytest.raises(ValueError, match="chain owners"):
            measure_chain(invalid, repetitions=1)
