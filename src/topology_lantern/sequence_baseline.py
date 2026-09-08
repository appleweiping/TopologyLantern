"""Auditable statistical baseline over replayable topology-rule sequences.

This module is deliberately not a learned analog-performance model. It counts
rule transitions in verified candidates and samples only through the existing
rewrite predicates and structural constraints.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from topology_lantern.canonical import state_signature
from topology_lantern.constraints import has_error, partial_violations
from topology_lantern.explain import replay_rule_ids, verify_replay
from topology_lantern.obligations import initial_state
from topology_lantern.rules import RULES, applicable_rules
from topology_lantern.search import candidate_from_state, generate_candidates
from topology_lantern.spec import DesignSpec, _load_json_object, load_spec
from topology_lantern.types import Candidate, DeviceKind, LanternError

_SEQUENCE_PREFIX = "tlrs1"
_START = "<start>"
_MODEL_KIND = "laplace-bigram-rule-baseline"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LINEAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RULE_IDS = frozenset(rule.rule_id for rule in RULES)
_MAX_SEQUENCE_CHARACTERS = 8_192
_MAX_RULES = 64
_MAX_SPECS = 128
_MAX_EXAMPLES = 10_000
_MAX_DRAWS = 10_000


class SequenceBaselineError(LanternError, ValueError):
    """A rule sequence, data split, checkpoint, or sample is invalid."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _positive_integer(value: object, context: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SequenceBaselineError(f"{context} must be an integer in [1, {maximum}]")
    return value


def _seed(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**63) <= value < 2**63:
        raise SequenceBaselineError("seed must be a signed 64-bit integer")
    return value


def _digest(value: object, context: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SequenceBaselineError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _lineage(value: object, context: str) -> str:
    if not isinstance(value, str) or _LINEAGE.fullmatch(value) is None:
        raise SequenceBaselineError(f"{context} must be a sha256: lineage ID")
    return value


@dataclass(frozen=True, slots=True)
class CompactRuleSequence:
    """A compact sequence that deterministically replays under one specification."""

    spec_fingerprint: str
    candidate_signature: str
    rule_ids: tuple[str, ...]

    def as_text(self) -> str:
        return "|".join(
            (
                _SEQUENCE_PREFIX,
                self.spec_fingerprint,
                self.candidate_signature,
                ",".join(self.rule_ids),
            )
        )

    @classmethod
    def parse(cls, value: str) -> CompactRuleSequence:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_SEQUENCE_CHARACTERS
            or value.strip() != value
        ):
            raise SequenceBaselineError("compact sequence is empty, oversized, or untrimmed")
        fields = value.split("|")
        if len(fields) != 4 or fields[0] != _SEQUENCE_PREFIX:
            raise SequenceBaselineError("compact sequence does not use the tlrs1 contract")
        fingerprint = _digest(fields[1], "sequence specification fingerprint")
        signature = _digest(fields[2], "sequence candidate signature")
        rules = tuple(fields[3].split(",")) if fields[3] else ()
        if not rules or len(rules) > _MAX_RULES:
            raise SequenceBaselineError(f"compact sequence must contain 1 to {_MAX_RULES} rules")
        unknown = sorted(set(rules) - _RULE_IDS)
        if unknown:
            raise SequenceBaselineError(f"compact sequence contains unknown rule {unknown[0]!r}")
        return cls(fingerprint, signature, rules)


def encode_rule_sequence(spec: DesignSpec, candidate: Candidate) -> CompactRuleSequence:
    """Encode a candidate only after its complete trace has been replay-verified."""
    selected = load_spec(spec)
    selected.validate()
    verify_replay(selected, candidate)
    return CompactRuleSequence(
        selected.fingerprint(),
        candidate.signature,
        tuple(step.rule_id for step in candidate.trace),
    )


def replay_compact_sequence(spec: DesignSpec, value: CompactRuleSequence | str) -> Candidate:
    """Recover and verify the candidate represented by a compact rule sequence."""
    selected = load_spec(spec)
    selected.validate()
    if isinstance(value, str):
        sequence = CompactRuleSequence.parse(value)
    elif isinstance(value, CompactRuleSequence):
        try:
            sequence = CompactRuleSequence.parse(value.as_text())
        except (AttributeError, TypeError) as error:
            raise SequenceBaselineError("compact sequence object has invalid fields") from error
    else:
        raise SequenceBaselineError("compact sequence must be text or CompactRuleSequence")
    if sequence.spec_fingerprint != selected.fingerprint():
        raise SequenceBaselineError("compact sequence belongs to a different specification")
    state = replay_rule_ids(selected, sequence.rule_ids)
    if state.obligations:
        raise SequenceBaselineError("compact sequence ends with unresolved obligations")
    candidate = candidate_from_state(selected, state)
    if candidate is None:
        raise SequenceBaselineError("compact sequence violates final structural constraints")
    if candidate.signature != sequence.candidate_signature:
        raise SequenceBaselineError("compact sequence candidate signature does not match replay")
    return candidate


def spec_lineage_id(spec: DesignSpec) -> str:
    """Group cosmetic renames and polarity-dual augmentation before splitting."""
    selected = load_spec(spec)
    selected.validate()
    payload = selected.as_dict()
    payload.pop("name")
    payload["polarity"] = "polarity-dual-family"
    allowed = payload["allowed_devices"]
    if isinstance(allowed, list):
        payload["allowed_devices"] = sorted(allowed)
    return f"sha256:{sha256(_canonical_json(payload).encode('ascii')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SequenceExample:
    """One verified sequence with an augmentation-safe lineage."""

    lineage_id: str
    variant: str
    spec: DesignSpec
    sequence: CompactRuleSequence


def _selected_spec(value: DesignSpec | Mapping[str, Any] | str | Path) -> DesignSpec:
    selected = DesignSpec.from_json(value) if isinstance(value, Path) else load_spec(value)
    selected.validate()
    return selected


def _spec_variants(spec: DesignSpec, augment_polarity: bool) -> tuple[tuple[str, DesignSpec], ...]:
    variants = [("base", spec)]
    if augment_polarity and {DeviceKind.NMOS, DeviceKind.PMOS}.issubset(spec.allowed_devices):
        dual = replace(
            spec,
            polarity="pmos_input" if spec.polarity == "nmos_input" else "nmos_input",
        )
        dual.validate()
        variants.append(("polarity_dual", dual))
    return tuple(variants)


def build_sequence_examples(
    specs: Sequence[DesignSpec | Mapping[str, Any] | str | Path],
    *,
    augment_polarity: bool = False,
    limit: int | None = None,
) -> tuple[SequenceExample, ...]:
    """Generate verified examples, keeping augmentations in their base lineage."""
    if not isinstance(augment_polarity, bool):
        raise SequenceBaselineError("augment_polarity must be a boolean")
    if isinstance(specs, str | bytes) or not specs or len(specs) > _MAX_SPECS:
        raise SequenceBaselineError(f"specs must contain 1 to {_MAX_SPECS} specifications")
    if limit is not None:
        _positive_integer(limit, "candidate limit", maximum=10_000)
    examples: dict[tuple[str, str, str], SequenceExample] = {}
    for raw_spec in specs:
        spec = _selected_spec(raw_spec)
        lineage = spec_lineage_id(spec)
        for variant, selected in _spec_variants(spec, augment_polarity):
            result = generate_candidates(selected, limit=limit)
            for candidate in result.candidates:
                sequence = encode_rule_sequence(selected, candidate)
                key = (lineage, selected.fingerprint(), sequence.as_text())
                examples[key] = SequenceExample(lineage, variant, selected, sequence)
                if len(examples) > _MAX_EXAMPLES:
                    raise SequenceBaselineError("sequence corpus exceeds the example limit")
    if not examples:
        raise SequenceBaselineError("sequence corpus contains no valid candidates")
    return tuple(examples[key] for key in sorted(examples))


def _validate_example(example: SequenceExample, context: str) -> None:
    if not isinstance(example, SequenceExample):
        raise SequenceBaselineError(f"{context} contains an invalid example object")
    _lineage(example.lineage_id, f"{context} lineage")
    if example.variant not in {"base", "polarity_dual"}:
        raise SequenceBaselineError(f"{context} has an unknown variant")
    if spec_lineage_id(example.spec) != example.lineage_id:
        raise SequenceBaselineError(f"{context} lineage does not match its specification")
    replay_compact_sequence(example.spec, example.sequence)


def split_sequence_examples(
    examples: Sequence[SequenceExample],
    *,
    heldout_fraction: float = 0.2,
    seed: int = 0,
) -> tuple[tuple[SequenceExample, ...], tuple[SequenceExample, ...]]:
    """Split whole lineages so no augmentation family crosses the boundary."""
    _seed(seed)
    if (
        isinstance(heldout_fraction, bool)
        or not isinstance(heldout_fraction, int | float)
        or not math.isfinite(float(heldout_fraction))
        or not 0 < heldout_fraction < 1
    ):
        raise SequenceBaselineError("heldout_fraction must be finite and strictly between 0 and 1")
    if not examples or len(examples) > _MAX_EXAMPLES:
        raise SequenceBaselineError(f"examples must contain 1 to {_MAX_EXAMPLES} items")
    for index, example in enumerate(examples):
        _validate_example(example, f"examples[{index}]")
    lineages = sorted({example.lineage_id for example in examples})
    if len(lineages) < 2:
        raise SequenceBaselineError("at least two lineages are required for a held-out split")
    ordered = sorted(
        lineages,
        key=lambda lineage: sha256(f"{seed}|{lineage}".encode()).hexdigest(),
    )
    heldout_count = max(1, min(len(ordered) - 1, math.ceil(len(ordered) * heldout_fraction)))
    heldout_lineages = frozenset(ordered[:heldout_count])
    training = tuple(example for example in examples if example.lineage_id not in heldout_lineages)
    heldout = tuple(example for example in examples if example.lineage_id in heldout_lineages)
    return training, heldout


@dataclass(frozen=True, slots=True)
class TransitionCount:
    previous: str
    next_rule: str
    count: int

    def as_dict(self) -> dict[str, object]:
        return {"previous": self.previous, "next_rule": self.next_rule, "count": self.count}


@dataclass(frozen=True, slots=True)
class SequenceBaselineCheckpoint:
    """Versioned sufficient statistics for the rule-sequence baseline."""

    sequence_count: int
    training_lineages: tuple[str, ...]
    training_spec_fingerprints: tuple[str, ...]
    training_sequence_sha256: tuple[str, ...]
    transitions: tuple[TransitionCount, ...]
    checkpoint_sha256: str

    def body_dict(self) -> dict[str, object]:
        return {
            "schema": "org.topology-lantern.rule-sequence-checkpoint",
            "version": 1,
            "model_kind": _MODEL_KIND,
            "sequence_count": self.sequence_count,
            "training_lineages": list(self.training_lineages),
            "training_spec_fingerprints": list(self.training_spec_fingerprints),
            "training_sequence_sha256": list(self.training_sequence_sha256),
            "transitions": [transition.as_dict() for transition in self.transitions],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.body_dict(), "checkpoint_sha256": self.checkpoint_sha256}

    def count_map(self) -> dict[tuple[str, str], int]:
        return {
            (transition.previous, transition.next_rule): transition.count
            for transition in self.transitions
        }


def _checkpoint_digest(checkpoint: SequenceBaselineCheckpoint) -> str:
    return sha256(_canonical_json(checkpoint.body_dict()).encode("ascii")).hexdigest()


def _revalidate_checkpoint(
    checkpoint: SequenceBaselineCheckpoint,
) -> SequenceBaselineCheckpoint:
    if not isinstance(checkpoint, SequenceBaselineCheckpoint):
        raise SequenceBaselineError("checkpoint must be a SequenceBaselineCheckpoint")
    try:
        return parse_sequence_checkpoint(checkpoint.as_dict())
    except (AttributeError, TypeError, ValueError) as error:
        if isinstance(error, SequenceBaselineError):
            raise
        raise SequenceBaselineError("checkpoint object has invalid fields") from error


def train_sequence_baseline(
    training: Sequence[SequenceExample],
    *,
    heldout: Sequence[SequenceExample] = (),
) -> SequenceBaselineCheckpoint:
    """Fit auditable bigram counts after rejecting lineage leakage."""
    if not training or len(training) > _MAX_EXAMPLES or len(heldout) > _MAX_EXAMPLES:
        raise SequenceBaselineError(
            "training must be non-empty and corpus limits must be respected"
        )
    for label, examples in (("training", training), ("heldout", heldout)):
        for index, example in enumerate(examples):
            _validate_example(example, f"{label}[{index}]")
    training_lineages = frozenset(example.lineage_id for example in training)
    heldout_lineages = frozenset(example.lineage_id for example in heldout)
    overlap = sorted(training_lineages & heldout_lineages)
    if overlap:
        raise SequenceBaselineError(f"training/held-out lineage leakage: {overlap[0]}")

    counts: Counter[tuple[str, str]] = Counter()
    sequence_hashes: set[str] = set()
    for example in training:
        previous = _START
        compact = example.sequence.as_text()
        sequence_hashes.add(sha256(compact.encode("ascii")).hexdigest())
        for rule_id in example.sequence.rule_ids:
            counts[(previous, rule_id)] += 1
            previous = rule_id
    transitions = tuple(
        TransitionCount(previous, next_rule, count)
        for (previous, next_rule), count in sorted(counts.items())
    )
    checkpoint = SequenceBaselineCheckpoint(
        sequence_count=len(training),
        training_lineages=tuple(sorted(training_lineages)),
        training_spec_fingerprints=tuple(
            sorted({example.spec.fingerprint() for example in training})
        ),
        training_sequence_sha256=tuple(sorted(sequence_hashes)),
        transitions=transitions,
        checkpoint_sha256="",
    )
    return replace(checkpoint, checkpoint_sha256=_checkpoint_digest(checkpoint))


def _exact(value: object, fields: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SequenceBaselineError(f"{context} has missing or unknown fields")
    return value


def _sorted_unique_strings(
    value: object,
    context: str,
    *,
    lineage: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_EXAMPLES:
        raise SequenceBaselineError(f"{context} must be a bounded non-empty array")
    result = tuple(_lineage(item, context) if lineage else _digest(item, context) for item in value)
    if result != tuple(sorted(set(result))):
        raise SequenceBaselineError(f"{context} must be sorted and unique")
    return result


def parse_sequence_checkpoint(value: Mapping[str, Any]) -> SequenceBaselineCheckpoint:
    """Strictly validate a serialized baseline checkpoint and its digest."""
    fields = {
        "schema",
        "version",
        "model_kind",
        "sequence_count",
        "training_lineages",
        "training_spec_fingerprints",
        "training_sequence_sha256",
        "transitions",
        "checkpoint_sha256",
    }
    raw = _exact(value, fields, "sequence checkpoint")
    if (
        raw["schema"] != "org.topology-lantern.rule-sequence-checkpoint"
        or raw["version"] != 1
        or isinstance(raw["version"], bool)
        or raw["model_kind"] != _MODEL_KIND
    ):
        raise SequenceBaselineError("unsupported sequence checkpoint schema, version, or model")
    sequence_count = _positive_integer(
        raw["sequence_count"], "checkpoint sequence_count", maximum=_MAX_EXAMPLES
    )
    lineages = _sorted_unique_strings(raw["training_lineages"], "training_lineages", lineage=True)
    fingerprints = _sorted_unique_strings(
        raw["training_spec_fingerprints"], "training_spec_fingerprints"
    )
    sequence_hashes = _sorted_unique_strings(
        raw["training_sequence_sha256"], "training_sequence_sha256"
    )
    transition_values = raw["transitions"]
    if (
        not isinstance(transition_values, list)
        or not transition_values
        or len(transition_values) > 100
    ):
        raise SequenceBaselineError("checkpoint transitions must be a bounded non-empty array")
    transitions: list[TransitionCount] = []
    keys: set[tuple[str, str]] = set()
    for index, item in enumerate(transition_values):
        transition = _exact(item, {"previous", "next_rule", "count"}, f"transitions[{index}]")
        previous = transition["previous"]
        next_rule = transition["next_rule"]
        if not isinstance(previous, str) or previous not in {*_RULE_IDS, _START}:
            raise SequenceBaselineError(f"transitions[{index}].previous is unknown")
        if not isinstance(next_rule, str) or next_rule not in _RULE_IDS:
            raise SequenceBaselineError(f"transitions[{index}].next_rule is unknown")
        count = _positive_integer(
            transition["count"], f"transitions[{index}].count", maximum=_MAX_EXAMPLES
        )
        key = (previous, next_rule)
        if key in keys:
            raise SequenceBaselineError("checkpoint contains a duplicate transition")
        keys.add(key)
        transitions.append(TransitionCount(previous, next_rule, count))
    if transitions != sorted(transitions, key=lambda item: (item.previous, item.next_rule)):
        raise SequenceBaselineError("checkpoint transitions must be canonically sorted")
    if sum(item.count for item in transitions if item.previous == _START) != sequence_count:
        raise SequenceBaselineError("checkpoint start counts do not match sequence_count")
    checkpoint = SequenceBaselineCheckpoint(
        sequence_count,
        lineages,
        fingerprints,
        sequence_hashes,
        tuple(transitions),
        _digest(raw["checkpoint_sha256"], "checkpoint_sha256"),
    )
    if checkpoint.checkpoint_sha256 != _checkpoint_digest(checkpoint):
        raise SequenceBaselineError("sequence checkpoint digest does not match its body")
    return checkpoint


def load_sequence_checkpoint(path: str | Path) -> SequenceBaselineCheckpoint:
    """Load a checkpoint with the repository's bounded strict JSON reader."""
    try:
        value = _load_json_object(path, "sequence checkpoint")
    except LanternError as error:
        raise SequenceBaselineError(str(error)) from error
    return parse_sequence_checkpoint(value)


def sequence_checkpoint_json(
    checkpoint: SequenceBaselineCheckpoint, *, pretty: bool = False
) -> str:
    """Serialize a checkpoint after verifying its content digest."""
    checkpoint = _revalidate_checkpoint(checkpoint)
    return (
        json.dumps(
            checkpoint.as_dict(),
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


@dataclass(frozen=True, slots=True)
class BaselineDraw:
    draw_index: int
    compact_sequence: str | None
    candidate_id: str | None
    rejection: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "draw_index": self.draw_index,
            "compact_sequence": self.compact_sequence,
            "candidate_id": self.candidate_id,
            "rejection": self.rejection,
        }


@dataclass(frozen=True, slots=True)
class BaselineSampleReport:
    checkpoint_sha256: str
    spec_fingerprint: str
    seed: int
    requested_draws: int
    draws: tuple[BaselineDraw, ...]

    def as_dict(self) -> dict[str, object]:
        valid = [draw for draw in self.draws if draw.compact_sequence is not None]
        return {
            "schema": "org.topology-lantern.rule-sequence-samples",
            "version": 1,
            "model_kind": _MODEL_KIND,
            "checkpoint_sha256": self.checkpoint_sha256,
            "spec_fingerprint": self.spec_fingerprint,
            "seed": self.seed,
            "requested_draws": self.requested_draws,
            "valid_draws": len(valid),
            "unique_sequences": len({draw.compact_sequence for draw in valid}),
            "draws": [draw.as_dict() for draw in self.draws],
            "disclaimer": (
                "Deterministic statistical baseline; not an analog-performance or trained ML model."
            ),
        }


def _weighted_rule(
    checkpoint: SequenceBaselineCheckpoint,
    previous: str,
    rule_ids: tuple[str, ...],
    *,
    spec: DesignSpec,
    state_key: str,
    seed: int,
    draw_index: int,
    step: int,
) -> str:
    counts = checkpoint.count_map()
    ordered = tuple(sorted(rule_ids))
    weights = tuple(1 + counts.get((previous, rule_id), 0) for rule_id in ordered)
    entropy = "|".join(
        (
            checkpoint.checkpoint_sha256,
            spec.fingerprint(),
            str(seed),
            str(draw_index),
            str(step),
            previous,
            state_key,
            ",".join(ordered),
        )
    )
    ticket = int(sha256(entropy.encode("ascii")).hexdigest(), 16) % sum(weights)
    for rule_id, weight in zip(ordered, weights, strict=True):
        if ticket < weight:
            return rule_id
        ticket -= weight
    raise AssertionError("weighted rule selection exhausted its positive weights")


def _draw(
    checkpoint: SequenceBaselineCheckpoint,
    spec: DesignSpec,
    *,
    seed: int,
    draw_index: int,
) -> BaselineDraw:
    state = initial_state(spec)
    previous = _START
    while state.obligations:
        if len(state.trace) >= spec.limits.max_depth:
            return BaselineDraw(draw_index, None, None, "depth_limit")
        if has_error(partial_violations(spec, state)):
            return BaselineDraw(draw_index, None, None, "partial_constraint")
        obligation = state.obligations[0]
        rules = applicable_rules(spec, state, obligation)
        if not rules:
            return BaselineDraw(draw_index, None, None, "no_applicable_rule")
        state_key = state_signature(
            state,
            max_permutations=spec.limits.max_canonical_permutations,
        )
        selected_id = _weighted_rule(
            checkpoint,
            previous,
            tuple(rule.rule_id for rule in rules),
            spec=spec,
            state_key=state_key,
            seed=seed,
            draw_index=draw_index,
            step=len(state.trace),
        )
        selected = next(rule for rule in rules if rule.rule_id == selected_id)
        state = selected.apply(spec, state, obligation)
        previous = selected_id
    candidate = candidate_from_state(spec, state)
    if candidate is None:
        return BaselineDraw(draw_index, None, None, "final_constraint")
    compact = encode_rule_sequence(spec, candidate).as_text()
    return BaselineDraw(draw_index, compact, candidate.candidate_id, None)


def sample_sequence_baseline(
    checkpoint: SequenceBaselineCheckpoint,
    spec: DesignSpec | Mapping[str, Any] | str | Path,
    *,
    draws: int,
    seed: int = 0,
) -> BaselineSampleReport:
    """Run exactly ``draws`` constrained, deterministic sampling attempts."""
    requested = _positive_integer(draws, "draws", maximum=_MAX_DRAWS)
    selected = _selected_spec(spec)
    selected_seed = _seed(seed)
    checkpoint = _revalidate_checkpoint(checkpoint)
    results = tuple(
        _draw(checkpoint, selected, seed=selected_seed, draw_index=index)
        for index in range(requested)
    )
    return BaselineSampleReport(
        checkpoint.checkpoint_sha256,
        selected.fingerprint(),
        selected_seed,
        requested,
        results,
    )


@dataclass(frozen=True, slots=True)
class BaselineEvaluationReport:
    checkpoint_sha256: str
    heldout_lineages: int
    heldout_specifications: int
    heldout_sequences: int
    requested_draws: int
    valid_draws: int
    exact_sequence_draws: int
    matched_heldout_sequences: int

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "org.topology-lantern.rule-sequence-evaluation",
            "version": 1,
            "model_kind": _MODEL_KIND,
            "checkpoint_sha256": self.checkpoint_sha256,
            "leakage_check": "passed_lineage_disjoint",
            "heldout_lineages": self.heldout_lineages,
            "heldout_specifications": self.heldout_specifications,
            "heldout_sequences": self.heldout_sequences,
            "requested_draws": self.requested_draws,
            "valid_draws": self.valid_draws,
            "exact_sequence_draws": self.exact_sequence_draws,
            "matched_heldout_sequences": self.matched_heldout_sequences,
            "constraint_valid_rate": self.valid_draws / self.requested_draws,
            "exact_sequence_rate": self.exact_sequence_draws / self.requested_draws,
            "heldout_sequence_coverage": (self.matched_heldout_sequences / self.heldout_sequences),
            "disclaimer": (
                "Held-out structural sequence metrics only; no simulated performance claim."
            ),
        }


def evaluate_sequence_baseline(
    checkpoint: SequenceBaselineCheckpoint,
    heldout: Sequence[SequenceExample],
    *,
    draws_per_spec: int = 16,
    seed: int = 0,
) -> BaselineEvaluationReport:
    """Measure constrained validity and exact sequence coverage on held-out lineages."""
    attempts_per_spec = _positive_integer(draws_per_spec, "draws_per_spec", maximum=_MAX_DRAWS)
    selected_seed = _seed(seed)
    checkpoint = _revalidate_checkpoint(checkpoint)
    if not heldout or len(heldout) > _MAX_EXAMPLES:
        raise SequenceBaselineError("heldout must be a bounded non-empty sequence")
    for index, example in enumerate(heldout):
        _validate_example(example, f"heldout[{index}]")
    heldout_lineages = frozenset(example.lineage_id for example in heldout)
    overlap = sorted(set(checkpoint.training_lineages) & heldout_lineages)
    if overlap:
        raise SequenceBaselineError(f"training/held-out lineage leakage: {overlap[0]}")
    specs: dict[str, DesignSpec] = {}
    expected: dict[str, set[str]] = {}
    for example in heldout:
        fingerprint = example.spec.fingerprint()
        specs[fingerprint] = example.spec
        expected.setdefault(fingerprint, set()).add(example.sequence.as_text())
    if len(specs) * attempts_per_spec > _MAX_DRAWS:
        raise SequenceBaselineError(f"evaluation cannot exceed {_MAX_DRAWS} total draws")
    valid = 0
    exact = 0
    matched: set[str] = set()
    for fingerprint in sorted(specs):
        report = sample_sequence_baseline(
            checkpoint,
            specs[fingerprint],
            draws=attempts_per_spec,
            seed=selected_seed,
        )
        for draw in report.draws:
            if draw.compact_sequence is None:
                continue
            valid += 1
            if draw.compact_sequence in expected[fingerprint]:
                exact += 1
                matched.add(draw.compact_sequence)
    heldout_sequence_count = len({item for values in expected.values() for item in values})
    return BaselineEvaluationReport(
        checkpoint.checkpoint_sha256,
        len(heldout_lineages),
        len(specs),
        heldout_sequence_count,
        len(specs) * attempts_per_spec,
        valid,
        exact,
        len(matched),
    )


def sample_report_json(report: BaselineSampleReport, *, pretty: bool = False) -> str:
    """Serialize deterministic samples as a versioned report."""
    return _report_json(report.as_dict(), pretty=pretty)


def evaluation_report_json(report: BaselineEvaluationReport, *, pretty: bool = False) -> str:
    """Serialize held-out metrics as a versioned report."""
    return _report_json(report.as_dict(), pretty=pretty)


def _report_json(value: Mapping[str, object], *, pretty: bool) -> str:
    return (
        json.dumps(
            value,
            indent=2 if pretty else None,
            sort_keys=True,
            separators=None if pretty else (",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )
