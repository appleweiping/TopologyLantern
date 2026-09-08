from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import topology_lantern.layout as layout_module
from topology_lantern.layout import (
    ConstraintOrigin,
    InferenceLimits,
    LayoutConstraint,
    LayoutConstraintError,
    LayoutConstraintKind,
    LayoutConstraintSet,
    LayoutInferenceReport,
    infer_layout_constraints,
    layout_constraints_json,
    layout_inference_report,
    layout_report_json,
    load_layout_constraints,
    parse_layout_constraints,
    validate_layout_conflicts,
)
from topology_lantern.spice import parse_spice


def graph_with_four_devices():
    return parse_spice(
        """
        .subckt quad a b c d vss
        m1 c a vss vss nch
        m2 d b vss vss nch
        r1 c vss 1k
        r2 d vss 1k
        .ends
        """
    )


def document(graph_id: str, constraints: list[object]) -> dict[str, object]:
    return {
        "schema": "org.topology-lantern.layout-constraints",
        "version": 1,
        "graph_id": graph_id,
        "constraints": constraints,
    }


def test_all_constraint_families_bind_to_graph_and_round_trip() -> None:
    graph = graph_with_four_devices()
    scope = graph.scopes[0]
    devices = [device.node_id for device in scope.devices]
    net = scope.nets[0].node_id
    raw = [
        {"id": "sym", "kind": "symmetry", "subjects": devices[:2], "axis": "vertical"},
        {
            "id": "centroid",
            "kind": "common_centroid",
            "groups": [[devices[0]], [devices[1]]],
            "axis": "both",
        },
        {
            "id": "match",
            "kind": "matching",
            "subjects": devices[:2],
            "tolerance": 0.01,
        },
        {"id": "align", "kind": "alignment", "subjects": devices[2:], "axis": "x"},
        {
            "id": "ordered",
            "kind": "order",
            "subjects": [devices[0], devices[2]],
            "direction": "left_to_right",
        },
        {"id": "guard", "kind": "keepout", "subjects": [devices[3]], "margin": 2},
        {"id": "critical", "kind": "net_priority", "subjects": [net], "priority": 90},
    ]
    parsed = parse_layout_constraints(document(graph.graph_id, raw), graph)
    assert parsed.graph_id == graph.graph_id
    assert [item.kind for item in parsed.constraints] == list(LayoutConstraintKind)
    assert all(item.origin is ConstraintOrigin.USER for item in parsed.constraints)
    assert [item.as_dict()["id"] for item in parsed.constraints] == [
        "sym",
        "centroid",
        "match",
        "align",
        "ordered",
        "guard",
        "critical",
    ]
    repository = Path(__file__).parents[1]
    schema = json.loads(
        (repository / "docs" / "schemas" / "layout-constraints-1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(document(graph.graph_id, raw))
    assert json.loads(layout_constraints_json(parsed)) == document(graph.graph_id, raw)
    assert '\n  "' in layout_constraints_json(parsed, pretty=True)


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "wrong"},
        {"version": 2},
        {"version": True},
        {"version": "1"},
        {"graph_id": "sha256:wrong"},
        {"constraints": {}},
        {"extra": 1},
    ],
)
def test_document_contract_is_strict(mutation: dict[str, object]) -> None:
    graph = graph_with_four_devices()
    raw = document(graph.graph_id, [])
    raw.update(mutation)
    with pytest.raises(LayoutConstraintError):
        parse_layout_constraints(raw, graph)


def test_constraint_count_is_bounded() -> None:
    graph = graph_with_four_devices()
    with pytest.raises(LayoutConstraintError, match="constraint limit"):
        parse_layout_constraints(document(graph.graph_id, [None] * 10_001), graph)


def test_total_graph_references_are_bounded() -> None:
    graph = graph_with_four_devices()
    oversized = [next(iter(graph.device_ids()))] * 100_001
    with pytest.raises(LayoutConstraintError, match="graph-reference limit"):
        parse_layout_constraints(
            document(
                graph.graph_id,
                [{"id": "huge", "kind": "keepout", "subjects": oversized, "margin": 1}],
            ),
            graph,
        )


@pytest.mark.parametrize(
    "constraint",
    [
        None,
        {"id": "bad id", "kind": "keepout", "subjects": [], "margin": 1},
        {"id": "x", "kind": "unknown", "subjects": []},
        {"id": "x", "kind": "symmetry", "subjects": [], "axis": "vertical"},
        {"id": "x", "kind": "symmetry", "subjects": [1, 2], "axis": "vertical"},
        {"id": "x", "kind": "symmetry", "subjects": ["missing", "other"], "axis": "vertical"},
        {"id": "x", "kind": "symmetry", "subjects": ["same", "same"], "axis": "vertical"},
        {"id": "x", "kind": "symmetry", "subjects": [], "axis": "diagonal", "extra": 1},
        {"id": "x", "kind": "common_centroid", "groups": {}, "axis": "both"},
        {"id": "x", "kind": "matching", "subjects": [], "tolerance": True},
        {"id": "x", "kind": "matching", "subjects": [], "tolerance": -1},
        {"id": "x", "kind": "matching", "subjects": [], "tolerance": float("inf")},
        {"id": "x", "kind": "alignment", "subjects": [], "axis": "z"},
        {"id": "x", "kind": "order", "subjects": [], "direction": "above"},
        {"id": "x", "kind": "keepout", "subjects": [], "margin": -1},
        {"id": "x", "kind": "net_priority", "subjects": [], "priority": True},
        {"id": "x", "kind": "net_priority", "subjects": [], "priority": 101},
    ],
)
def test_invalid_constraint_shapes_are_rejected(constraint: object) -> None:
    graph = graph_with_four_devices()
    with pytest.raises(LayoutConstraintError):
        parse_layout_constraints(document(graph.graph_id, [constraint]), graph)


def test_common_centroid_groups_must_be_balanced_disjoint_and_known() -> None:
    graph = graph_with_four_devices()
    ids = list(graph.device_ids())
    variants = [
        [[ids[0]], [ids[0]]],
        [[ids[0]], [ids[1], ids[2]]],
        [[ids[0]], ["tlg-device-00000000000000000000"]],
    ]
    for groups in variants:
        with pytest.raises(LayoutConstraintError):
            parse_layout_constraints(
                document(
                    graph.graph_id,
                    [{"id": "cc", "kind": "common_centroid", "groups": groups, "axis": "both"}],
                ),
                graph,
            )


def test_placement_constraint_cannot_cross_definition_scopes() -> None:
    graph = parse_spice(
        ".subckt leaf p n\nrleaf p n 1k\n.ends\n"
        ".subckt top p n\nxleaf p n leaf\nrtop p n 2k\n.ends",
        top="top",
    )
    subjects = [scope.devices[0].node_id for scope in graph.scopes]
    with pytest.raises(LayoutConstraintError, match="one definition scope"):
        parse_layout_constraints(
            document(
                graph.graph_id,
                [{"id": "cross", "kind": "matching", "subjects": subjects, "tolerance": 0}],
            ),
            graph,
        )


def test_kind_specific_values_are_validated_after_node_binding() -> None:
    graph = graph_with_four_devices()
    a, b = sorted(graph.device_ids())[:2]
    net = next(iter(graph.net_ids()))
    invalid = [
        {"id": "x", "kind": "symmetry", "subjects": [a, b], "axis": "diagonal"},
        {"id": "x", "kind": "matching", "subjects": [a, b], "tolerance": True},
        {"id": "x", "kind": "matching", "subjects": [a, b], "tolerance": -1},
        {"id": "x", "kind": "matching", "subjects": [a, b], "tolerance": float("inf")},
        {"id": "x", "kind": "alignment", "subjects": [a, b], "axis": "z"},
        {"id": "x", "kind": "order", "subjects": [a, b], "direction": "above"},
        {"id": "x", "kind": "keepout", "subjects": [a], "margin": -1},
        {"id": "x", "kind": "net_priority", "subjects": [net], "priority": True},
        {"id": "x", "kind": "net_priority", "subjects": [net], "priority": 101},
    ]
    for constraint in invalid:
        with pytest.raises(LayoutConstraintError):
            parse_layout_constraints(document(graph.graph_id, [constraint]), graph)


def test_conflict_detector_rejects_duplicates_axes_priorities_and_cycles() -> None:
    a, b = sorted(graph_with_four_devices().device_ids())[:2]
    net = next(iter(graph_with_four_devices().net_ids()))
    base = LayoutConstraint("one", LayoutConstraintKind.KEEPOUT, subjects=(a,), margin=1)
    with pytest.raises(LayoutConstraintError, match="duplicate constraint ID"):
        validate_layout_conflicts((base, base))
    with pytest.raises(LayoutConstraintError, match="duplicate semantic"):
        validate_layout_conflicts(
            (base, LayoutConstraint("two", LayoutConstraintKind.KEEPOUT, subjects=(a,), margin=1))
        )
    with pytest.raises(LayoutConstraintError, match="symmetry"):
        validate_layout_conflicts(
            (
                LayoutConstraint("s1", LayoutConstraintKind.SYMMETRY, (a, b), axis="vertical"),
                LayoutConstraint("s2", LayoutConstraintKind.SYMMETRY, (b, a), axis="horizontal"),
            )
        )
    with pytest.raises(LayoutConstraintError, match="alignment"):
        validate_layout_conflicts(
            (
                LayoutConstraint("a1", LayoutConstraintKind.ALIGNMENT, (a, b), axis="x"),
                LayoutConstraint("a2", LayoutConstraintKind.ALIGNMENT, (b, a), axis="y"),
            )
        )
    with pytest.raises(LayoutConstraintError, match="conflicting priorities"):
        validate_layout_conflicts(
            (
                LayoutConstraint("p1", LayoutConstraintKind.NET_PRIORITY, (net,), priority=1),
                LayoutConstraint("p2", LayoutConstraintKind.NET_PRIORITY, (net,), priority=2),
            )
        )
    with pytest.raises(LayoutConstraintError, match="has no priority"):
        validate_layout_conflicts(
            (LayoutConstraint("p", LayoutConstraintKind.NET_PRIORITY, (net,)),)
        )
    with pytest.raises(LayoutConstraintError, match="cycle"):
        validate_layout_conflicts(
            (
                LayoutConstraint(
                    "o1", LayoutConstraintKind.ORDER, (a, b), direction="left_to_right"
                ),
                LayoutConstraint(
                    "o2", LayoutConstraintKind.ORDER, (a, b), direction="right_to_left"
                ),
            )
        )


def test_reverse_order_spelling_is_the_same_semantic_constraint() -> None:
    a, b, c = sorted(graph_with_four_devices().device_ids())[:3]
    with pytest.raises(LayoutConstraintError, match="duplicate semantic"):
        validate_layout_conflicts(
            (
                LayoutConstraint(
                    "forward",
                    LayoutConstraintKind.ORDER,
                    (a, b, c),
                    direction="left_to_right",
                ),
                LayoutConstraint(
                    "reverse",
                    LayoutConstraintKind.ORDER,
                    (c, b, a),
                    direction="right_to_left",
                ),
            )
        )


@pytest.mark.parametrize(
    ("direction", "axis"),
    [
        ("left_to_right", "x"),
        ("right_to_left", "x"),
        ("bottom_to_top", "y"),
        ("top_to_bottom", "y"),
    ],
)
def test_order_conflicts_with_equal_coordinate_alignment(direction: str, axis: str) -> None:
    a, b = sorted(graph_with_four_devices().device_ids())[:2]
    with pytest.raises(LayoutConstraintError, match="ordering conflicts"):
        validate_layout_conflicts(
            (
                LayoutConstraint("align", LayoutConstraintKind.ALIGNMENT, (a, b), axis=axis),
                LayoutConstraint("order", LayoutConstraintKind.ORDER, (a, b), direction=direction),
            )
        )


def test_transitive_order_conflicts_with_alignment_equivalence() -> None:
    a, b, c = sorted(graph_with_four_devices().device_ids())[:3]
    with pytest.raises(LayoutConstraintError, match="ordering conflicts"):
        validate_layout_conflicts(
            (
                LayoutConstraint("align", LayoutConstraintKind.ALIGNMENT, (a, c), axis="x"),
                LayoutConstraint(
                    "order", LayoutConstraintKind.ORDER, (a, b, c), direction="left_to_right"
                ),
            )
        )


def test_inference_is_deterministic_conservative_and_evidence_labelled() -> None:
    graph = parse_spice(
        """
        .subckt blocks in1 in2 out1 out2 tail vss ref mirrorout vdd
        ma1 out1 in1 tail vss nch
        ma2 out1 in1 tail vss nch
        mb1 out2 in2 tail vss nch
        mb2 out2 in2 tail vss nch
        mref ref ref vdd vdd pch
        mout mirrorout ref vdd vdd pch
        .ends
        """
    )
    inferred = infer_layout_constraints(graph)
    assert inferred == infer_layout_constraints(graph)
    kinds = {item.kind for item in inferred}
    assert {
        LayoutConstraintKind.MATCHING,
        LayoutConstraintKind.SYMMETRY,
        LayoutConstraintKind.COMMON_CENTROID,
    } <= kinds
    assert all(item.origin is ConstraintOrigin.INFERRED for item in inferred)
    assert all(item.confidence is not None and 0 <= item.confidence <= 1 for item in inferred)
    assert all(
        item.evidence and item.constraint_id.startswith("tlc-inferred-") for item in inferred
    )
    assert infer_layout_constraints(parse_spice("r1 a b 1k")) == ()
    unequal = parse_spice(
        ".subckt ratio in1 in2 out1 out2 tail vss\n"
        "m1 out1 in1 tail vss nch w=1u\n"
        "m2 out2 in2 tail vss nch w=2u\n.ends"
    )
    assert infer_layout_constraints(unequal) == ()


def test_inference_limits_fail_before_constraint_construction(monkeypatch) -> None:
    many_devices = graph_with_four_devices()
    with pytest.raises(LayoutConstraintError, match="inference-device limit"):
        infer_layout_constraints(
            many_devices,
            limits=InferenceLimits(max_inference_devices=3),
        )

    three_mos = parse_spice(
        ".subckt trio a b c o1 o2 o3 tail vss\n"
        "m1 o1 a tail vss nch\n"
        "m2 o2 b tail vss nch\n"
        "m3 o3 c tail vss nch\n"
        ".ends"
    )
    with pytest.raises(LayoutConstraintError, match="pair-evaluation limit"):
        infer_layout_constraints(
            three_mos,
            limits=InferenceLimits(max_pair_evaluations=2),
        )

    differential_pair = parse_spice(
        ".subckt pair inp inn outp outn tail vss\n"
        "m1 outp inp tail vss nch\n"
        "m2 outn inn tail vss nch\n"
        ".ends"
    )
    calls = 0

    def fail_if_constructed(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("inferred object was constructed before the cap check")

    monkeypatch.setattr(layout_module, "_candidate", fail_if_constructed)
    with pytest.raises(LayoutConstraintError, match="inferred-constraint limit"):
        infer_layout_constraints(
            differential_pair,
            limits=InferenceLimits(max_inferred_constraints=1),
        )
    assert calls == 0


@pytest.mark.parametrize(
    "limits",
    [
        InferenceLimits(max_inference_devices=0),
        InferenceLimits(max_pair_evaluations=True),
        InferenceLimits(max_inferred_constraints=1.5),  # type: ignore[arg-type]
    ],
)
def test_inference_limits_must_be_positive_integers(limits: InferenceLimits) -> None:
    with pytest.raises(LayoutConstraintError, match="positive integer"):
        infer_layout_constraints(graph_with_four_devices(), limits=limits)


def test_inference_limits_can_only_tighten_safety_ceilings() -> None:
    with pytest.raises(LayoutConstraintError, match="safety ceiling"):
        infer_layout_constraints(
            graph_with_four_devices(),
            limits=InferenceLimits(max_pair_evaluations=100_001),
        )


def test_report_keeps_user_and_inferred_constraints_separate() -> None:
    graph = parse_spice(
        ".subckt mirror ref out vdd\nmref ref ref vdd vdd pch\nmout out ref vdd vdd pch\n.ends"
    )
    ids = sorted(graph.device_ids())
    parsed = parse_layout_constraints(
        document(
            graph.graph_id,
            [{"id": "declared", "kind": "matching", "subjects": ids, "tolerance": 0}],
        ),
        graph,
    )
    report = layout_inference_report(graph, parsed)
    payload = json.loads(layout_report_json(report, pretty=True))
    assert payload["user_constraints"][0]["origin"] == "user"
    assert payload["inferred_constraints"][0]["origin"] == "inferred"
    assert "not user requirements" in payload["disclaimer"]
    repository = Path(__file__).parents[1]
    schema = json.loads(
        (repository / "docs" / "schemas" / "layout-constraint-report-1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(payload)
    with pytest.raises(LayoutConstraintError, match="do not belong"):
        layout_inference_report(graph, LayoutConstraintSet("wrong", ()))
    with pytest.raises(LayoutConstraintError, match="non-user origin"):
        inferred_as_declared = LayoutConstraint(
            "bad-origin",
            LayoutConstraintKind.MATCHING,
            tuple(ids),
            origin=ConstraintOrigin.INFERRED,
            confidence=1,
            evidence=("manual",),
        )
        layout_inference_report(
            graph,
            LayoutConstraintSet(graph.graph_id, (inferred_as_declared,)),
        )
    with pytest.raises(LayoutConstraintError, match="only user"):
        inferred_as_declared.as_declaration_dict()


@pytest.mark.parametrize(
    "bad_constraint",
    [
        LayoutConstraint(
            "unknown-node",
            LayoutConstraintKind.MATCHING,
            subjects=("tlg-device-00000000000000000000", "tlg-device-11111111111111111111"),
            tolerance=0,
        ),
        LayoutConstraint(
            "not-finite",
            LayoutConstraintKind.MATCHING,
            subjects=(),
            tolerance=float("nan"),
        ),
        LayoutConstraint(
            "wrong-shape",
            LayoutConstraintKind.SYMMETRY,
            subjects=(),
            axis="vertical",
            margin=1,
        ),
        LayoutConstraint(  # type: ignore[arg-type]
            "wrong-kind",
            "matching",
        ),
        LayoutConstraint(
            "user-metadata",
            LayoutConstraintKind.KEEPOUT,
            subjects=(),
            margin=1,
            confidence=0.5,
        ),
    ],
)
def test_report_revalidates_public_constraint_objects(
    bad_constraint: LayoutConstraint,
) -> None:
    graph = graph_with_four_devices()
    # Replace empty subject tuples in shape/finite cases with valid bound IDs so
    # each case reaches the intended strict revalidation boundary.
    ids = tuple(sorted(graph.device_ids())[:2])
    if bad_constraint.constraint_id in {"not-finite", "wrong-shape"}:
        bad_constraint = LayoutConstraint(
            bad_constraint.constraint_id,
            bad_constraint.kind,
            subjects=ids,
            axis=bad_constraint.axis,
            tolerance=bad_constraint.tolerance,
            margin=bad_constraint.margin,
        )
    with pytest.raises(LayoutConstraintError):
        layout_inference_report(
            graph,
            LayoutConstraintSet(graph.graph_id, (bad_constraint,)),
        )


def test_report_schema_rejects_oversized_constraint_arrays() -> None:
    repository = Path(__file__).parents[1]
    schema = json.loads(
        (repository / "docs" / "schemas" / "layout-constraint-report-1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    graph = graph_with_four_devices()
    valid = LayoutConstraint(
        "declared",
        LayoutConstraintKind.KEEPOUT,
        subjects=(next(iter(graph.device_ids())),),
        margin=1,
    ).as_dict()
    payload = LayoutInferenceReport(graph.graph_id, (), ()).as_dict()
    payload["user_constraints"] = [valid] * 10_001
    assert not Draft202012Validator(schema).is_valid(payload)


def test_load_constraints_uses_bounded_strict_json(tmp_path: Path) -> None:
    graph = graph_with_four_devices()
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(document(graph.graph_id, [])), encoding="utf-8")
    assert load_layout_constraints(path, graph).constraints == ()
    path.write_text("{", encoding="utf-8")
    with pytest.raises(LayoutConstraintError, match="invalid JSON"):
        load_layout_constraints(path, graph)
