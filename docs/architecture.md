# Architecture

## Objective

TopologyLantern explores a deliberately finite conceptual design space while
preserving an explanation for every graph edit. The central invariant is that a
candidate can be recreated from its design specification and ordered rule IDs
without hidden state, randomness, or external tools.

## Modules

- `spec` validates design intent, limits, objectives, and allowed primitives.
- `types` defines immutable graphs, obligations, facts, traces, metrics, and
  result objects.
- `graph` offers a copy-on-write builder and connectivity inspection.
- `obligations` derives the initial interface and ordered proof obligations.
- `rules` contains independent predicates and graph transformations.
- `constraints` separates conclusive incremental errors from final review notes.
- `canonical` provides exact identifier-independent identity within the
  permutation budget and collision-safe bounded bucketing above it.
- `search` performs bounded deterministic best-first expansion and pruning.
- `rank` computes structural metrics, Pareto fronts, and weighted tie-breaks.
- `explain` renders and replays derivations.
- `emit` serializes stable JSON, terminal summaries, and unsized SPICE skeletons.
- `cli` exposes generation, validation, explanation, and replay with stable exits.

## Immutable state

A `SearchState` contains a topology, ordered unresolved obligations, sorted
facts, and ordered trace. The topology contains named ports, nets, and devices.
Each device has the exact terminal tuple required by its kind. `TopologyBuilder`
copies the prior graph, validates every edit, sorts the committed representation,
and never mutates an earlier state.

Facts are explicit strings such as `left_drain=n_left`,
`load_structure=current_mirror`, or `stage_count=2`. They are not numerical
simulation results. A rule may use prior facts only through the state supplied
to its transform.

## Obligations and rules

The initial sequence is input stage, optional tail bias, load, optional
compensation, and output. Search always expands the first obligation. This makes
rule replay unambiguous and prevents ordering permutations from producing
meaningless duplicates.

A `RewriteRule` declares:

1. a stable rule ID;
2. the obligation kind it consumes;
3. a pure applicability predicate;
4. a graph transform;
5. a human summary and rationale.

The wrapper records added device and net names, the consumed obligation, and
any newly produced obligations in a `TraceStep`. A transform cannot silently
edit the trace itself.

## Constraints

Incremental checks reject device-count overflow, excluded device families,
shorted MOS drain/source terminals, and incorrect bulk rails. These properties
cannot be repaired by a later obligation in the current rule model.

Final checks also require every port to have an endpoint, every internal net to
have at least two endpoints, no unresolved obligations, the requested port
cardinality, and one connected graph. Bias sensitivity, buffering cost, and
symmetry concerns remain warning or note records rather than errors.

The distinction prevents partial graphs from being rejected merely because an
output or load obligation has not yet connected a net.

## Bounded search

The heap priority is unresolved obligation count, device count, trace depth,
then rule-ID sequence. A serial integer removes any dependence on object
comparison. The spec caps states, depth, devices, candidates, and canonical
permutations.

Each state signature combines canonical topology, ordered obligations, and
sorted facts. A seen set suppresses duplicate states. Complete candidates are
also keyed by canonical topology signature, because two different proof paths
to the same final circuit should not inflate the result set.

Search stops at the candidate limit, state limit, or an empty heap. The result
reports explored, pruned, and duplicate counts plus whether the heap was fully
exhausted. There is no clock-based cutoff, random seed, or platform-dependent
iteration order.

## Canonical identity

Ports are fixed labels because their names and roles are external intent.
Device names are ignored. For a graph whose internal-net factorial is within
the permutation budget, canonicalization tries every mapping from internal nets
to `n0`, `n1`, and so on. Each mapping produces sorted device descriptors made
of kind, terminal-labelled nets, and attributes. The lexicographically smallest
encoding is exact under internal-net renaming.

When enumeration would exceed the limit, bounded bipartite color refinement
hashes incident terminal labels, device kinds, attributes, and fixed port
colors. Refinement is only a bucket, not proof of graph equivalence. A labeled
encoding is retained as a collision guard: this can preserve duplicates that
differ only by submitted internal labels, but it prevents non-isomorphic graphs
from being merged merely because refinement collides. The fallback is tagged
separately so exact and refined identities cannot collide by construction.

Reports record the effective requested candidate limit. Replay regenerates the
complete search result from the specification and that limit, then strictly
compares every declared report field, including search counters, rule catalog,
candidate ordering, ranks, scores, and all candidate evidence.

## Ranking

Metrics are directly counted or read from rule facts:

- total devices, transistors, passives, and internal nets;
- conceptual stage count;
- relative headroom units;
- paired-symmetry penalty;
- final review warning count.

One candidate dominates another only when every metric is no worse and at
least one is better. Repeated removal of non-dominated sets assigns Pareto
fronts. Within a front, the specification's integer weights produce an explicit
score, followed by metric vector and signature as stable tie-breakers.

Neither dominance nor score estimates gain, bandwidth, power, noise, area, or
yield. Adding such estimates would require a separately documented and tested
model with units and validity bounds.

## Replay

Replay reconstructs the initial state from the original spec and consumes the
first obligation with each stored rule ID. Unknown, extra, or inapplicable rules
fail with a step number. Verification requires no remaining obligations and an
identical canonical topology signature.

A JSON report also stores summaries and rationales so it remains explainable
without loading executable project code. The CLI validates the report identity
and schema before reading a trace.

## Adding a rule

A contribution should include:

1. a unique stable ID and one obligation class;
2. a predicate based only on validated spec and immutable state;
3. a transform that creates a valid committed topology;
4. explicit facts for later transforms and metrics;
5. a concise rationale that states tradeoffs without claiming performance;
6. applicability, rejection, positive graph, trace, replay, constraint, and
   deterministic ordering tests;
7. documentation of new conceptual assumptions.

Do not add a rule that shells out, loads a model, guesses a PDK, embeds a hidden
score, or relies on dictionary insertion order. A topology family that requires
new semantics should introduce an explicit obligation or fact rather than infer
meaning from a device name.
