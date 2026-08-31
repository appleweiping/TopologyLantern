from __future__ import annotations

from dataclasses import replace

import pytest

from topology_lantern.constraints import final_violations, has_error, partial_violations
from topology_lantern.graph import TopologyBuilder
from topology_lantern.obligations import initial_state, merge_facts, replace_obligation
from topology_lantern.rules import RULES, applicable_rules, rule_by_id
from topology_lantern.spec import DesignSpec
from topology_lantern.types import (
    DeviceKind,
    Obligation,
    ObligationKind,
    SearchState,
    Severity,
)


def spec(**updates: object) -> DesignSpec:
    value: dict[str, object] = {"name": "test", "supply_voltage": 1.8}
    value.update(updates)
    return DesignSpec.from_mapping(value)


def apply_rule(state: SearchState, selected: DesignSpec, rule_id: str) -> SearchState:
    obligation = state.obligations[0]
    return rule_by_id(rule_id).apply(selected, state, obligation)


def complete(selected: DesignSpec, rule_ids: list[str]) -> SearchState:
    state = initial_state(selected)
    for rule_id in rule_ids:
        state = apply_rule(state, selected, rule_id)
    return state


def test_initial_differential_interface_and_obligations() -> None:
    state = initial_state(spec())
    assert state.topology.port_map() == {
        "vdd": state.topology.port_map()["vdd"],
        "vinn": state.topology.port_map()["vinn"],
        "vinp": state.topology.port_map()["vinp"],
        "vout": state.topology.port_map()["vout"],
        "vss": state.topology.port_map()["vss"],
    }
    assert [item.kind for item in state.obligations] == [
        ObligationKind.INPUT_STAGE,
        ObligationKind.TAIL_BIAS,
        ObligationKind.LOAD,
        ObligationKind.OUTPUT,
    ]
    assert state.fact_map()["polarity"] == "nmos_input"
    assert state.trace == ()
    assert state.topology.devices == ()


def test_initial_single_interface_omits_tail_obligation() -> None:
    state = initial_state(spec(input_mode="single"))
    assert set(state.topology.port_map()) == {"vdd", "vss", "vin", "vout"}
    assert [item.kind for item in state.obligations] == [
        ObligationKind.INPUT_STAGE,
        ObligationKind.LOAD,
        ObligationKind.OUTPUT,
    ]


def test_compensation_obligation_precedes_output() -> None:
    state = initial_state(spec(require_compensation=True))
    assert [item.key for item in state.obligations][-2:] == ["compensation", "output"]


def test_replace_obligation_preserves_order_and_validates_key() -> None:
    state = initial_state(spec())
    replacement = Obligation("new", ObligationKind.LOAD)
    result = replace_obligation(state, "tail", (replacement,))
    assert [item.key for item in result] == ["input", "new", "load", "output"]
    with pytest.raises(ValueError, match="not present"):
        replace_obligation(state, "missing")
    duplicate = replace(state, obligations=(state.obligations[0], state.obligations[0]))
    with pytest.raises(ValueError, match="duplicate"):
        replace_obligation(duplicate, "input")


def test_merge_facts_updates_and_sorts_without_mutation() -> None:
    state = initial_state(spec())
    merged = merge_facts(state, {"z": "last", "input_mode": "changed"})
    assert [fact.name for fact in merged] == sorted(fact.name for fact in merged)
    assert {fact.name: fact.value for fact in merged}["input_mode"] == "changed"
    assert state.fact_map()["input_mode"] == "differential"


def test_rule_catalog_ids_are_unique_and_lookup_is_exact() -> None:
    ids = [rule.rule_id for rule in RULES]
    assert len(ids) == len(set(ids))
    assert all("." in rule_id for rule_id in ids)
    for rule in RULES:
        assert rule_by_id(rule.rule_id) is rule
        assert rule.summary
        assert rule.rationale
    with pytest.raises(KeyError):
        rule_by_id("missing.rule")


def test_only_mode_appropriate_input_rule_applies() -> None:
    differential = initial_state(spec())
    single = initial_state(spec(input_mode="single"))
    assert [
        rule.rule_id for rule in applicable_rules(spec(), differential, differential.obligations[0])
    ] == ["input.diff_pair"]
    single_spec = spec(input_mode="single")
    assert [
        rule.rule_id for rule in applicable_rules(single_spec, single, single.obligations[0])
    ] == ["input.common_source"]


def test_diff_pair_rule_builds_matched_nmos_graph_and_trace() -> None:
    selected = spec()
    before = initial_state(selected)
    state = apply_rule(before, selected, "input.diff_pair")
    devices = state.topology.device_map()
    assert set(devices) == {"M_IN_P", "M_IN_N"}
    assert all(device.kind is DeviceKind.NMOS for device in devices.values())
    assert devices["M_IN_P"].net("g") == "vinp"
    assert devices["M_IN_N"].net("g") == "vinn"
    assert devices["M_IN_P"].net("s") == devices["M_IN_N"].net("s") == "n_tail"
    assert state.fact_map()["symmetry"] == "paired"
    assert state.trace[0].rule_id == "input.diff_pair"
    assert state.trace[0].added_devices == ("M_IN_N", "M_IN_P")
    assert set(state.trace[0].added_nets) == {"n_left", "n_right", "n_tail"}
    assert before.topology.devices == ()


def test_diff_pair_rule_honors_pmos_polarity() -> None:
    selected = spec(polarity="pmos_input")
    state = apply_rule(initial_state(selected), selected, "input.diff_pair")
    assert all(device.kind is DeviceKind.PMOS for device in state.topology.devices)
    assert all(device.net("b") == "vdd" for device in state.topology.devices)


def test_common_source_rule_builds_single_stage() -> None:
    selected = spec(input_mode="single")
    state = apply_rule(initial_state(selected), selected, "input.common_source")
    device = state.topology.device_map()["M_IN"]
    assert device.net("g") == "vin"
    assert device.net("d") == "n_gain"
    assert state.fact_map()["left_drain"] == "n_gain"
    assert state.obligations[0].kind is ObligationKind.LOAD


def differential_after_input(selected: DesignSpec | None = None) -> tuple[DesignSpec, SearchState]:
    chosen = selected or spec()
    return chosen, apply_rule(initial_state(chosen), chosen, "input.diff_pair")


def test_tail_rules_produce_distinct_bias_facts() -> None:
    selected, state = differential_after_input()
    current = apply_rule(state, selected, "tail.current_source")
    resistor = apply_rule(state, selected, "tail.resistor")
    assert current.topology.device_map()["I_TAIL"].kind is DeviceKind.CURRENT_SOURCE
    assert current.fact_map()["tail_structure"] == "current_source"
    assert resistor.topology.device_map()["R_TAIL"].kind is DeviceKind.RESISTOR
    assert resistor.fact_map()["tail_structure"] == "resistor"
    assert current.fact_map()["headroom_units"] == "2"
    assert resistor.fact_map()["headroom_units"] == "1"


def test_tail_rule_applicability_respects_spec() -> None:
    no_current = spec(allowed_devices=["nmos", "pmos", "resistor", "capacitor"])
    _, state = differential_after_input(no_current)
    assert [rule.rule_id for rule in applicable_rules(no_current, state, state.obligations[0])] == [
        "tail.resistor"
    ]
    no_resistor = spec(allow_resistive_bias=False)
    _, state = differential_after_input(no_resistor)
    assert [
        rule.rule_id for rule in applicable_rules(no_resistor, state, state.obligations[0])
    ] == ["tail.current_source"]


def after_tail(selected: DesignSpec, tail: str = "tail.current_source") -> SearchState:
    state = apply_rule(initial_state(selected), selected, "input.diff_pair")
    return apply_rule(state, selected, tail)


def test_resistive_load_is_symmetric_for_pair() -> None:
    selected = spec()
    state = apply_rule(after_tail(selected), selected, "load.resistive")
    devices = state.topology.device_map()
    assert devices["R_LOAD_1"].net("n") == "n_left"
    assert devices["R_LOAD_2"].net("n") == "n_right"
    assert devices["R_LOAD_1"].net("p") == devices["R_LOAD_2"].net("p") == "vdd"
    assert state.fact_map()["load_structure"] == "resistive"


def test_active_load_builds_opposite_polarity_mirror() -> None:
    selected = spec()
    state = apply_rule(after_tail(selected), selected, "load.active_mirror")
    devices = state.topology.device_map()
    reference = devices["M_LOAD_REF"]
    output = devices["M_LOAD_OUT"]
    assert reference.kind is output.kind is DeviceKind.PMOS
    assert reference.net("d") == reference.net("g") == "n_left"
    assert output.net("g") == "n_left"
    assert output.net("d") == "n_right"
    assert state.fact_map()["load_structure"] == "current_mirror"


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        ("active", ["load.active_mirror"]),
        ("resistive", ["load.resistive"]),
        ("either", ["load.resistive", "load.active_mirror"]),
    ],
)
def test_load_preference_controls_rule_branching(preference: str, expected: list[str]) -> None:
    selected = spec(load_preference=preference)
    state = after_tail(selected)
    assert [
        rule.rule_id for rule in applicable_rules(selected, state, state.obligations[0])
    ] == expected


def test_single_active_load_uses_diode_connected_device() -> None:
    selected = spec(input_mode="single", load_preference="active")
    state = apply_rule(initial_state(selected), selected, "input.common_source")
    state = apply_rule(state, selected, "load.active_mirror")
    device = state.topology.device_map()["M_LOAD"]
    assert device.net("d") == device.net("g") == "n_gain"
    assert state.fact_map()["load_structure"] == "diode_connected"


def test_compensation_rule_records_unsized_intent() -> None:
    selected = spec(require_compensation=True)
    state = after_tail(selected)
    state = apply_rule(state, selected, "load.active_mirror")
    state = apply_rule(state, selected, "compensation.capacitor")
    capacitor = state.topology.device_map()["C_COMP"]
    assert capacitor.kind is DeviceKind.CAPACITOR
    assert dict(capacitor.attributes)["value"] == "unsized"
    assert state.fact_map()["compensation"] == "explicit_capacitor"


def test_direct_output_merges_drain_with_port() -> None:
    selected = spec(load_preference="active")
    state = apply_rule(after_tail(selected), selected, "load.active_mirror")
    complete_state = apply_rule(state, selected, "output.direct")
    assert complete_state.obligations == ()
    assert "n_right" not in complete_state.topology.nets
    assert complete_state.topology.device_map()["M_IN_N"].net("d") == "vout"
    assert complete_state.fact_map()["output_structure"] == "direct_drain"
    assert complete_state.fact_map()["right_drain"] == "vout"


def test_differential_output_exposes_both_drains() -> None:
    selected = spec(output_mode="differential", load_preference="resistive")
    state = apply_rule(after_tail(selected), selected, "load.resistive")
    state = apply_rule(state, selected, "output.direct")
    assert "n_left" not in state.topology.nets
    assert "n_right" not in state.topology.nets
    assert state.topology.device_map()["M_IN_P"].net("d") == "voutp"
    assert state.topology.device_map()["M_IN_N"].net("d") == "voutn"
    assert state.fact_map()["left_drain"] == "voutp"
    assert state.fact_map()["right_drain"] == "voutn"


def test_buffered_output_adds_stage_and_bias() -> None:
    selected = spec(load_preference="active")
    state = apply_rule(after_tail(selected), selected, "load.active_mirror")
    state = apply_rule(state, selected, "output.source_follower")
    devices = state.topology.device_map()
    assert devices["M_BUFFER"].net("g") == "n_right"
    assert devices["M_BUFFER"].net("s") == "vout"
    assert devices["I_BUFFER"].net("p") == "vout"
    assert state.fact_map()["stage_count"] == "2"
    assert state.fact_map()["output_structure"] == "source_follower"


def test_inapplicable_rule_refuses_direct_application() -> None:
    selected = spec()
    state = initial_state(selected)
    with pytest.raises(ValueError, match="not applicable"):
        rule_by_id("output.direct").apply(selected, state, state.obligations[0])


def valid_complete_state(tail: str = "tail.current_source") -> tuple[DesignSpec, SearchState]:
    selected = spec(load_preference="active")
    state = complete(
        selected,
        ["input.diff_pair", tail, "load.active_mirror", "output.direct"],
    )
    return selected, state


def test_valid_complete_state_has_no_errors() -> None:
    selected, state = valid_complete_state()
    violations = final_violations(selected, state)
    assert not has_error(violations)
    assert violations == ()


def test_resistor_tail_produces_review_warning_not_error() -> None:
    selected, state = valid_complete_state("tail.resistor")
    violations = final_violations(selected, state)
    warning = next(item for item in violations if item.code == "BIAS001")
    assert warning.severity is Severity.WARNING
    assert has_error(violations) is False


def test_buffer_produces_note_not_error() -> None:
    selected = spec(load_preference="active")
    state = complete(
        selected,
        ["input.diff_pair", "tail.current_source", "load.active_mirror", "output.source_follower"],
    )
    violations = final_violations(selected, state)
    assert next(item for item in violations if item.code == "OUT001").severity is Severity.NOTE
    assert not has_error(violations)


def test_partial_constraint_catches_device_budget() -> None:
    selected, state = valid_complete_state()
    selected = replace(selected, limits=replace(selected.limits, max_devices=1))
    assert "LIM001" in {item.code for item in partial_violations(selected, state)}


def test_partial_constraint_catches_disallowed_device() -> None:
    selected, state = valid_complete_state()
    selected = replace(selected, allowed_devices=(DeviceKind.NMOS,))
    violation = next(item for item in partial_violations(selected, state) if item.code == "DEV001")
    assert violation.subjects


def test_partial_constraint_catches_mos_short_and_bulk() -> None:
    selected = spec()
    topology = (
        TopologyBuilder(initial_state(selected).topology)
        .add_device(
            "M_BAD",
            "nmos",
            {"d": "vdd", "g": "vinp", "s": "vdd", "b": "vdd"},
        )
        .commit()
    )
    state = replace(initial_state(selected), topology=topology)
    assert {item.code for item in partial_violations(selected, state)} >= {"MOS001", "MOS002"}


def test_final_constraints_catch_ports_nets_obligations_and_components() -> None:
    selected = spec()
    state = initial_state(selected)
    violations = final_violations(selected, state)
    codes = {item.code for item in violations}
    assert {"PORT001", "OBL001", "CONN001"} <= codes
    assert has_error(violations)


def test_final_constraint_catches_dangling_internal_net() -> None:
    selected, state = valid_complete_state()
    topology = TopologyBuilder(state.topology).add_net("dangling").commit()
    violations = final_violations(selected, replace(state, topology=topology))
    assert "NET001" in {item.code for item in violations}


def test_final_constraint_catches_missing_compensation_fact() -> None:
    selected, state = valid_complete_state()
    compensated = replace(selected, require_compensation=True)
    assert "COMP001" in {item.code for item in final_violations(compensated, state)}


def test_violation_and_trace_objects_serialize() -> None:
    selected, state = valid_complete_state("tail.resistor")
    violation = next(item for item in final_violations(selected, state) if item.code == "BIAS001")
    assert violation.as_dict()["severity"] == "warning"
    assert state.trace[0].as_dict()["rule_id"] == "input.diff_pair"
    assert state.as_dict()["topology"] == state.topology.as_dict()
