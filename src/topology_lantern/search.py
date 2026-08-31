"""Bounded deterministic best-first search over independent rewrite rules."""

from __future__ import annotations

import heapq
from dataclasses import replace

from topology_lantern.canonical import candidate_id, state_signature, topology_signature
from topology_lantern.constraints import final_violations, has_error, partial_violations
from topology_lantern.obligations import initial_state
from topology_lantern.rank import measure, rank_candidates, with_warning_count
from topology_lantern.rules import RULES, applicable_rules
from topology_lantern.spec import DesignSpec, load_spec
from topology_lantern.types import Candidate, GenerationResult, SearchState


def _priority(state: SearchState) -> tuple[object, ...]:
    return (
        len(state.obligations),
        len(state.topology.devices),
        len(state.trace),
        tuple(step.rule_id for step in state.trace),
    )


def candidate_from_state(spec: DesignSpec, state: SearchState) -> Candidate | None:
    violations = final_violations(spec, state)
    if has_error(violations):
        return None
    signature = topology_signature(
        state.topology,
        max_permutations=spec.limits.max_canonical_permutations,
    )
    base = Candidate(
        candidate_id=candidate_id(signature),
        signature=signature,
        topology=state.topology,
        facts=state.facts,
        trace=state.trace,
        metrics=measure(state),
        violations=violations,
    )
    return replace(base, metrics=with_warning_count(base.metrics, base))


def generate_candidates(
    spec: DesignSpec | dict[str, object] | str,
    *,
    limit: int | None = None,
) -> GenerationResult:
    """Generate ranked complete candidates under explicit search limits."""

    selected = load_spec(spec)
    requested = limit if limit is not None else selected.limits.max_candidates
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("limit must be a positive integer")
    requested = min(requested, selected.limits.max_candidates)
    start = initial_state(selected)
    queue: list[tuple[tuple[object, ...], int, SearchState]] = []
    serial = 0
    heapq.heappush(queue, (_priority(start), serial, start))
    seen = {
        state_signature(
            start,
            max_permutations=selected.limits.max_canonical_permutations,
        )
    }
    completed: dict[str, Candidate] = {}
    explored = 0
    pruned = 0
    duplicates = 0

    while queue and explored < selected.limits.max_states and len(completed) < requested:
        _, _, state = heapq.heappop(queue)
        explored += 1
        if len(state.trace) > selected.limits.max_depth:
            pruned += 1
            continue
        if has_error(partial_violations(selected, state)):
            pruned += 1
            continue
        if not state.obligations:
            candidate = candidate_from_state(selected, state)
            if candidate is None:
                pruned += 1
            else:
                completed.setdefault(candidate.signature, candidate)
            continue
        obligation = state.obligations[0]
        rules = applicable_rules(selected, state, obligation)
        if not rules:
            pruned += 1
            continue
        for rule in rules:
            child = rule.apply(selected, state, obligation)
            signature = state_signature(
                child,
                max_permutations=selected.limits.max_canonical_permutations,
            )
            if signature in seen:
                duplicates += 1
                continue
            seen.add(signature)
            serial += 1
            heapq.heappush(queue, (_priority(child), serial, child))

    ranked = rank_candidates(selected, tuple(completed.values()))
    return GenerationResult(
        spec_fingerprint=selected.fingerprint(),
        candidates=ranked,
        explored_states=explored,
        pruned_states=pruned,
        duplicate_states=duplicates,
        exhausted=not queue,
        requested_limit=requested,
        rule_catalog=tuple(rule.rule_id for rule in RULES),
    )
