"""Human explanations and deterministic trace replay."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from topology_lantern.canonical import candidate_id, topology_signature
from topology_lantern.obligations import initial_state
from topology_lantern.rules import rule_by_id
from topology_lantern.search import candidate_from_state
from topology_lantern.spec import DesignSpec, load_spec
from topology_lantern.types import Candidate, ReplayError, SearchState


def explain_candidate(candidate: Candidate) -> str:
    lines = [
        f"{candidate.candidate_id}: Pareto front {candidate.pareto_rank}, score {candidate.score}",
        f"signature: {candidate.signature}",
        (
            "metrics: "
            f"{candidate.metrics.device_count} devices, "
            f"{candidate.metrics.transistor_count} transistors, "
            f"{candidate.metrics.stage_count} stages, "
            f"{candidate.metrics.headroom_units} headroom units"
        ),
        "derivation:",
    ]
    for step in candidate.trace:
        lines.append(f"  {step.index}. {step.rule_id} — {step.summary}")
        lines.append(f"     {step.rationale}")
        if step.added_devices:
            lines.append(f"     devices: {', '.join(step.added_devices)}")
        if step.added_nets:
            lines.append(f"     nets: {', '.join(step.added_nets)}")
    if candidate.violations:
        lines.append("review notes:")
        for item in candidate.violations:
            lines.append(f"  - [{item.severity.value}] {item.code}: {item.message}")
    else:
        lines.append("review notes: none")
    lines.append(
        "boundary: conceptual topology only; sizing, bias verification, stability, noise, "
        "corners, layout, and foundry rules remain unresolved."
    )
    return "\n".join(lines) + "\n"


def replay_rule_ids(
    spec: DesignSpec | Mapping[str, object] | str,
    rule_ids: Sequence[str],
) -> SearchState:
    selected = load_spec(spec)
    state = initial_state(selected)
    for index, rule_id in enumerate(rule_ids, start=1):
        if not state.obligations:
            raise ReplayError(f"trace has extra rule {rule_id!r} at step {index}")
        try:
            rule = rule_by_id(rule_id)
        except KeyError as exc:
            raise ReplayError(f"unknown rule ID {rule_id!r} at step {index}") from exc
        obligation = state.obligations[0]
        if not rule.applicable(selected, state, obligation):
            raise ReplayError(
                f"rule {rule_id!r} is not applicable to {obligation.kind.value} at step {index}"
            )
        state = rule.apply(selected, state, obligation)
    return state


def verify_replay(
    spec: DesignSpec | Mapping[str, object] | str,
    candidate: Candidate,
) -> SearchState:
    selected = load_spec(spec)
    state = replay_rule_ids(selected, [step.rule_id for step in candidate.trace])
    if state.obligations:
        raise ReplayError(
            "trace ended with unresolved obligations: "
            + ", ".join(item.key for item in state.obligations)
        )
    signature = topology_signature(
        state.topology,
        max_permutations=selected.limits.max_canonical_permutations,
    )
    if signature != candidate.signature:
        raise ReplayError(f"replayed signature {signature} does not match {candidate.signature}")
    if candidate.candidate_id != candidate_id(signature):
        raise ReplayError("candidate ID does not match the verified topology signature")
    trusted = candidate_from_state(selected, state)
    if trusted is None:
        raise ReplayError("replayed topology violates the final candidate contract")
    for field in ("topology", "facts", "trace", "metrics", "violations"):
        if getattr(candidate, field) != getattr(trusted, field):
            raise ReplayError(f"candidate {field} does not match replayed evidence")
    return state
