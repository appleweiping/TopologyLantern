"""What the search did with each rule, and why it discarded what it discarded.

The search reported `pruned_states` as a number. That count says how much work
was thrown away and nothing about what, so when a specification changes and a
topology stops appearing there was no way to say why.

The obvious place to look is the prunes, and on real specifications it is the
wrong one. A rule declares a predicate over the specification, and
`applicable_rules` consults it before the rule ever runs, so a rule the
specification forbids produces no state and therefore no prune. On the bundled
examples the search prunes nothing whatsoever: every topology that disappears
does so because a rule stopped being applicable, several steps before anything
could be rejected.

So the ledger records both, and the rule counts carry the weight. `applied`
counts the states a rule produced. `declined` counts the times a rule governed
the obligation in hand and its predicate refused it. A rule with declines and
no applications is one the specification has closed off, which is the sentence
a reader wants when a topology vanishes.

The rejection counts remain, because a specification tight enough to prune does
produce them, and complete topologies that were built and then refused are kept
individually up to a bound. Those are the only rejections attributable to a
named topology: a partial state has no topology signature yet, so blaming a
missing candidate on an early prune would be a guess.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from topology_lantern.types import ConstraintViolation, Obligation, Severity

#: Complete refused topologies retained individually. A search may visit many
#: thousands of states, so the record is bounded; the counts are not.
MAX_RECORDED_TOPOLOGIES = 200


class RejectionCause(StrEnum):
    """Where in the search a state stopped."""

    DEPTH = "depth_limit"
    PARTIAL = "partial_violation"
    NO_RULE = "no_applicable_rule"
    FINAL = "final_violation"


@dataclass(frozen=True, slots=True)
class RefusedTopology:
    """A complete topology the search finished building and then refused."""

    signature: str
    violations: tuple[ConstraintViolation, ...]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(
            violation.code for violation in self.violations if violation.severity is Severity.ERROR
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "violations": [violation.as_dict() for violation in self.violations],
        }


@dataclass(frozen=True, slots=True)
class RuleLedger:
    """How each rewrite rule fared against one specification."""

    applied: dict[str, int] = field(default_factory=dict)
    declined: dict[str, int] = field(default_factory=dict)

    @property
    def used(self) -> frozenset[str]:
        return frozenset(rule for rule, count in self.applied.items() if count)

    @property
    def closed_off(self) -> frozenset[str]:
        """Rules the specification governed but never permitted.

        A rule here was reached -- the obligation it answers came up -- and its
        predicate refused every time. That is a specification closing a branch,
        as opposed to a rule that simply never became relevant.
        """

        return frozenset(self.declined) - self.used

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": dict(sorted(self.applied.items())),
            "declined": dict(sorted(self.declined.items())),
            "closed_off": sorted(self.closed_off),
        }


@dataclass(frozen=True, slots=True)
class SearchLedger:
    """The accounting behind one generation result."""

    rules: RuleLedger = field(default_factory=RuleLedger)
    by_cause: dict[str, int] = field(default_factory=dict)
    by_code: dict[str, int] = field(default_factory=dict)
    by_obligation: dict[str, int] = field(default_factory=dict)
    refused: tuple[RefusedTopology, ...] = ()
    truncated: bool = False

    @property
    def rejected(self) -> int:
        return sum(self.by_cause.values())

    def refusal_for(self, signature: str) -> RefusedTopology | None:
        """Find the recorded refusal of one topology, if it was recorded.

        Absence is not evidence that the topology was accepted or never built:
        the record is bounded, and `truncated` says whether that bound was hit.
        """

        for item in self.refused:
            if item.signature == signature:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules": self.rules.as_dict(),
            "rejected": self.rejected,
            "by_cause": dict(sorted(self.by_cause.items())),
            "by_code": dict(sorted(self.by_code.items())),
            "by_obligation": dict(sorted(self.by_obligation.items())),
            "truncated": self.truncated,
            "refused_topologies": [item.as_dict() for item in self.refused],
        }


class LedgerRecorder:
    """Collect the accounting during one search.

    Deliberately mutable and deliberately outside the search state: a recorder
    that influenced the search would change the result it describes.
    """

    def __init__(self, *, limit: int = MAX_RECORDED_TOPOLOGIES) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("refusal record limit must be a non-negative integer")
        self._limit = limit
        self._applied: Counter[str] = Counter()
        self._declined: Counter[str] = Counter()
        self._causes: Counter[str] = Counter()
        self._codes: Counter[str] = Counter()
        self._obligations: Counter[str] = Counter()
        self._refused: list[RefusedTopology] = []
        self._seen: set[str] = set()
        self._truncated = False

    def rules(self, governing: tuple[str, ...], applicable: tuple[str, ...]) -> None:
        """Record one obligation's worth of rule decisions.

        `governing` is every rule that answers this obligation kind, whether or
        not the specification permits it. The difference between the two is the
        specification talking.
        """

        permitted = set(applicable)
        for rule_id in governing:
            if rule_id in permitted:
                self._applied[rule_id] += 1
            else:
                self._declined[rule_id] += 1

    def depth(self) -> None:
        self._causes[RejectionCause.DEPTH.value] += 1

    def partial(self, violations: tuple[ConstraintViolation, ...]) -> None:
        self._causes[RejectionCause.PARTIAL.value] += 1
        self._count_codes(violations)

    def no_rule(self, obligation: Obligation) -> None:
        self._causes[RejectionCause.NO_RULE.value] += 1
        self._obligations[obligation.kind.value] += 1

    def final(self, signature: str, violations: tuple[ConstraintViolation, ...]) -> None:
        """Record a complete topology that failed its final checks.

        Only the first refusal of a signature is kept. Equivalent graphs reached
        by different rule orders fail identically, so repeats would fill the
        record with one topology and crowd out the rest.
        """

        self._causes[RejectionCause.FINAL.value] += 1
        self._count_codes(violations)
        if signature in self._seen:
            return
        if len(self._refused) >= self._limit:
            self._truncated = True
            return
        self._seen.add(signature)
        self._refused.append(RefusedTopology(signature=signature, violations=violations))

    def _count_codes(self, violations: tuple[ConstraintViolation, ...]) -> None:
        for violation in violations:
            if violation.severity is Severity.ERROR:
                self._codes[violation.code] += 1

    def finish(self) -> SearchLedger:
        return SearchLedger(
            rules=RuleLedger(applied=dict(self._applied), declined=dict(self._declined)),
            by_cause=dict(self._causes),
            by_code=dict(self._codes),
            by_obligation=dict(self._obligations),
            refused=tuple(self._refused),
            truncated=self._truncated,
        )
