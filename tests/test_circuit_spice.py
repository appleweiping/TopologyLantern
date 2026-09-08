from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from topology_lantern.circuit import CircuitElementKind, CircuitGraphError, circuit_graph_json
from topology_lantern.spice import IngestLimits, load_spice, parse_spice


def test_root_primitives_are_normalized_typed_and_deterministic() -> None:
    first = parse_spice(
        """
        * every supported primitive family
        Q1 nc nb ne NPN area=2
        D1 nd 0 DIODE_MODEL
        V1 vdd 0 1.8
        I1 tail 0 20u
        C1 out 0 1p
        R1 out 0 10K temp=27
        M1 out in tail 0 NCH W=4U L=180N
        """
    )
    second = parse_spice(
        """
        m1 OUT IN TAIL 0 nch l=180n w=4u
        r1 out 0 10k TEMP=27
        c1 out 0 1P
        i1 tail 0 20U
        v1 vdd 0 1.8
        d1 nd 0 diode_model
        q1 nc nb ne npn AREA=2
        """,
        source="different-name.sp",
    )
    assert first.graph_id == second.graph_id
    assert first.top == "__root__"
    assert first.sources == ("<memory>",)
    scope = first.scopes[0]
    assert [device.kind for device in scope.devices] == [
        CircuitElementKind.CAPACITOR,
        CircuitElementKind.DIODE,
        CircuitElementKind.CURRENT_SOURCE,
        CircuitElementKind.MOSFET,
        CircuitElementKind.BJT,
        CircuitElementKind.RESISTOR,
        CircuitElementKind.VOLTAGE_SOURCE,
    ]
    assert scope.device_map()["m1"].terminal_map().keys() == {"d", "g", "s", "b"}
    assert scope.net_map()["0"].is_global is True
    assert scope.net_map()["out"].is_global is False
    assert first.node_ids() >= first.device_ids() | first.net_ids()
    assert json.loads(circuit_graph_json(first)) == first.as_dict()
    assert '\n  "' in circuit_graph_json(first, pretty=True)


def test_hierarchy_closure_connections_ports_and_global_nets() -> None:
    graph = parse_spice(
        """
        .global supply
        .subckt leaf p n PARAMS: gain=1
        rleaf p 0 1k
        rsupply p supply 2k
        .ends leaf
        .subckt unused p n
        runused p n 9k
        .ends unused
        .subckt top p n
        xtwo p n leaf gain=2
        rground n 0 3k
        rsupply n supply 4k
        .ends top
        """,
        top="TOP",
    )
    assert [scope.name for scope in graph.scopes] == ["leaf", "top"]
    leaf, top = graph.scopes
    assert [port.index for port in leaf.ports] == [0, 1]
    assert dict(top.instances[0].connections) == {
        "p": top.net_map()["p"].node_id,
        "n": top.net_map()["n"].node_id,
    }
    assert top.instances[0].reference_scope_id == leaf.scope_id
    assert dict(top.instances[0].parameters) == {"gain": "2"}
    assert dict(top.instances[0].effective_parameters) == {"gain": "2"}
    assert leaf.net_map()["0"].node_id == top.net_map()["0"].node_id
    assert leaf.net_map()["supply"].node_id == top.net_map()["supply"].node_id
    assert leaf.net_map()["supply"].is_global


def test_subcircuit_defaults_are_preserved_in_semantic_identity() -> None:
    first = parse_spice(".subckt gain p n ratio=2\nr1 p n 1k\n.ends")
    second = parse_spice(".subckt gain p n ratio=3\nr1 p n 1k\n.ends")
    assert dict(first.scopes[0].parameters) == {"ratio": "2"}
    assert first.graph_id != second.graph_id


def test_case_insensitive_params_defaults_and_instance_overrides() -> None:
    graph = parse_spice(
        ".subckt leaf p n PaRaMs: ratio=2 bias=1\n"
        "r1 p n 1k\n"
        ".ends\n"
        ".subckt top p n\n"
        "xdefault p n leaf\n"
        "xoverride p n leaf pArAmS: ratio=3\n"
        ".ends",
        top="top",
        source="parameters.sp",
    )
    leaf, top = graph.scopes
    assert dict(leaf.parameters) == {"bias": "1", "ratio": "2"}
    default, override = top.instances
    assert dict(default.parameters) == {}
    assert dict(default.effective_parameters) == {"bias": "1", "ratio": "2"}
    assert dict(override.parameters) == {"ratio": "3"}
    assert dict(override.effective_parameters) == {"bias": "1", "ratio": "3"}
    assert override.source.file == "parameters.sp"
    assert override.source.line == 6

    equivalent = parse_spice(
        ".subckt LEAF P N params: BIAS=1 RATIO=2\n"
        "R1 P N 1K\n"
        ".ends\n"
        ".subckt TOP P N\n"
        "XDEFAULT P N LEAF\n"
        "XOVERRIDE P N LEAF PARAMS: RATIO=3\n"
        ".ends",
        top="TOP",
        source="other.sp",
    )
    assert graph.graph_id == equivalent.graph_id


def test_undeclared_instance_parameters_are_rejected_not_treated_as_ports() -> None:
    with pytest.raises(CircuitGraphError, match="overrides undeclared parameter"):
        parse_spice(
            ".subckt leaf p n\nr1 p n 1k\n.ends\n"
            ".subckt top p n\nx1 p n leaf PARAMS: gain=2\n.ends",
            top="top",
        )


@pytest.mark.parametrize(
    "text",
    [
        ".subckt a p PARAMS:\nr1 p 0 1k\n.ends",
        ".subckt a p gain=1 PARAMS: ratio=2\nr1 p 0 1k\n.ends",
        ".subckt leaf p PARAMS: gain=1\nr1 p 0 1k\n.ends\n.subckt top p\nx1 p leaf PARAMS:\n.ends",
    ],
)
def test_params_marker_shape_is_strict(text: str) -> None:
    with pytest.raises(CircuitGraphError, match="PARAMS:"):
        parse_spice(text, top="a" if ".subckt a" in text else "top")


def test_file_loader_expands_relative_includes_once(tmp_path: Path) -> None:
    blocks = tmp_path / "blocks.sp"
    blocks.write_text(".subckt leaf p n\nr1 p n 1k\n.ends\n", encoding="utf-8")
    entry = tmp_path / "top.sp"
    entry.write_text(
        '.include "blocks.sp"\n.include blocks.sp\n.subckt top p n\nx1 p n leaf\n.ends\n',
        encoding="utf-8",
    )
    graph = load_spice(entry)
    assert graph.sources == ("top.sp", "blocks.sp")
    assert [scope.name for scope in graph.scopes] == ["leaf", "top"]
    assert graph.scopes[0].devices[0].source.file == "blocks.sp"
    assert graph.scopes[0].devices[0].source.line == 2
    assert graph.scopes[1].instances[0].source.file == "top.sp"
    assert graph.scopes[1].instances[0].source.line == 4


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no circuit elements"),
        ("+ orphan", "orphan continuation"),
        (".noise x", "unsupported directive"),
        (".global", "requires at least one net"),
        (".ends", "no open subcircuit"),
        (".subckt a p\n.subckt b p", "nested .subckt"),
        (".subckt a p\nr1 p 0 1k", "no matching .ends"),
        (".subckt a p\n.ends b", "does not match"),
        (".subckt a p\n.ends a extra", "at most one name"),
        (".subckt a p p\nr1 p 0 1k\n.ends", "ports must be"),
        (".subckt a p\nr1 p 0 1k\n.ends\n.subckt a p\nr2 p 0 1k\n.ends", "duplicate subcircuit"),
        ("r1 a b 1k\nr1 a b 2k", "duplicate element"),
        ("e1 a b 1", "unsupported element family"),
        ("m1 d g s", "MOSFET requires"),
        ("r1 p", "two-terminal element requires"),
        ("d1 a c", "diode requires"),
        ("q1 c b e", "BJT requires"),
        ("r1 a b 1k bare", "name=value"),
        ("r1 a b 1k value=2k", "repeats parameter"),
        ("x1 only_reference", "requires nets"),
        ("r bad", "two-terminal element requires"),
        ("r/1 a b 1k", "unsupported characters"),
    ],
)
def test_malformed_spice_is_rejected(text: str, message: str) -> None:
    with pytest.raises(CircuitGraphError, match=message):
        parse_spice(text)


def test_top_selection_reference_and_arity_errors() -> None:
    ambiguous = ".subckt a p\nr1 p 0 1k\n.ends\n.subckt b p\nr2 p 0 1k\n.ends"
    with pytest.raises(CircuitGraphError, match="ambiguous"):
        parse_spice(ambiguous)
    with pytest.raises(CircuitGraphError, match="unknown top"):
        parse_spice(ambiguous, top="missing")
    with pytest.raises(CircuitGraphError, match="undefined"):
        parse_spice(".subckt top p\nx1 p missing\n.ends")
    with pytest.raises(CircuitGraphError, match="recursive"):
        parse_spice(".subckt top p\nx1 p top\n.ends", top="top")
    wrong_arity = ".subckt leaf p n\nr1 p n 1k\n.ends\n.subckt top p\nx1 p leaf\n.ends"
    with pytest.raises(CircuitGraphError, match="passes 1 nets"):
        parse_spice(wrong_arity)


def test_continuations_and_metadata_directives() -> None:
    graph = parse_spice(
        ".model nch nmos\n.option brief\n.param w=1u\n.temp 27\n"
        "m1 d g s 0 nch\n+ w=1u\n+ l=180n\n.end"
    )
    assert dict(graph.scopes[0].devices[0].parameters) == {"l": "180n", "w": "1u"}


def test_end_is_terminal_and_only_valid_in_the_entry_source(tmp_path: Path) -> None:
    assert parse_spice("r1 a b 1k\n.end\n* trailing comment").scopes[0].devices
    for text, message in (
        ("r1 a b 1k\n.end extra", ".end does not accept arguments"),
        ("r1 a b 1k\n.end\nr2 b c 2k", "content after .end"),
    ):
        with pytest.raises(CircuitGraphError, match=message):
            parse_spice(text)

    included = tmp_path / "included.sp"
    included.write_text("r1 a b 1k\n.end\n", encoding="utf-8")
    entry = tmp_path / "entry.sp"
    entry.write_text(".include included.sp\n.end\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match=r"included files cannot contain \.end"):
        load_spice(entry)

    included.write_text("r1 a b 1k\n", encoding="utf-8")
    entry.write_text(".include included.sp\n.end\n.include included.sp\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match=r"content after \.end"):
        load_spice(entry)


def test_identifiers_are_nfc_normalized_and_controls_are_rejected() -> None:
    composed = parse_spice("r\u00e9s n\u00e9t 0 1k")
    decomposed = parse_spice("re\u0301s ne\u0301t 0 1k")
    assert composed.graph_id == decomposed.graph_id
    assert composed.scopes[0].devices[0].name == "r\u00e9s"
    for unsafe in ("r1 a\x07b 0 1k", "r1 a\u202eb 0 1k"):
        with pytest.raises(CircuitGraphError, match="control"):
            parse_spice(unsafe)


@pytest.mark.parametrize(
    "limits",
    [
        IngestLimits(max_files=0),
        IngestLimits(max_file_bytes=0),
        IngestLimits(max_total_bytes=True),
        IngestLimits(max_lines=1.5),  # type: ignore[arg-type]
    ],
)
def test_limits_must_be_positive_integers(limits: IngestLimits) -> None:
    with pytest.raises(CircuitGraphError, match="positive integer"):
        parse_spice("r1 a b 1k", limits=limits)


def test_ingest_limits_can_only_tighten_built_in_safety_ceilings() -> None:
    with pytest.raises(CircuitGraphError, match="safety ceiling"):
        parse_spice("r1 a b 1k", limits=IngestLimits(max_files=33))


def test_in_memory_resource_limits_are_enforced() -> None:
    with pytest.raises(CircuitGraphError, match="source label"):
        parse_spice("r1 a b 1k", source="")
    with pytest.raises(CircuitGraphError, match="source label"):
        parse_spice("r1 a b 1k", source="two\nlines")
    normalized_source = parse_spice("r1 a b 1k", source="cafe\u0301 source.sp")
    assert normalized_source.sources == ("caf\u00e9 source.sp",)
    for unsafe_source in (
        "x" * 4_097,
        "\ud800",
        "right\u202ereordered",
        "isolated\u2066text",
        "two\u2028lines",
    ):
        with pytest.raises(CircuitGraphError, match="source label"):
            parse_spice("r1 a b 1k", source=unsafe_source)

    class EncodeMustNotRun(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("oversized input was encoded before its character lower bound")

    with pytest.raises(CircuitGraphError, match="per-file byte"):
        parse_spice(
            EncodeMustNotRun("r" * 9),
            limits=IngestLimits(max_file_bytes=8, max_total_bytes=16),
        )
    with pytest.raises(CircuitGraphError, match="total byte"):
        parse_spice("r1 a b 1k", limits=IngestLimits(max_total_bytes=4))
    with pytest.raises(CircuitGraphError, match="line length"):
        parse_spice("r1 a b 1k", limits=IngestLimits(max_line_characters=4))
    with pytest.raises(CircuitGraphError, match="logical-line"):
        parse_spice("r1 a b 1k\nr2 b c 2k", limits=IngestLimits(max_lines=1))
    with pytest.raises(CircuitGraphError, match="token"):
        parse_spice("rtoolong a b 1k", limits=IngestLimits(max_token_characters=3))
    with pytest.raises(CircuitGraphError, match="logical-line length"):
        parse_spice(
            "r1 a b 1\n+ 234567",
            limits=IngestLimits(max_line_characters=10),
        )
    with pytest.raises(CircuitGraphError, match="subcircuit limit"):
        parse_spice(
            ".subckt a p\nr1 p 0 1k\n.ends\n.subckt b p\nr2 p 0 1k\n.ends",
            top="a",
            limits=IngestLimits(max_subcircuits=1),
        )
    with pytest.raises(CircuitGraphError, match="element limit"):
        parse_spice("r1 a b 1k\nr2 b c 1k", limits=IngestLimits(max_elements=1))
    with pytest.raises(CircuitGraphError, match="hierarchy-depth"):
        parse_spice(
            ".subckt leaf p\nr1 p 0 1k\n.ends\n.subckt top p\nx1 p leaf\n.ends",
            limits=IngestLimits(max_hierarchy_depth=1),
        )
    with pytest.raises(CircuitGraphError, match="cannot resolve"):
        parse_spice(".include blocks.sp\nr1 a b 1k")
    with pytest.raises(CircuitGraphError, match="UTF-8"):
        parse_spice("\ud800")


def test_file_resource_and_path_boundaries(tmp_path: Path) -> None:
    child = tmp_path / "child.sp"
    child.write_text("r1 a b 1k\n", encoding="utf-8")
    entry = tmp_path / "entry.sp"
    entry.write_text(".include child.sp\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match="included-file limit"):
        load_spice(entry, limits=IngestLimits(max_files=1))
    with pytest.raises(CircuitGraphError, match="total byte"):
        load_spice(entry, limits=IngestLimits(max_total_bytes=4))
    with pytest.raises(CircuitGraphError, match="logical-line"):
        load_spice(entry, limits=IngestLimits(max_lines=1))

    entry.write_text(".include ../outside.sp\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match="entry directory"):
        load_spice(entry)
    entry.write_text(f".include {child.resolve()}\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match="entry directory"):
        load_spice(entry)
    entry.write_text(".include one two\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match="exactly one path"):
        load_spice(entry)
    with pytest.raises(CircuitGraphError, match="cannot read"):
        load_spice(tmp_path / "missing.sp")

    invalid = tmp_path / "invalid.sp"
    invalid.write_bytes(b"\xff")
    with pytest.raises(CircuitGraphError, match="not UTF-8"):
        load_spice(invalid)


def test_file_and_cumulative_include_byte_limits_are_independent(tmp_path: Path) -> None:
    single = tmp_path / "single.sp"
    single_payload = b"r1 a b 123456789\n"
    single.write_bytes(single_payload)
    with pytest.raises(CircuitGraphError, match="per-file byte limit"):
        load_spice(
            single,
            limits=IngestLimits(
                max_file_bytes=len(single_payload) - 1,
                max_total_bytes=len(single_payload) * 2,
            ),
        )

    child = tmp_path / "child.sp"
    child_payload = b"r1 a b 123456789\n"
    child.write_bytes(child_payload)
    entry = tmp_path / "entry.sp"
    entry_payload = b".include child.sp\n"
    entry.write_bytes(entry_payload)
    with pytest.raises(CircuitGraphError, match="total byte limit across files"):
        load_spice(
            entry,
            limits=IngestLimits(
                max_file_bytes=max(len(entry_payload), len(child_payload)),
                max_total_bytes=len(entry_payload) + len(child_payload) - 1,
            ),
        )


def test_recursive_include_is_rejected(tmp_path: Path) -> None:
    left = tmp_path / "left.sp"
    right = tmp_path / "right.sp"
    left.write_text(".include right.sp\n", encoding="utf-8")
    right.write_text(".include left.sp\n", encoding="utf-8")
    with pytest.raises(CircuitGraphError, match="recursive include"):
        load_spice(left)


def test_ota_matches_checked_in_golden_graph() -> None:
    repository = Path(__file__).parents[1]
    graph = load_spice(repository / "examples" / "circuits" / "ota.sp", top="ota")
    golden = json.loads(
        (repository / "tests" / "golden" / "ota-graph-v1.json").read_text(encoding="utf-8")
    )
    assert graph.as_dict() == golden
    schema = json.loads(
        (repository / "docs" / "schemas" / "circuit-graph-1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(golden)


def test_checked_in_clean_room_and_adversarial_corpus() -> None:
    repository = Path(__file__).parents[1]
    corpus = repository / "examples" / "circuits"
    families = load_spice(corpus / "device_families.sp")
    assert {device.kind for device in families.scopes[0].devices} == set(CircuitElementKind)
    rejected = {
        "recursive_subckt.sp": ("recursive subcircuit reference", "recursive"),
        "content_after_end.sp": (r"content after \.end", None),
        "unknown_override.sp": ("overrides undeclared parameter", None),
    }
    for name, (message, top) in rejected.items():
        with pytest.raises(CircuitGraphError, match=message):
            load_spice(corpus / "adversarial" / name, top=top)
