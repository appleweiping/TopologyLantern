"""What changed between two searches, and why.

Iterating on a specification is the ordinary workflow, and every `generate` run
was an island: two reports could be read side by side but not against each
other. Candidate numbering is no help, because it follows the ranking and moves
whenever anything else does.

The identifier-independent topology signature is what makes a comparison
possible. Two runs that build the same graph produce the same signature
regardless of how nets were named or which rule order reached it, so topologies
can be matched across runs by structure rather than by position.

For a topology that one run produced and the other did not, the answer comes in
two strengths and they are not interchangeable. When the second search built
the graph and refused it, the refusal names the constraints that failed, and
that is a fact. When the second search never reached the graph, nothing here
can say which pruned branch would have led to it, and this reports exactly
that. A search that stopped at its limits is a third case again, and one worth
knowing before reading anything into an absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from topology_lantern.ledger import RefusedTopology
from topology_lantern.types import Candidate, GenerationResult


class Absence(StrEnum):
    """How firmly the other run's failure to produce a topology is explained."""

    REFUSED = "refused"
    RULE_CLOSED = "rule_closed"
    LACKS_UNIVERSAL = "lacks_universal"
    NOT_REACHED = "not_reached"


@dataclass(frozen=True, slots=True)
class SharedTopology:
    """A topology both runs produced, and where each of them ranked it."""

    signature: str
    candidate_id: str
    left_position: int
    right_position: int
    left_pareto_rank: int
    right_pareto_rank: int

    @property
    def moved(self) -> int:
        """Positions gained in the right run; positive means it ranked higher."""

        return self.left_position - self.right_position

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "candidate_id": self.candidate_id,
            "left_position": self.left_position,
            "right_position": self.right_position,
            "left_pareto_rank": self.left_pareto_rank,
            "right_pareto_rank": self.right_pareto_rank,
            "moved": self.moved,
        }


@dataclass(frozen=True, slots=True)
class AbsentTopology:
    """A topology one run produced and the other did not."""

    signature: str
    candidate_id: str
    position: int
    absence: Absence
    refusal: RefusedTopology | None
    blocked_by: tuple[str, ...] = ()
    lacks: tuple[str, ...] = ()

    @property
    def codes(self) -> tuple[str, ...]:
        return self.refusal.codes if self.refusal else ()

    def explanation(self, *, other_truncated: bool) -> str:
        """Say why the other run lacks this topology, at the strength available.

        A refusal is a fact about a graph that was built. A closed-off rule is a
        fact about this topology: its own trace uses a rule the other
        specification governed and never permitted, so that run could not have
        constructed it.

        A rule every topology of the other run carries and this one lacks is
        weaker than either, and is worded as what it is -- an observation about
        that run's output, not a rule read out of its specification. It is what
        a newly required stage looks like from here, since a requirement adds an
        obligation rather than closing a rule off.

        Anything else is an absence with no cause to offer, and saying so is
        better than picking one.
        """

        if self.refusal is not None:
            codes = ", ".join(self.codes) or "no error-level violation recorded"
            return f"built and refused: {codes}"
        if self.blocked_by:
            rules = ", ".join(self.blocked_by)
            return (
                f"cannot exist under the other specification, which closed off "
                f"{rules}; this topology uses it"
            )
        if self.lacks:
            rules = ", ".join(self.lacks)
            return f"every topology the other run produced uses {rules}, and this one does not"
        if other_truncated:
            return (
                "not produced; the other run's refusal record was full, so it may "
                "have been refused without being recorded"
            )
        return "not produced, and the other run never built it"

    def as_dict(self, *, other_truncated: bool) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "candidate_id": self.candidate_id,
            "position": self.position,
            "absence": self.absence.value,
            "codes": list(self.codes),
            "blocked_by": list(self.blocked_by),
            "lacks": list(self.lacks),
            "explanation": self.explanation(other_truncated=other_truncated),
            "refusal": self.refusal.as_dict() if self.refusal else None,
        }


@dataclass(frozen=True, slots=True)
class ResultDiff:
    """Two candidate sets matched by structure."""

    left_fingerprint: str
    right_fingerprint: str
    shared: tuple[SharedTopology, ...]
    only_left: tuple[AbsentTopology, ...]
    only_right: tuple[AbsentTopology, ...]
    left_exhausted: bool
    right_exhausted: bool
    left_truncated: bool
    right_truncated: bool
    rules_only_left: tuple[str, ...] = ()
    rules_only_right: tuple[str, ...] = ()
    closed_off_left: tuple[str, ...] = ()
    closed_off_right: tuple[str, ...] = ()

    @property
    def unchanged(self) -> bool:
        """Whether both runs produced the same topologies in the same order."""

        return (
            not self.only_left
            and not self.only_right
            and all(item.moved == 0 for item in self.shared)
        )

    @property
    def same_spec(self) -> bool:
        return self.left_fingerprint == self.right_fingerprint

    @property
    def reordered(self) -> tuple[SharedTopology, ...]:
        return tuple(item for item in self.shared if item.moved != 0)

    @property
    def limited(self) -> bool:
        """Whether either search stopped before exhausting its space.

        An absence from a search that stopped at its limits says less than an
        absence from one that finished: the topology may simply lie beyond the
        budget.
        """

        return not (self.left_exhausted and self.right_exhausted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "left": {
                "spec_fingerprint": self.left_fingerprint,
                "exhausted": self.left_exhausted,
                "refusal_record_truncated": self.left_truncated,
            },
            "right": {
                "spec_fingerprint": self.right_fingerprint,
                "exhausted": self.right_exhausted,
                "refusal_record_truncated": self.right_truncated,
            },
            "same_spec": self.same_spec,
            "unchanged": self.unchanged,
            "searches_limited": self.limited,
            "rules": {
                "only_left": list(self.rules_only_left),
                "only_right": list(self.rules_only_right),
                "closed_off_left": list(self.closed_off_left),
                "closed_off_right": list(self.closed_off_right),
            },
            "shared": [item.as_dict() for item in self.shared],
            "only_left": [
                item.as_dict(other_truncated=self.right_truncated) for item in self.only_left
            ],
            "only_right": [
                item.as_dict(other_truncated=self.left_truncated) for item in self.only_right
            ],
        }


def _positions(result: GenerationResult) -> dict[str, tuple[int, Candidate]]:
    return {
        candidate.signature: (index + 1, candidate)
        for index, candidate in enumerate(result.candidates)
    }


def _universal_rules(result: GenerationResult) -> set[str]:
    """Rules that appear in the trace of every topology one run produced.

    A specification that starts requiring a stage adds an obligation, so the
    rule discharging it turns up everywhere rather than closing another rule
    off. An empty result has no universal rules rather than all of them.
    """

    if not result.candidates:
        return set()
    traces = [{step.rule_id for step in item.trace} for item in result.candidates]
    return set.intersection(*traces)


def _absent(
    entries: dict[str, tuple[int, Candidate]],
    present: dict[str, tuple[int, Candidate]],
    other: GenerationResult,
) -> tuple[AbsentTopology, ...]:
    missing = []
    closed = other.ledger.rules.closed_off
    universal = _universal_rules(other)
    for signature, (position, candidate) in entries.items():
        if signature in present:
            continue
        refusal = other.ledger.refusal_for(signature)
        # The trace names every rule that built this topology, so a rule the
        # other specification closed off is a proof it could not have been
        # built there -- not an inference from what happened to be absent.
        own = {step.rule_id for step in candidate.trace}
        blocked = tuple(sorted(own & closed))
        lacks = tuple(sorted(universal - own))
        if refusal is not None:
            absence = Absence.REFUSED
        elif blocked:
            absence = Absence.RULE_CLOSED
        elif lacks:
            absence = Absence.LACKS_UNIVERSAL
        else:
            absence = Absence.NOT_REACHED
        missing.append(
            AbsentTopology(
                signature=signature,
                candidate_id=candidate.candidate_id,
                position=position,
                absence=absence,
                refusal=refusal,
                blocked_by=blocked,
                lacks=lacks,
            )
        )
    return tuple(sorted(missing, key=lambda item: item.position))


def diff_results(left: GenerationResult, right: GenerationResult) -> ResultDiff:
    """Match two candidate sets by topology signature and explain the gaps.

    Signatures are identifier-independent, so a topology is recognized across
    runs whatever its nets were called and whichever rule order produced it.
    Positions are one-based ranks within each run.
    """

    left_index = _positions(left)
    right_index = _positions(right)
    shared = tuple(
        SharedTopology(
            signature=signature,
            candidate_id=candidate.candidate_id,
            left_position=position,
            right_position=right_index[signature][0],
            left_pareto_rank=candidate.pareto_rank,
            right_pareto_rank=right_index[signature][1].pareto_rank,
        )
        for signature, (position, candidate) in sorted(
            left_index.items(), key=lambda item: item[1][0]
        )
        if signature in right_index
    )
    left_rules = left.ledger.rules
    right_rules = right.ledger.rules
    return ResultDiff(
        left_fingerprint=left.spec_fingerprint,
        right_fingerprint=right.spec_fingerprint,
        shared=shared,
        only_left=_absent(left_index, right_index, right),
        only_right=_absent(right_index, left_index, left),
        left_exhausted=left.exhausted,
        right_exhausted=right.exhausted,
        left_truncated=left.ledger.truncated,
        right_truncated=right.ledger.truncated,
        rules_only_left=tuple(sorted(left_rules.used - right_rules.used)),
        rules_only_right=tuple(sorted(right_rules.used - left_rules.used)),
        closed_off_left=tuple(sorted(left_rules.closed_off)),
        closed_off_right=tuple(sorted(right_rules.closed_off)),
    )


def render_diff(diff: ResultDiff) -> str:
    """Render a comparison for a terminal, leading with what is uncertain."""

    lines: list[str] = []
    if diff.same_spec:
        lines.append(
            "Both runs used the same specification fingerprint; any difference "
            "comes from the search limits rather than the specification."
        )
    if diff.limited:
        lines.append(
            "At least one search stopped before exhausting its space, so an "
            "absence may mean the topology was out of budget rather than refused."
        )
    if lines:
        lines.append("")

    if diff.unchanged:
        lines.append(f"No change: {len(diff.shared)} topologies, same order.")
        return "\n".join(lines)

    lines.append(
        f"{len(diff.shared)} topologies in both, "
        f"{len(diff.only_left)} only on the left, "
        f"{len(diff.only_right)} only on the right."
    )

    for label, rules in (
        ("Rules only the left specification permits", diff.rules_only_left),
        ("Rules only the right specification permits", diff.rules_only_right),
    ):
        if rules:
            lines.append("")
            lines.append(f"{label}: {', '.join(rules)}")

    moved = diff.reordered
    if moved:
        lines.append("")
        lines.append("Reordered:")
        for item in moved:
            direction = "up" if item.moved > 0 else "down"
            lines.append(
                f"  {item.candidate_id}  {item.left_position} -> "
                f"{item.right_position} ({direction} {abs(item.moved)})"
            )

    for title, entries, truncated in (
        ("Lost (present on the left only)", diff.only_left, diff.right_truncated),
        ("Gained (present on the right only)", diff.only_right, diff.left_truncated),
    ):
        if not entries:
            continue
        lines.append("")
        lines.append(f"{title}:")
        for absent in entries:
            lines.append(f"  {absent.candidate_id}  at position {absent.position}")
            lines.append(f"    {absent.explanation(other_truncated=truncated)}")
    return "\n".join(lines)
