"""Independent, replayable rewrite rules for conceptual analog topologies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from topology_lantern.graph import GraphError, TopologyBuilder
from topology_lantern.obligations import merge_facts, replace_obligation
from topology_lantern.spec import DesignSpec
from topology_lantern.types import (
    DeviceKind,
    Obligation,
    ObligationKind,
    SearchState,
    TraceStep,
)

Transform = Callable[[DesignSpec, SearchState, Obligation], SearchState]
Predicate = Callable[[DesignSpec, SearchState, Obligation], bool]


@dataclass(frozen=True, slots=True)
class RewriteRule:
    rule_id: str
    obligation: ObligationKind
    summary: str
    rationale: str
    predicate: Predicate
    transform: Transform

    def applicable(self, spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
        return obligation.kind is self.obligation and self.predicate(spec, state, obligation)

    def apply(self, spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
        if not self.applicable(spec, state, obligation):
            raise GraphError(f"rule {self.rule_id} is not applicable to {obligation.key}")
        before_devices = set(state.topology.device_map())
        before_nets = set(state.topology.nets)
        transformed = self.transform(spec, state, obligation)
        added_devices = tuple(sorted(set(transformed.topology.device_map()) - before_devices))
        added_nets = tuple(sorted(set(transformed.topology.nets) - before_nets))
        produced_keys = tuple(
            item.key
            for item in transformed.obligations
            if item.key not in {old.key for old in state.obligations}
        )
        step = TraceStep(
            index=len(state.trace) + 1,
            rule_id=self.rule_id,
            summary=self.summary,
            rationale=self.rationale,
            consumed=(obligation.key,),
            produced=produced_keys,
            added_devices=added_devices,
            added_nets=added_nets,
        )
        return SearchState(
            transformed.topology,
            transformed.obligations,
            transformed.facts,
            (*state.trace, step),
        )


def _always(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    return True


def _allowed(kind: DeviceKind) -> Predicate:
    def predicate(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
        return kind in spec.allowed_devices

    return predicate


def _all_allowed(*kinds: DeviceKind) -> Predicate:
    def predicate(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
        return all(kind in spec.allowed_devices for kind in kinds)

    return predicate


def _resistive_allowed(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    return DeviceKind.RESISTOR in spec.allowed_devices and spec.load_preference in {
        "resistive",
        "either",
    }


def _active_allowed(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    opposite = DeviceKind.PMOS if spec.polarity == "nmos_input" else DeviceKind.NMOS
    return (
        spec.output_mode == "single"
        and opposite in spec.allowed_devices
        and spec.load_preference in {"active", "either"}
    )


def _input_pair(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    kind = DeviceKind.NMOS if spec.polarity == "nmos_input" else DeviceKind.PMOS
    rail = "vss" if kind is DeviceKind.NMOS else "vdd"
    builder = TopologyBuilder(state.topology)
    builder.add_net("n_left").add_net("n_right").add_net("n_tail")
    builder.add_device(
        "M_IN_P",
        kind,
        {"d": "n_left", "g": "vinp", "s": "n_tail", "b": rail},
        {"role": "differential_input"},
    )
    builder.add_device(
        "M_IN_N",
        kind,
        {"d": "n_right", "g": "vinn", "s": "n_tail", "b": rail},
        {"role": "differential_input"},
    )
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(
            state,
            {
                "input_structure": "differential_pair",
                "left_drain": "n_left",
                "right_drain": "n_right",
                "tail_net": "n_tail",
                "stage_count": "1",
                "symmetry": "paired",
            },
        ),
        state.trace,
    )


def _single_transconductor(
    spec: DesignSpec, state: SearchState, obligation: Obligation
) -> SearchState:
    kind = DeviceKind.NMOS if spec.polarity == "nmos_input" else DeviceKind.PMOS
    source_rail = "vss" if kind is DeviceKind.NMOS else "vdd"
    builder = TopologyBuilder(state.topology)
    builder.add_net("n_gain")
    builder.add_device(
        "M_IN",
        kind,
        {"d": "n_gain", "g": "vin", "s": source_rail, "b": source_rail},
        {"role": "common_source"},
    )
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(
            state,
            {
                "input_structure": "common_source",
                "left_drain": "n_gain",
                "right_drain": "n_gain",
                "stage_count": "1",
                "symmetry": "single_ended",
            },
        ),
        state.trace,
    )


def _tail_current(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    facts = state.fact_map()
    tail = facts["tail_net"]
    builder = TopologyBuilder(state.topology)
    if spec.polarity == "nmos_input":
        connections = {"p": tail, "n": "vss"}
    else:
        connections = {"p": "vdd", "n": tail}
    builder.add_device(
        "I_TAIL",
        DeviceKind.CURRENT_SOURCE,
        connections,
        {"role": "tail_bias"},
    )
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(state, {"tail_structure": "current_source", "headroom_units": "2"}),
        state.trace,
    )


def _tail_resistor(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    facts = state.fact_map()
    tail = facts["tail_net"]
    rail = "vss" if spec.polarity == "nmos_input" else "vdd"
    builder = TopologyBuilder(state.topology)
    builder.add_device(
        "R_TAIL",
        DeviceKind.RESISTOR,
        {"p": tail, "n": rail},
        {"role": "tail_bias", "value": "unsized"},
    )
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(state, {"tail_structure": "resistor", "headroom_units": "1"}),
        state.trace,
    )


def _resistive_load(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    facts = state.fact_map()
    drains = [facts["left_drain"]]
    if facts["right_drain"] != facts["left_drain"]:
        drains.append(facts["right_drain"])
    rail = "vdd" if spec.polarity == "nmos_input" else "vss"
    builder = TopologyBuilder(state.topology)
    for index, drain in enumerate(drains, start=1):
        builder.add_device(
            f"R_LOAD_{index}",
            DeviceKind.RESISTOR,
            {"p": rail, "n": drain},
            {"role": "load", "value": "unsized"},
        )
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(state, {"load_structure": "resistive"}),
        state.trace,
    )


def _active_mirror_load(
    spec: DesignSpec, state: SearchState, obligation: Obligation
) -> SearchState:
    facts = state.fact_map()
    left = facts["left_drain"]
    right = facts["right_drain"]
    kind = DeviceKind.PMOS if spec.polarity == "nmos_input" else DeviceKind.NMOS
    rail = "vdd" if kind is DeviceKind.PMOS else "vss"
    builder = TopologyBuilder(state.topology)
    if left == right:
        builder.add_device(
            "M_LOAD",
            kind,
            {"d": left, "g": left, "s": rail, "b": rail},
            {"role": "diode_load"},
        )
        structure = "diode_connected"
    else:
        builder.add_device(
            "M_LOAD_REF",
            kind,
            {"d": left, "g": left, "s": rail, "b": rail},
            {"role": "mirror_reference"},
        )
        builder.add_device(
            "M_LOAD_OUT",
            kind,
            {"d": right, "g": left, "s": rail, "b": rail},
            {"role": "mirror_output"},
        )
        structure = "current_mirror"
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(state, {"load_structure": structure, "headroom_units": "2"}),
        state.trace,
    )


def _compensation_cap(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    facts = state.fact_map()
    left = facts["left_drain"]
    right = facts["right_drain"]
    target = left if left != right else "vss"
    builder = TopologyBuilder(state.topology)
    builder.add_device(
        "C_COMP",
        DeviceKind.CAPACITOR,
        {"p": right, "n": target},
        {"role": "compensation", "value": "unsized"},
    )
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(state, {"compensation": "explicit_capacitor"}),
        state.trace,
    )


def _direct_output(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    facts = state.fact_map()
    builder = TopologyBuilder(state.topology)
    if spec.output_mode == "differential":
        builder.rename_net(facts["left_drain"], "voutp")
        builder.rename_net(facts["right_drain"], "voutn")
        output_structure = "differential_drains"
        fact_updates = {
            "left_drain": "voutp",
            "right_drain": "voutn",
            "output_structure": output_structure,
        }
    else:
        shared_drain = facts["left_drain"] == facts["right_drain"]
        builder.rename_net(facts["right_drain"], "vout")
        output_structure = "direct_drain"
        fact_updates = {"right_drain": "vout", "output_structure": output_structure}
        if shared_drain:
            fact_updates["left_drain"] = "vout"
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(state, fact_updates),
        state.trace,
    )


def _buffered_output(spec: DesignSpec, state: SearchState, obligation: Obligation) -> SearchState:
    facts = state.fact_map()
    raw = facts["right_drain"]
    builder = TopologyBuilder(state.topology)
    if spec.polarity == "nmos_input":
        kind = DeviceKind.NMOS
        connections = {"d": "vdd", "g": raw, "s": "vout", "b": "vss"}
        bias = {"p": "vout", "n": "vss"}
    else:
        kind = DeviceKind.PMOS
        connections = {"d": "vss", "g": raw, "s": "vout", "b": "vdd"}
        bias = {"p": "vdd", "n": "vout"}
    builder.add_device("M_BUFFER", kind, connections, {"role": "source_follower"})
    builder.add_device("I_BUFFER", DeviceKind.CURRENT_SOURCE, bias, {"role": "buffer_bias"})
    return SearchState(
        builder.commit(),
        replace_obligation(state, obligation.key),
        merge_facts(
            state,
            {
                "output_structure": "source_follower",
                "stage_count": "2",
                "headroom_units": "3",
            },
        ),
        state.trace,
    )


def _single_mode(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    return spec.input_mode == "single"


def _differential_mode(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    return spec.input_mode == "differential"


def _resistor_tail_allowed(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    return spec.allow_resistive_bias and DeviceKind.RESISTOR in spec.allowed_devices


def _buffer_allowed(spec: DesignSpec, state: SearchState, obligation: Obligation) -> bool:
    input_kind = DeviceKind.NMOS if spec.polarity == "nmos_input" else DeviceKind.PMOS
    return (
        spec.output_mode == "single"
        and input_kind in spec.allowed_devices
        and DeviceKind.CURRENT_SOURCE in spec.allowed_devices
    )


RULES: tuple[RewriteRule, ...] = (
    RewriteRule(
        "input.diff_pair",
        ObligationKind.INPUT_STAGE,
        "Create a matched differential input pair",
        "A paired transconductor exposes differential input intent and two load nodes.",
        _differential_mode,
        _input_pair,
    ),
    RewriteRule(
        "input.common_source",
        ObligationKind.INPUT_STAGE,
        "Create a common-source input stage",
        "A single gate-driven transconductor satisfies single-ended input intent.",
        _single_mode,
        _single_transconductor,
    ),
    RewriteRule(
        "tail.current_source",
        ObligationKind.TAIL_BIAS,
        "Bias the pair with an ideal conceptual current source",
        "A tail current source records high small-signal tail impedance before sizing.",
        _allowed(DeviceKind.CURRENT_SOURCE),
        _tail_current,
    ),
    RewriteRule(
        "tail.resistor",
        ObligationKind.TAIL_BIAS,
        "Bias the pair with a resistor",
        "A resistor is a lower-headroom conceptual bias alternative when explicitly allowed.",
        _resistor_tail_allowed,
        _tail_resistor,
    ),
    RewriteRule(
        "load.resistive",
        ObligationKind.LOAD,
        "Attach symmetric resistive loads",
        "Resistive loads use transparent passive branches and preserve both drain nodes.",
        _resistive_allowed,
        _resistive_load,
    ),
    RewriteRule(
        "load.active_mirror",
        ObligationKind.LOAD,
        "Attach an active mirror load",
        "The opposite-polarity mirror offers a compact active-load conceptual branch.",
        _active_allowed,
        _active_mirror_load,
    ),
    RewriteRule(
        "compensation.capacitor",
        ObligationKind.COMPENSATION,
        "Insert an explicit compensation capacitor",
        "The unsized capacitor records compensation intent without claiming stability.",
        _allowed(DeviceKind.CAPACITOR),
        _compensation_cap,
    ),
    RewriteRule(
        "output.direct",
        ObligationKind.OUTPUT,
        "Expose the selected drain node as output",
        "Direct output avoids an additional headroom-consuming stage.",
        _always,
        _direct_output,
    ),
    RewriteRule(
        "output.source_follower",
        ObligationKind.OUTPUT,
        "Add a conceptual source-follower output buffer",
        "A second stage illustrates a buffered output alternative and its device cost.",
        _buffer_allowed,
        _buffered_output,
    ),
)


def applicable_rules(
    spec: DesignSpec, state: SearchState, obligation: Obligation
) -> tuple[RewriteRule, ...]:
    return tuple(rule for rule in RULES if rule.applicable(spec, state, obligation))


def rule_by_id(rule_id: str) -> RewriteRule:
    for rule in RULES:
        if rule.rule_id == rule_id:
            return rule
    raise KeyError(rule_id)
