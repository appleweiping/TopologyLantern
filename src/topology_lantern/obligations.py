"""Initial ports, facts, and proof obligations derived from design intent."""

from __future__ import annotations

from topology_lantern.graph import TopologyBuilder
from topology_lantern.spec import DesignSpec
from topology_lantern.types import (
    Fact,
    Obligation,
    ObligationKind,
    PortRole,
    SearchState,
)


def initial_state(spec: DesignSpec) -> SearchState:
    """Create a topology interface and ordered obligations for one search."""

    builder = TopologyBuilder()
    builder.add_port("vdd", PortRole.SUPPLY)
    builder.add_port("vss", PortRole.GROUND)
    if spec.input_mode == "differential":
        builder.add_port("vinp", PortRole.INPUT)
        builder.add_port("vinn", PortRole.INPUT)
    else:
        builder.add_port("vin", PortRole.INPUT)
    if spec.output_mode == "differential":
        builder.add_port("voutp", PortRole.OUTPUT)
        builder.add_port("voutn", PortRole.OUTPUT)
    else:
        builder.add_port("vout", PortRole.OUTPUT)

    obligations = [
        Obligation(
            "input",
            ObligationKind.INPUT_STAGE,
            (("mode", spec.input_mode), ("polarity", spec.polarity)),
        )
    ]
    if spec.input_mode == "differential":
        obligations.append(
            Obligation("tail", ObligationKind.TAIL_BIAS, (("polarity", spec.polarity),))
        )
    obligations.append(
        Obligation("load", ObligationKind.LOAD, (("preference", spec.load_preference),))
    )
    if spec.require_compensation:
        obligations.append(Obligation("compensation", ObligationKind.COMPENSATION))
    obligations.append(Obligation("output", ObligationKind.OUTPUT, (("mode", spec.output_mode),)))
    facts = (
        Fact("input_mode", spec.input_mode),
        Fact("output_mode", spec.output_mode),
        Fact("polarity", spec.polarity),
        Fact("supply_voltage", f"{spec.supply_voltage:g}"),
    )
    return SearchState(builder.commit(), tuple(obligations), facts)


def replace_obligation(
    state: SearchState,
    consumed_key: str,
    produced: tuple[Obligation, ...] = (),
) -> tuple[Obligation, ...]:
    """Replace exactly one obligation without disturbing deterministic order."""

    result: list[Obligation] = []
    found = False
    for obligation in state.obligations:
        if obligation.key == consumed_key:
            if found:
                raise ValueError(f"duplicate obligation key {consumed_key!r}")
            result.extend(produced)
            found = True
        else:
            result.append(obligation)
    if not found:
        raise ValueError(f"obligation {consumed_key!r} is not present")
    return tuple(result)


def merge_facts(state: SearchState, updates: dict[str, str]) -> tuple[Fact, ...]:
    values = state.fact_map()
    values.update(updates)
    return tuple(Fact(name, value) for name, value in sorted(values.items()))
