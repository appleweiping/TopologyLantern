from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from topology_lantern.search import generate_candidates
from topology_lantern.sequence_baseline import (
    BaselineEvaluationReport,
    CompactRuleSequence,
    SequenceBaselineError,
    build_sequence_examples,
    encode_rule_sequence,
    evaluate_sequence_baseline,
    evaluation_report_json,
    load_sequence_checkpoint,
    parse_sequence_checkpoint,
    replay_compact_sequence,
    sample_report_json,
    sample_sequence_baseline,
    sequence_checkpoint_json,
    spec_lineage_id,
    split_sequence_examples,
    train_sequence_baseline,
)
from topology_lantern.spec import DesignSpec, SearchLimits
from topology_lantern.types import DeviceKind, ReplayError


def spec(name: str, *, voltage: float = 1.8, **updates: object) -> DesignSpec:
    value: dict[str, object] = {"name": name, "supply_voltage": voltage}
    value.update(updates)
    return DesignSpec.from_mapping(value)


def test_compact_sequence_is_strict_and_replays_the_exact_topology() -> None:
    selected = spec("sequence")
    candidate = generate_candidates(selected, limit=1).candidates[0]
    encoded = encode_rule_sequence(selected, candidate)
    assert CompactRuleSequence.parse(encoded.as_text()) == encoded
    replayed = replay_compact_sequence(selected, encoded.as_text())
    assert replayed.signature == candidate.signature
    assert replayed.topology == candidate.topology
    assert tuple(step.rule_id for step in replayed.trace) == encoded.rule_ids


@pytest.mark.parametrize(
    "value",
    [
        "",
        " tlrs1|" + "0" * 64 + "|" + "0" * 64 + "|input.diff_pair",
        "tlrs2|" + "0" * 64 + "|" + "0" * 64 + "|input.diff_pair",
        "tlrs1|bad|" + "0" * 64 + "|input.diff_pair",
        "tlrs1|" + "0" * 64 + "|bad|input.diff_pair",
        "tlrs1|" + "0" * 64 + "|" + "0" * 64 + "|",
        "tlrs1|" + "0" * 64 + "|" + "0" * 64 + "|unknown.rule",
        "tlrs1|" + "0" * 64 + "|" + "0" * 64 + "|" + ",".join(["output.direct"] * 65),
        "x" * 8_193,
    ],
)
def test_compact_sequence_rejects_malformed_or_unbounded_text(value: str) -> None:
    with pytest.raises(SequenceBaselineError):
        CompactRuleSequence.parse(value)


def test_replay_rejects_spec_signature_and_incomplete_sequence_mismatches() -> None:
    selected = spec("sequence")
    other = spec("other", voltage=2.0)
    candidate = generate_candidates(selected, limit=1).candidates[0]
    encoded = encode_rule_sequence(selected, candidate)
    with pytest.raises(SequenceBaselineError, match="different specification"):
        replay_compact_sequence(other, encoded)
    with pytest.raises(SequenceBaselineError, match="signature"):
        replay_compact_sequence(
            selected,
            replace(encoded, candidate_signature="0" * 64),
        )
    with pytest.raises(SequenceBaselineError, match="unresolved obligations"):
        replay_compact_sequence(selected, replace(encoded, rule_ids=encoded.rule_ids[:1]))
    with pytest.raises(ReplayError):
        encode_rule_sequence(selected, replace(candidate, signature="0" * 64))
    with pytest.raises(SequenceBaselineError, match="unknown rule"):
        replay_compact_sequence(selected, replace(encoded, rule_ids=("unknown.rule",)))
    with pytest.raises(SequenceBaselineError, match="must be text"):
        replay_compact_sequence(selected, object())  # type: ignore[arg-type]


def test_polarity_augmentation_is_grouped_and_replay_verified() -> None:
    selected = spec("augment")
    examples = build_sequence_examples([selected], augment_polarity=True, limit=2)
    assert {example.variant for example in examples} == {"base", "polarity_dual"}
    assert {example.lineage_id for example in examples} == {spec_lineage_id(selected)}
    assert len({example.spec.fingerprint() for example in examples}) == 2
    for example in examples:
        assert replay_compact_sequence(example.spec, example.sequence).signature == (
            example.sequence.candidate_signature
        )

    nmos_only = spec(
        "no-dual",
        allowed_devices=["nmos", "resistor", "capacitor", "current_source"],
        load_preference="resistive",
    )
    assert {
        item.variant for item in build_sequence_examples([nmos_only], augment_polarity=True)
    } == {"base"}


def test_lineage_ignores_names_device_order_and_polarity_but_not_intent() -> None:
    left = spec("left")
    right = spec(
        "right",
        polarity="pmos_input",
        allowed_devices=[item.value for item in reversed(tuple(DeviceKind))],
    )
    changed = spec("changed", voltage=2.0)
    assert spec_lineage_id(left) == spec_lineage_id(right)
    assert spec_lineage_id(left) != spec_lineage_id(changed)


def test_example_builder_rejects_bad_bounds_and_empty_search() -> None:
    selected = spec("bounded")
    for specs, limit in (([], None), ([selected] * 129, None), ([selected], 0)):
        with pytest.raises(SequenceBaselineError):
            build_sequence_examples(specs, limit=limit)
    with pytest.raises(SequenceBaselineError, match="specs"):
        build_sequence_examples("not-a-sequence")  # type: ignore[arg-type]
    with pytest.raises(SequenceBaselineError, match="boolean"):
        build_sequence_examples([selected], augment_polarity=1)  # type: ignore[arg-type]
    impossible = spec(
        "no compensation device",
        require_compensation=True,
        allowed_devices=["nmos", "pmos", "resistor", "current_source"],
    )
    with pytest.raises(SequenceBaselineError, match="no valid candidates"):
        build_sequence_examples([impossible])


def test_split_is_deterministic_and_never_splits_a_lineage() -> None:
    examples = build_sequence_examples(
        [spec("one", voltage=1.2), spec("two", voltage=1.8), spec("three", voltage=2.4)],
        augment_polarity=True,
        limit=2,
    )
    first = split_sequence_examples(examples, heldout_fraction=0.34, seed=17)
    assert first == split_sequence_examples(examples, heldout_fraction=0.34, seed=17)
    training, heldout = first
    assert {item.lineage_id for item in training}.isdisjoint(item.lineage_id for item in heldout)
    for lineage in {item.lineage_id for item in examples}:
        assert all(item in training for item in examples if item.lineage_id == lineage) or all(
            item in heldout for item in examples if item.lineage_id == lineage
        )


@pytest.mark.parametrize("fraction", [True, 0, 1, -0.1, float("inf"), "0.2"])
def test_split_rejects_invalid_fraction(fraction: object) -> None:
    examples = build_sequence_examples([spec("one")], limit=1)
    with pytest.raises(SequenceBaselineError):
        split_sequence_examples(examples, heldout_fraction=fraction)  # type: ignore[arg-type]


def test_split_requires_two_valid_lineages_and_a_valid_seed() -> None:
    examples = build_sequence_examples([spec("one")], limit=1)
    with pytest.raises(SequenceBaselineError, match="two lineages"):
        split_sequence_examples(examples)
    with pytest.raises(SequenceBaselineError, match="seed"):
        split_sequence_examples(
            [*examples, replace(examples[0], lineage_id="sha256:" + "1" * 64)], seed=True
        )


def test_training_is_deterministic_and_rejects_augmentation_leakage() -> None:
    examples = build_sequence_examples(
        [spec("train", voltage=1.2), spec("heldout", voltage=2.4)],
        augment_polarity=True,
        limit=3,
    )
    training, heldout = split_sequence_examples(examples, heldout_fraction=0.5, seed=3)
    checkpoint = train_sequence_baseline(training, heldout=heldout)
    assert checkpoint == train_sequence_baseline(training, heldout=heldout)
    assert sum(
        transition.count
        for transition in checkpoint.transitions
        if transition.previous == "<start>"
    ) == len(training)
    with pytest.raises(SequenceBaselineError, match="lineage leakage"):
        train_sequence_baseline(training, heldout=training[:1])
    with pytest.raises(SequenceBaselineError, match="non-empty"):
        train_sequence_baseline([])


def test_training_revalidates_public_examples() -> None:
    example = build_sequence_examples([spec("valid")], limit=1)[0]
    malformed = [
        replace(example, lineage_id="bad"),
        replace(example, variant="unknown"),
        replace(example, sequence=replace(example.sequence, candidate_signature="0" * 64)),
    ]
    for item in malformed:
        with pytest.raises(SequenceBaselineError):
            train_sequence_baseline([item])
    with pytest.raises(SequenceBaselineError, match="invalid example"):
        train_sequence_baseline([object()])  # type: ignore[list-item]


def test_checkpoint_round_trip_tamper_detection_and_bounded_loader(tmp_path: Path) -> None:
    examples = build_sequence_examples([spec("checkpoint")], limit=3)
    checkpoint = train_sequence_baseline(examples)
    compact = sequence_checkpoint_json(checkpoint)
    pretty = sequence_checkpoint_json(checkpoint, pretty=True)
    assert '\n  "' in pretty
    assert parse_sequence_checkpoint(json.loads(compact)) == checkpoint
    path = tmp_path / "checkpoint.json"
    path.write_text(compact, encoding="utf-8")
    assert load_sequence_checkpoint(path) == checkpoint

    tampered = json.loads(compact)
    tampered["sequence_count"] += 1
    with pytest.raises(SequenceBaselineError):
        parse_sequence_checkpoint(tampered)
    with pytest.raises(SequenceBaselineError, match="digest"):
        sequence_checkpoint_json(replace(checkpoint, checkpoint_sha256="0" * 64))
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SequenceBaselineError, match="invalid JSON"):
        load_sequence_checkpoint(path)


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "wrong"},
        {"version": True},
        {"model_kind": "neural-network"},
        {"sequence_count": 0},
        {"training_lineages": []},
        {"training_spec_fingerprints": ["bad"]},
        {"training_sequence_sha256": []},
        {"transitions": []},
        {"checkpoint_sha256": "bad"},
        {"extra": 1},
    ],
)
def test_checkpoint_shape_is_strict(mutation: dict[str, object]) -> None:
    checkpoint = train_sequence_baseline(build_sequence_examples([spec("strict")], limit=1))
    value = checkpoint.as_dict()
    value.update(mutation)
    with pytest.raises(SequenceBaselineError):
        parse_sequence_checkpoint(value)


def test_checkpoint_transition_shape_order_and_accounting_are_strict() -> None:
    checkpoint = train_sequence_baseline(build_sequence_examples([spec("strict")], limit=3))
    variants = []
    duplicate = checkpoint.as_dict()
    duplicate["transitions"] = [*duplicate["transitions"], duplicate["transitions"][0]]
    variants.append(duplicate)
    reversed_transitions = checkpoint.as_dict()
    reversed_transitions["transitions"] = list(reversed(reversed_transitions["transitions"]))
    variants.append(reversed_transitions)
    unknown = checkpoint.as_dict()
    unknown["transitions"] = [
        {**unknown["transitions"][0], "next_rule": "unknown.rule"},
        *unknown["transitions"][1:],
    ]
    variants.append(unknown)
    bad_count = checkpoint.as_dict()
    bad_count["transitions"] = [
        {**bad_count["transitions"][0], "count": 0},
        *bad_count["transitions"][1:],
    ]
    variants.append(bad_count)
    for value in variants:
        with pytest.raises(SequenceBaselineError):
            parse_sequence_checkpoint(value)


def test_sampling_is_seeded_budget_exact_and_constraint_valid() -> None:
    training = build_sequence_examples([spec("training", voltage=1.2)], limit=8)
    checkpoint = train_sequence_baseline(training)
    target = spec("target", voltage=2.4)
    first = sample_sequence_baseline(checkpoint, target, draws=12, seed=42)
    assert first == sample_sequence_baseline(checkpoint, target, draws=12, seed=42)
    assert first.requested_draws == len(first.draws) == 12
    assert [draw.draw_index for draw in first.draws] == list(range(12))
    assert all(draw.rejection is None for draw in first.draws)
    for draw in first.draws:
        assert draw.compact_sequence is not None
        assert (
            replay_compact_sequence(target, draw.compact_sequence).candidate_id == draw.candidate_id
        )
    payload = json.loads(sample_report_json(first, pretty=True))
    assert payload["requested_draws"] == 12
    assert payload["valid_draws"] == 12
    assert "not an analog-performance" in payload["disclaimer"]


def test_sampling_reports_constrained_rejections_without_fabricating_sequences() -> None:
    checkpoint = train_sequence_baseline(build_sequence_examples([spec("training")], limit=2))
    shallow = replace(spec("shallow"), limits=SearchLimits(max_depth=1))
    shallow.validate()
    depth = sample_sequence_baseline(checkpoint, shallow, draws=2)
    assert {draw.rejection for draw in depth.draws} == {"depth_limit"}
    assert all(draw.compact_sequence is None for draw in depth.draws)

    no_tail = spec(
        "no-tail",
        allow_resistive_bias=False,
        allowed_devices=["nmos", "pmos", "resistor", "capacitor"],
    )
    missing = sample_sequence_baseline(checkpoint, no_tail, draws=1)
    assert missing.draws[0].rejection == "no_applicable_rule"

    tiny = replace(spec("tiny"), limits=SearchLimits(max_devices=1))
    tiny.validate()
    partial = sample_sequence_baseline(checkpoint, tiny, draws=1)
    assert partial.draws[0].rejection == "partial_constraint"


@pytest.mark.parametrize(("draws", "seed"), [(0, 0), (10_001, 0), (1, True), (1, 2**63)])
def test_sampling_rejects_invalid_budget_seed_or_checkpoint(draws: int, seed: int) -> None:
    checkpoint = train_sequence_baseline(build_sequence_examples([spec("training")], limit=1))
    with pytest.raises(SequenceBaselineError):
        sample_sequence_baseline(checkpoint, spec("target", voltage=2.0), draws=draws, seed=seed)
    if draws == 1 and seed is True:
        with pytest.raises(SequenceBaselineError, match="digest"):
            sample_sequence_baseline(
                replace(checkpoint, checkpoint_sha256="0" * 64),
                spec("target", voltage=2.0),
                draws=1,
            )


def test_heldout_evaluation_reports_validity_exact_match_and_coverage() -> None:
    all_examples = build_sequence_examples(
        [spec("train", voltage=1.2), spec("heldout", voltage=2.4)],
        augment_polarity=True,
        limit=8,
    )
    training, heldout = split_sequence_examples(all_examples, heldout_fraction=0.5, seed=1)
    checkpoint = train_sequence_baseline(training, heldout=heldout)
    report = evaluate_sequence_baseline(checkpoint, heldout, draws_per_spec=16, seed=8)
    assert report.requested_draws == report.heldout_specifications * 16
    assert 0 <= report.exact_sequence_draws <= report.valid_draws <= report.requested_draws
    payload = json.loads(evaluation_report_json(report, pretty=True))
    assert payload["leakage_check"] == "passed_lineage_disjoint"
    assert 0 <= payload["constraint_valid_rate"] <= 1
    assert 0 <= payload["exact_sequence_rate"] <= 1
    assert 0 <= payload["heldout_sequence_coverage"] <= 1
    assert "no simulated performance" in payload["disclaimer"]


def test_evaluation_rejects_leakage_empty_data_and_excess_budget() -> None:
    examples = build_sequence_examples([spec("same")], limit=1)
    checkpoint = train_sequence_baseline(examples)
    with pytest.raises(SequenceBaselineError, match="lineage leakage"):
        evaluate_sequence_baseline(checkpoint, examples)
    unrelated = build_sequence_examples(
        [spec("other", voltage=2.4), spec("second", voltage=3.0)],
        limit=1,
    )
    with pytest.raises(SequenceBaselineError, match="heldout"):
        evaluate_sequence_baseline(checkpoint, [])
    with pytest.raises(SequenceBaselineError, match="total draws"):
        evaluate_sequence_baseline(checkpoint, unrelated, draws_per_spec=10_000)
    with pytest.raises(SequenceBaselineError, match="draws_per_spec"):
        evaluate_sequence_baseline(checkpoint, unrelated, draws_per_spec=0)


def test_evaluation_report_rates_use_explicit_denominators() -> None:
    report = BaselineEvaluationReport("0" * 64, 1, 1, 2, 4, 3, 2, 1).as_dict()
    assert report["constraint_valid_rate"] == 0.75
    assert report["exact_sequence_rate"] == 0.5
    assert report["heldout_sequence_coverage"] == 0.5


def test_sequence_baseline_documents_validate_against_public_schemas() -> None:
    repository = Path(__file__).parents[1]
    training_examples = build_sequence_examples([spec("train", voltage=1.2)], limit=2)
    heldout_examples = build_sequence_examples([spec("heldout", voltage=2.4)], limit=2)
    checkpoint = train_sequence_baseline(training_examples, heldout=heldout_examples)
    samples = sample_sequence_baseline(checkpoint, heldout_examples[0].spec, draws=2)
    evaluation = evaluate_sequence_baseline(
        checkpoint,
        heldout_examples,
        draws_per_spec=2,
    )
    documents = {
        "rule-sequence-checkpoint-1.schema.json": checkpoint.as_dict(),
        "rule-sequence-samples-1.schema.json": samples.as_dict(),
        "rule-sequence-evaluation-1.schema.json": evaluation.as_dict(),
    }
    for name, document in documents.items():
        schema = json.loads((repository / "docs" / "schemas" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
