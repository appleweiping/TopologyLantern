"""The search ledger, and comparing what two specifications admit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from topology_lantern.cli import main
from topology_lantern.compare import Absence, diff_results, render_diff
from topology_lantern.emit import result_json
from topology_lantern.ledger import (
    MAX_RECORDED_TOPOLOGIES,
    LedgerRecorder,
    RefusedTopology,
    RuleLedger,
    SearchLedger,
)
from topology_lantern.search import generate_candidates
from topology_lantern.types import ConstraintViolation, Obligation, ObligationKind, Severity

EXAMPLES = Path(__file__).parents[1] / "examples"
BASE = EXAMPLES / "low_voltage_diff_stage.json"


def spec_with(tmp_path: Path, name: str, **changes: object) -> Path:
    document = json.loads(BASE.read_text(encoding="utf-8"))
    document.update(changes)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def error(code: str) -> ConstraintViolation:
    return ConstraintViolation(code=code, severity=Severity.ERROR, message=code)


def note(code: str) -> ConstraintViolation:
    return ConstraintViolation(code=code, severity=Severity.NOTE, message=code)


# ---------------------------------------------------------------------------
# The rule ledger.
# ---------------------------------------------------------------------------


def test_a_rule_the_specification_forbids_is_declined_not_pruned() -> None:
    """Where the causal information actually lives.

    Rules are filtered by a predicate over the specification before any state is
    built, so forbidding one produces no pruned state at all. The bundled
    examples prune nothing, and the difference survives only in this count.
    """

    permissive = generate_candidates(str(BASE))
    assert permissive.pruned_states == 0
    assert "tail.resistor" in permissive.ledger.rules.applied


def test_forbidding_resistive_bias_closes_the_rule_off(tmp_path: Path) -> None:
    strict = generate_candidates(str(spec_with(tmp_path, "strict", allow_resistive_bias=False)))
    assert "tail.resistor" in strict.ledger.rules.closed_off
    assert strict.ledger.rules.applied.get("tail.resistor", 0) == 0


def test_a_rule_that_is_used_is_never_reported_as_closed_off() -> None:
    ledger = RuleLedger(applied={"a": 2}, declined={"a": 1, "b": 3})
    assert ledger.used == frozenset({"a"})
    assert ledger.closed_off == frozenset({"b"})


def test_a_rule_applied_zero_times_does_not_count_as_used() -> None:
    assert RuleLedger(applied={"a": 0}, declined={"a": 1}).closed_off == frozenset({"a"})


def test_the_recorder_splits_governing_rules_by_what_was_permitted() -> None:
    recorder = LedgerRecorder()
    recorder.rules(("a", "b", "c"), ("a", "c"))
    ledger = recorder.finish()
    assert ledger.rules.applied == {"a": 1, "c": 1}
    assert ledger.rules.declined == {"b": 1}


# ---------------------------------------------------------------------------
# Rejection counts.
# ---------------------------------------------------------------------------


def test_causes_and_codes_are_counted_separately() -> None:
    recorder = LedgerRecorder()
    recorder.depth()
    recorder.partial((error("E1"), note("N1")))
    recorder.no_rule(Obligation(key="k", kind=ObligationKind.LOAD))
    ledger = recorder.finish()
    assert ledger.by_cause == {"depth_limit": 1, "partial_violation": 1, "no_applicable_rule": 1}
    assert ledger.by_code == {"E1": 1}  # notes are not why a state died
    assert ledger.by_obligation == {"load": 1}
    assert ledger.rejected == 3


def test_a_refused_topology_is_kept_with_its_violations() -> None:
    recorder = LedgerRecorder()
    recorder.final("sig-1", (error("E2"),))
    ledger = recorder.finish()
    found = ledger.refusal_for("sig-1")
    assert found is not None
    assert found.codes == ("E2",)


def test_the_same_topology_is_not_recorded_twice() -> None:
    # Equivalent graphs reached by different rule orders fail identically, so
    # repeats would crowd the record with one topology.
    recorder = LedgerRecorder()
    for _ in range(5):
        recorder.final("sig-1", (error("E2"),))
    ledger = recorder.finish()
    assert len(ledger.refused) == 1
    assert ledger.by_cause["final_violation"] == 5


def test_the_record_is_bounded_and_says_when_it_filled() -> None:
    recorder = LedgerRecorder(limit=2)
    for index in range(5):
        recorder.final(f"sig-{index}", (error("E3"),))
    ledger = recorder.finish()
    assert len(ledger.refused) == 2
    assert ledger.truncated
    assert ledger.by_cause["final_violation"] == 5


def test_an_unrecorded_signature_is_not_evidence_of_anything() -> None:
    ledger = LedgerRecorder(limit=0).finish()
    assert ledger.refusal_for("anything") is None


@pytest.mark.parametrize("limit", [-1, 1.5, True, "3"])
def test_a_bad_record_limit_is_refused(limit: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        LedgerRecorder(limit=limit)  # type: ignore[arg-type]


def test_the_default_bound_is_generous_but_finite() -> None:
    assert 0 < MAX_RECORDED_TOPOLOGIES < 100_000


def test_a_refused_topology_reports_only_error_codes() -> None:
    refused = RefusedTopology(signature="s", violations=(error("E"), note("N")))
    assert refused.codes == ("E",)
    assert len(refused.as_dict()["violations"]) == 2


# ---------------------------------------------------------------------------
# The report keeps its shape unless asked otherwise.
# ---------------------------------------------------------------------------


def test_the_default_report_gains_no_field() -> None:
    """Downstream readers reject unknown fields, which is why this is opt-in."""

    payload = json.loads(result_json(generate_candidates(str(BASE))))
    assert "ledger" not in payload
    assert payload["schema_version"] == 1


def test_the_ledger_is_included_on_request() -> None:
    payload = json.loads(result_json(generate_candidates(str(BASE)), ledger=True))
    assert "ledger" in payload
    assert payload["ledger"]["rules"]["applied"]


# ---------------------------------------------------------------------------
# Comparing two runs.
# ---------------------------------------------------------------------------


def test_a_specification_compared_with_itself_reports_no_change() -> None:
    result = generate_candidates(str(BASE))
    diff = diff_results(result, generate_candidates(str(BASE)))
    assert diff.unchanged
    assert diff.same_spec
    assert not diff.only_left and not diff.only_right
    assert "No change" in render_diff(diff)


def test_topologies_are_matched_by_structure_not_by_position(tmp_path: Path) -> None:
    """Candidate numbering follows the ranking and moves when anything does."""

    left = generate_candidates(str(BASE))
    right = generate_candidates(str(spec_with(tmp_path, "s", allow_resistive_bias=False)))
    diff = diff_results(left, right)
    assert diff.shared
    moved = diff.reordered
    assert moved, "this change should reorder surviving topologies"
    assert all(item.left_position != item.right_position for item in moved)
    assert all(item.moved == item.left_position - item.right_position for item in diff.shared)


def test_a_closed_rule_explains_the_topologies_that_used_it(tmp_path: Path) -> None:
    """The strongest explanation: proved from the topology's own trace."""

    left = generate_candidates(str(BASE))
    right = generate_candidates(str(spec_with(tmp_path, "s", allow_resistive_bias=False)))
    diff = diff_results(left, right)
    assert diff.only_left
    assert diff.rules_only_left == ("tail.resistor",)
    for item in diff.only_left:
        assert item.absence is Absence.RULE_CLOSED
        assert item.blocked_by == ("tail.resistor",)
        assert "closed off tail.resistor" in item.explanation(other_truncated=False)


def test_a_newly_required_stage_shows_as_a_rule_every_topology_carries(
    tmp_path: Path,
) -> None:
    """A requirement adds an obligation rather than closing a rule off.

    The weaker wording is deliberate: this is an observation about the other
    run's output, not a rule read out of its specification.
    """

    left = generate_candidates(str(BASE))
    right = generate_candidates(str(spec_with(tmp_path, "c", require_compensation=True)))
    diff = diff_results(left, right)
    assert diff.rules_only_right == ("compensation.capacitor",)
    assert diff.only_left
    for item in diff.only_left:
        assert item.absence is Absence.LACKS_UNIVERSAL
        assert item.lacks == ("compensation.capacitor",)
        assert "every topology the other run produced" in item.explanation(other_truncated=False)


def test_an_absence_with_no_cause_says_so() -> None:
    from topology_lantern.compare import AbsentTopology

    absent = AbsentTopology(
        signature="s", candidate_id="TL-x", position=1, absence=Absence.NOT_REACHED, refusal=None
    )
    assert "never built it" in absent.explanation(other_truncated=False)
    assert "record was full" in absent.explanation(other_truncated=True)


def test_a_recorded_refusal_outranks_every_weaker_reading() -> None:
    from topology_lantern.compare import AbsentTopology

    absent = AbsentTopology(
        signature="s",
        candidate_id="TL-x",
        position=1,
        absence=Absence.REFUSED,
        refusal=RefusedTopology(signature="s", violations=(error("E9"),)),
        blocked_by=("some.rule",),
        lacks=("other.rule",),
    )
    assert absent.explanation(other_truncated=True) == "built and refused: E9"


def test_a_refusal_without_an_error_level_violation_still_reads() -> None:
    from topology_lantern.compare import AbsentTopology

    absent = AbsentTopology(
        signature="s",
        candidate_id="TL-x",
        position=1,
        absence=Absence.REFUSED,
        refusal=RefusedTopology(signature="s", violations=(note("N1"),)),
    )
    assert "no error-level violation recorded" in absent.explanation(other_truncated=False)


def test_an_exhausted_pair_is_not_reported_as_limited() -> None:
    diff = diff_results(generate_candidates(str(BASE)), generate_candidates(str(BASE)))
    assert not diff.limited
    assert "stopped before exhausting" not in render_diff(diff)


def test_a_limited_search_warns_before_anything_is_read_into_an_absence() -> None:
    left = generate_candidates(str(BASE), limit=2)
    diff = diff_results(left, generate_candidates(str(BASE)))
    rendered = render_diff(diff)
    assert diff.limited
    # The caveats lead, before any count a reader might act on.
    caveats, _, body = rendered.partition("\n\n")
    assert "At least one search stopped" in caveats
    assert "topologies in both" in body


def test_a_comparison_serializes_every_strength_it_used(tmp_path: Path) -> None:
    left = generate_candidates(str(BASE))
    right = generate_candidates(str(spec_with(tmp_path, "s", allow_resistive_bias=False)))
    payload = diff_results(left, right).as_dict()
    assert payload["schema_version"] == 1
    assert payload["rules"]["only_left"] == ["tail.resistor"]
    assert payload["only_left"][0]["absence"] == "rule_closed"
    assert payload["only_left"][0]["blocked_by"] == ["tail.resistor"]
    json.dumps(payload, allow_nan=False)


def test_an_empty_run_has_no_universal_rules(tmp_path: Path) -> None:
    # Intersecting no traces would otherwise yield every rule rather than none.
    from topology_lantern.compare import _universal_rules

    empty = generate_candidates(str(BASE))
    assert _universal_rules(empty)
    from dataclasses import replace

    assert _universal_rules(replace(empty, candidates=())) == set()


def test_an_empty_ledger_is_a_usable_default() -> None:
    ledger = SearchLedger()
    assert ledger.rejected == 0
    assert ledger.rules.closed_off == frozenset()
    assert ledger.as_dict()["rejected"] == 0


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


def test_the_command_compares_two_specifications(tmp_path: Path, capsys) -> None:
    other = spec_with(tmp_path, "s", allow_resistive_bias=False)
    assert main(["diff", str(BASE), str(other)]) == 0
    output = capsys.readouterr().out
    assert "topologies in both" in output
    assert "tail.resistor" in output


def test_the_command_writes_json_on_request(tmp_path: Path) -> None:
    other = spec_with(tmp_path, "s", allow_resistive_bias=False)
    target = tmp_path / "diff.json"
    assert main(["diff", str(BASE), str(other), "--format", "json", "--output", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["rules"]["only_left"] == ["tail.resistor"]


def test_the_command_honours_a_shared_limit(tmp_path: Path, capsys) -> None:
    assert main(["diff", str(BASE), str(BASE), "--limit", "2"]) == 0
    assert "stopped before exhausting" in capsys.readouterr().out


def test_the_command_adds_the_ledger_to_a_report_on_request(tmp_path: Path, capsys) -> None:
    assert main(["generate", str(BASE), "--format", "json", "--ledger"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "ledger" in payload


def test_the_command_leaves_the_report_alone_by_default(capsys) -> None:
    assert main(["generate", str(BASE), "--format", "json"]) == 0
    assert "ledger" not in json.loads(capsys.readouterr().out)
