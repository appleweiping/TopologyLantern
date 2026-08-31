from __future__ import annotations

from dataclasses import replace

import pytest

from topology_lantern import __version__
from topology_lantern.canonical import topology_signature
from topology_lantern.explain import replay_rule_ids, verify_replay
from topology_lantern.rank import dominates, objective_vector, rank_candidates
from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec
from topology_lantern.types import ReplayError, Severity


def spec(**updates: object) -> DesignSpec:
    mapping: dict[str, object] = {"name": "search", "supply_voltage": 1.8}
    mapping.update(updates)
    return DesignSpec.from_mapping(mapping)


def test_default_search_generates_expected_cross_product() -> None:
    result = generate_candidates(spec())
    assert len(result.candidates) == 8
    assert result.explored_states == 16
    assert result.pruned_states == 0
    assert result.duplicate_states == 0
    assert result.exhausted is True
    assert len(result.rule_catalog) == 9
    paths = {tuple(step.rule_id for step in candidate.trace) for candidate in result.candidates}
    assert all(path[0] == "input.diff_pair" for path in paths)
    assert {path[1] for path in paths} == {"tail.current_source", "tail.resistor"}
    assert {path[2] for path in paths} == {"load.active_mirror", "load.resistive"}
    assert {path[3] for path in paths} == {"output.direct", "output.source_follower"}


def test_search_is_byte_for_byte_deterministic() -> None:
    selected = spec()
    first = generate_candidates(selected)
    second = generate_candidates(selected)
    assert first == second
    assert [candidate.signature for candidate in first.candidates] == [
        candidate.signature for candidate in second.candidates
    ]


def test_limit_caps_candidates_and_reports_unexhausted_queue() -> None:
    result = generate_candidates(spec(), limit=2)
    assert len(result.candidates) == 2
    assert result.exhausted is False
    with pytest.raises(ValueError, match="positive"):
        generate_candidates(spec(), limit=0)
    with pytest.raises(ValueError, match="positive integer"):
        generate_candidates(spec(), limit=True)
    with pytest.raises(ValueError, match="positive integer"):
        generate_candidates(spec(), limit=1.5)  # type: ignore[arg-type]


def test_limit_cannot_exceed_spec_candidate_limit() -> None:
    selected = spec(limits={"max_candidates": 1})
    result = generate_candidates(selected, limit=20)
    assert len(result.candidates) == 1


def test_active_preference_removes_resistive_load_branches() -> None:
    result = generate_candidates(spec(load_preference="active"))
    assert len(result.candidates) == 4
    assert all(
        "load.active_mirror" in [step.rule_id for step in candidate.trace]
        for candidate in result.candidates
    )


def test_disabling_resistive_bias_removes_tail_branch() -> None:
    result = generate_candidates(spec(allow_resistive_bias=False))
    assert len(result.candidates) == 4
    assert all(
        "tail.current_source" in [step.rule_id for step in candidate.trace]
        for candidate in result.candidates
    )


def test_single_input_generates_common_source_without_tail() -> None:
    result = generate_candidates(spec(input_mode="single"))
    assert len(result.candidates) == 4
    for candidate in result.candidates:
        path = [step.rule_id for step in candidate.trace]
        assert path[0] == "input.common_source"
        assert not any(rule.startswith("tail.") for rule in path)
        assert candidate.topology.device_map()["M_IN"].net("g") == "vin"


def test_differential_output_disables_buffer_branch() -> None:
    result = generate_candidates(spec(output_mode="differential"))
    assert len(result.candidates) == 2
    for candidate in result.candidates:
        assert candidate.topology.port_map()["voutp"].value == "output"
        assert candidate.topology.port_map()["voutn"].value == "output"
        assert candidate.trace[-1].rule_id == "output.direct"
        assert "load.resistive" in {step.rule_id for step in candidate.trace}
        assert "load.active_mirror" not in {step.rule_id for step in candidate.trace}


def test_required_compensation_adds_rule_and_device() -> None:
    result = generate_candidates(spec(require_compensation=True))
    assert len(result.candidates) == 8
    for candidate in result.candidates:
        assert "C_COMP" in candidate.topology.device_map()
        assert [step.rule_id for step in candidate.trace][-2] == "compensation.capacitor"


def test_missing_required_device_family_can_yield_no_candidates() -> None:
    selected = spec(
        require_compensation=True,
        allowed_devices=["nmos", "pmos", "resistor", "current_source"],
    )
    result = generate_candidates(selected)
    assert result.candidates == ()
    assert result.pruned_states >= 1
    assert result.exhausted is True


def test_state_limit_stops_search_deterministically() -> None:
    selected = spec(limits={"max_states": 1})
    result = generate_candidates(selected)
    assert result.candidates == ()
    assert result.explored_states == 1
    assert result.exhausted is False


def test_device_limit_prunes_complete_branches() -> None:
    selected = spec(limits={"max_devices": 2})
    result = generate_candidates(selected)
    assert result.candidates == ()
    assert result.pruned_states > 0


def test_candidates_are_unique_by_canonical_signature() -> None:
    result = generate_candidates(spec())
    signatures = [candidate.signature for candidate in result.candidates]
    ids = [candidate.candidate_id for candidate in result.candidates]
    assert len(signatures) == len(set(signatures))
    assert len(ids) == len(set(ids))
    assert all(candidate_id.startswith("TL-") and len(candidate_id) == 15 for candidate_id in ids)


def test_candidate_signature_matches_topology() -> None:
    selected = spec()
    result = generate_candidates(selected)
    for candidate in result.candidates:
        assert candidate.signature == topology_signature(
            candidate.topology,
            max_permutations=selected.limits.max_canonical_permutations,
        )


def test_resistive_tail_warning_is_counted_in_metrics_and_score() -> None:
    result = generate_candidates(spec(load_preference="active"))
    resistive = next(
        candidate
        for candidate in result.candidates
        if any(step.rule_id == "tail.resistor" for step in candidate.trace)
        and candidate.trace[-1].rule_id == "output.direct"
    )
    current = next(
        candidate
        for candidate in result.candidates
        if any(step.rule_id == "tail.current_source" for step in candidate.trace)
        and candidate.trace[-1].rule_id == "output.direct"
    )
    assert resistive.metrics.review_warnings == 1
    assert current.metrics.review_warnings == 0
    assert any(item.code == "BIAS001" for item in resistive.violations)


def test_buffer_note_is_counted_as_review_note() -> None:
    result = generate_candidates(spec(load_preference="active"))
    buffered = next(
        candidate
        for candidate in result.candidates
        if candidate.trace[-1].rule_id == "output.source_follower"
    )
    assert buffered.metrics.review_warnings >= 1
    assert any(
        item.code == "OUT001" and item.severity is Severity.NOTE for item in buffered.violations
    )


def test_objective_vector_contains_only_minimized_structural_metrics() -> None:
    candidate = generate_candidates(spec(), limit=1).candidates[0]
    vector = objective_vector(candidate)
    assert vector == (
        candidate.metrics.device_count,
        candidate.metrics.headroom_units,
        candidate.metrics.passive_count,
        candidate.metrics.symmetry_penalty,
        candidate.metrics.review_warnings,
        candidate.metrics.stage_count,
    )


def test_dominance_and_pareto_fronts_are_transparent() -> None:
    result = generate_candidates(spec(load_preference="active"))
    candidates = result.candidates
    assert any(
        dominates(left, right) for left in candidates for right in candidates if left is not right
    )
    for candidate in candidates:
        if candidate.pareto_rank == 0:
            assert not any(
                dominates(other, candidate) for other in candidates if other is not candidate
            )


def test_objective_weights_change_order_within_front_not_membership() -> None:
    base_spec = spec(load_preference="either")
    base = generate_candidates(base_spec).candidates
    changed_spec = spec(
        load_preference="either",
        objectives={
            "device_count": 0,
            "headroom": 10,
            "symmetry": 0,
            "passives": 0,
            "warnings": 1,
        },
    )
    reranked = rank_candidates(
        changed_spec, tuple(replace(item, pareto_rank=0, score=0) for item in base)
    )
    assert {item.signature: item.pareto_rank for item in base} == {
        item.signature: item.pareto_rank for item in reranked
    }
    assert all(item.score >= 0 for item in reranked)


def test_every_candidate_trace_replays_and_verifies() -> None:
    selected = spec(require_compensation=True)
    result = generate_candidates(selected)
    for candidate in result.candidates:
        state = verify_replay(selected, candidate)
        assert state.obligations == ()
        assert topology_signature(state.topology) == candidate.signature


def test_replay_rejects_unknown_extra_and_inapplicable_rules() -> None:
    selected = spec()
    with pytest.raises(ReplayError, match="unknown rule"):
        replay_rule_ids(selected, ["unknown.rule"])
    with pytest.raises(ReplayError, match="not applicable"):
        replay_rule_ids(selected, ["output.direct"])
    valid = [step.rule_id for step in generate_candidates(selected, limit=1).candidates[0].trace]
    with pytest.raises(ReplayError, match="extra rule"):
        replay_rule_ids(selected, [*valid, "output.direct"])


def test_verify_replay_rejects_tampered_signature() -> None:
    selected = spec()
    candidate = generate_candidates(selected, limit=1).candidates[0]
    with pytest.raises(ReplayError, match="does not match"):
        verify_replay(selected, replace(candidate, signature="0" * 64))


def test_verify_replay_rejects_tampered_id_and_derived_evidence() -> None:
    selected = spec()
    candidate = generate_candidates(selected, limit=1).candidates[0]
    with pytest.raises(ReplayError, match="candidate ID"):
        verify_replay(selected, replace(candidate, candidate_id="TL-tampered"))
    with pytest.raises(ReplayError, match="metrics"):
        verify_replay(
            selected,
            replace(candidate, metrics=replace(candidate.metrics, device_count=-999)),
        )


def test_generation_result_serializes_search_evidence() -> None:
    result = generate_candidates(spec(), limit=2)
    value = result.as_dict()
    assert value["schema_version"] == 1
    assert value["tool"] == {"name": "TopologyLantern", "version": __version__}
    assert value["search"]["explored_states"] == result.explored_states
    assert value["search"]["requested_limit"] == result.requested_limit
    assert len(value["candidates"]) == 2
    assert value["candidates"][0]["trace"]
