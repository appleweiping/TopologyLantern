# TopologyLantern

[![CI](https://github.com/appleweiping/TopologyLantern/actions/workflows/ci.yml/badge.svg)](https://github.com/appleweiping/TopologyLantern/actions/workflows/ci.yml)
[![CodeQL](https://github.com/appleweiping/TopologyLantern/actions/workflows/codeql.yml/badge.svg)](https://github.com/appleweiping/TopologyLantern/actions/workflows/codeql.yml)
[![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

TopologyLantern generates conceptual analog topologies and builds canonical
typed graphs from a bounded hierarchical SPICE subset. Generated candidates
include rule-by-rule derivations, structural review notes,
identifier-independent signatures, transparent metrics, Pareto fronts, and
replayable traces.

The generator is deliberately offline and unsized. It does not call an LLM, a
simulator, a PDK, or an optimizer, and it does not claim electrical performance.
Its output is a reviewable starting point for engineering work.

It can also ingest a resource-bounded hierarchical SPICE connectivity subset
into a canonical typed graph, validate versioned layout constraints, and emit
evidence-backed placement candidates without presenting inference as user
intent. See [the circuit graph guide](docs/circuit-graph.md).

For controlled experiments, the project also provides a compact replayable
rule-sequence representation and a leakage-checked, constraint-guided bigram
baseline. This is explicitly a deterministic statistical baseline—not a
trained neural model or a circuit-performance predictor. See
[the rule-sequence baseline guide](docs/sequence-baseline.md).

![A real TopologyLantern CLI run](docs/assets/demo.svg)

## Installation

Python 3.11 or newer is required. There are no runtime dependencies.

```console
python -m pip install .
topology-lantern --help
```

For development:

```console
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python -m build
```

## Circuit graph and layout evidence

Convert the synthetic hierarchical OTA to the stable version-1 graph contract:

```console
topology-lantern ingest-spice examples/circuits/ota.sp --top ota --pretty
```

Inspect conservative layout candidates from current-mirror and differential-pair
connectivity:

```console
topology-lantern layout-evidence examples/circuits/ota.sp --top ota --pretty
```

The output keeps `user_constraints` and `inferred_constraints` in separate
arrays. Every inferred item carries confidence and evidence and remains a
candidate for review, never an implicit placement requirement. Parsing is
offline: no expression evaluation, simulator, PDK, or external command is used.
Identifiers are NFC-normalized with control/bidi rejection, every included file
is read against independent per-file and cumulative budgets, and inference is
preflight-bounded before candidate construction. Device and instance records
carry source file/line provenance. Versioned JSON schemas ship inside the wheel.

All commands use atomic, no-clobber file output. Add `--force` only when an
existing result should be replaced; even then an output can never alias a
netlist/include, constraint file, specification, or report input.

## Generate candidates

Search a JSON design specification and rank the topologies it admits:

```console
topology-lantern generate examples/low_voltage_diff_stage.json --limit 2
```

```text
TopologyLantern: 2 candidates
spec: sha256:de7b6207e6677c688b6aa0055c65b5d2b779c743c20392a1892824cec3c13512
search: 6 explored, 0 pruned, 0 duplicate, exhausted=false
1. TL-2a033a1ce04c front=0 score=26 devices=5 stages=1 headroom=2
   input.diff_pair -> tail.current_source -> load.active_mirror -> output.direct
2. TL-9fe18125a784 front=1 score=42 devices=7 stages=2 headroom=3
   input.diff_pair -> tail.current_source -> load.active_mirror -> output.source_follower
```

Every run reports the spec fingerprint and the search accounting (explored,
pruned, duplicate, and whether the space was exhausted), so a result can be tied
back to the exact input it came from. `front=0` marks the Pareto front.

`--format json` emits the same result as a machine-readable report, and
`--format spice --candidate N` writes a conceptual netlist for one candidate:

```console
topology-lantern generate examples/low_voltage_diff_stage.json --format spice --candidate 1
```

That netlist is a **review artifact, not a design**: devices are unsized,
placeholders such as `{I_UNSIZED}` are left symbolic, and the header says so.
Use it to reason about structure, not to simulate. `explain` and `replay` work
on a saved JSON report if you want the per-step reasoning for one candidate.

## Reproducible sizing benchmark

Export the strict cross-project topology-and-sizing contract:

```console
topology-lantern benchmark examples/low_voltage_diff_stage.json --limit 4 --pretty --output benchmark.json
```

The contract includes canonical topology signatures, bounded sizing variables,
provenance, expected metrics, and a canonical SHA-256. BiasWeave can consume it
without importing this package. See `benchmarks/README.md` and
`docs/validation.md`. Its analytic metrics are regression proxies, not
simulation results.

The included differential-stage specification exercises independent choices
for tail bias, load, output exposure, and buffering:

```console
topology-lantern generate examples/low_voltage_diff_stage.json --limit 6
```

Write a stable JSON result:

```console
topology-lantern generate examples/low_voltage_diff_stage.json \
  --limit 6 --format json --pretty --output generated-report.json
```

Emit one deliberately unsized SPICE review artifact:

```console
topology-lantern generate examples/low_voltage_diff_stage.json \
  --limit 6 --format spice --candidate 1
```

The SPICE form contains intentionally symbolic, unsized models and values and a prominent warning.
It exists to make the graph easy to inspect, not to create a runnable design.

## Specification

A versioned JSON specification contains only bounded design intent:

```json
{
  "schema_version": 1,
  "name": "low-voltage differential stage",
  "supply_voltage": 1.8,
  "input_mode": "differential",
  "output_mode": "single",
  "polarity": "nmos_input",
  "load_preference": "either",
  "require_compensation": false,
  "allow_resistive_bias": true,
  "allowed_devices": [
    "nmos", "pmos", "resistor", "capacitor", "current_source"
  ],
  "limits": {
    "max_candidates": 12,
    "max_states": 5000,
    "max_depth": 8,
    "max_devices": 24,
    "max_canonical_permutations": 40320
  },
  "objectives": {
    "device_count": 4,
    "headroom": 3,
    "symmetry": 2,
    "passives": 1,
    "warnings": 5
  }
}
```

Unknown fields and values with the wrong JSON type are rejected; numeric strings,
booleans in integer fields, and fractional limit or objective values are not coerced.
`input_mode` and `output_mode` are `single` or
`differential`; the current rule catalog requires differential input for a
differential output. `polarity` selects `nmos_input` or `pmos_input`.
`load_preference` is `active`, `resistive`, or `either`.

All limits are positive. Objective weights are non-negative and at least one
must be non-zero. The chosen input transistor family must remain in
`allowed_devices`.

Validate and fingerprint a spec:

```console
topology-lantern validate-spec examples/low_voltage_diff_stage.json
```

Trace replay also verifies that the report's specification fingerprint matches
the supplied specification before applying any rules.

## How generation works

The initial graph contains only named interface ports and ordered proof
obligations. A bounded best-first search consumes the first obligation with
each applicable independent rule. Current rules cover:

- matched differential pair or single common-source input;
- current-source or explicitly permitted resistive tail bias;
- symmetric resistor loads, or a single-ended opposite-polarity active mirror;
- optional unsized compensation capacitor;
- direct single/differential drain output or a conceptual source follower.

Every rule declares its obligation class, predicate, summary, rationale, added
devices, and produced facts. States with conclusive structural errors or excess
devices are pruned immediately. Complete states receive final connectivity,
port, bulk, short, symmetry, bias, and compensation checks.

Equivalent small graphs are deduplicated by an exact canonical encoding that
enumerates internal-net identifiers under a configurable permutation budget.
Larger graphs use bounded color refinement as a bucket plus a deterministic
labeled collision guard. The guard may retain renamed duplicates, but a
refinement collision cannot discard a structurally different candidate.

Candidates are separated into non-dominated Pareto fronts over device count,
headroom proxy, passives, symmetry penalty, warnings, and stage count. Declared
integer objective weights order candidates inside a front. These metrics are
transparent structural proxies, not predicted circuit performance.

A ledger records, for every rule, the states it produced and the times the
specification refused it. That is where a change between two runs shows up:
rules are gated before a state is built, so a forbidden rule leaves no pruned
state behind.

## Comparing two specifications

Iterating on a specification is the ordinary workflow, and each `generate` run
was an island. Candidate numbering is no help across runs, because it follows
the ranking and moves whenever anything else does.

```console
topology-lantern diff before.json after.json
```

```
4 topologies in both, 4 only on the left, 0 only on the right.

Rules only the left specification permits: tail.resistor

Reordered:
  TL-b4f99d5eafc1  3 -> 2 (up 1)
  TL-9fe18125a784  5 -> 3 (up 2)
  TL-1899f6330a4b  6 -> 4 (up 2)

Lost (present on the left only):
  TL-bc45e11450ed  at position 2
    cannot exist under the other specification, which closed off tail.resistor; this topology uses it
```

Topologies are matched by the identifier-independent signature, so the same
graph is recognized across runs whatever its nets were called and whichever
rule order produced it.

### How firmly an absence is explained

The reason a topology is missing comes in four strengths, and they are not
interchangeable:

1. **Built and refused.** The other search constructed the graph and its final
   checks rejected it. The failing constraint codes are reported. This is a
   fact about a graph that existed.
2. **A rule was closed off.** The topology's own trace uses a rule the other
   specification governed and never permitted, so that run could not have built
   it. This is proved from the trace, not inferred from the absence.
3. **A rule every other topology carries.** The other run's topologies all use
   a rule this one lacks. Deliberately weaker wording, because it is an
   observation about that run's output rather than a rule read out of its
   specification. This is what a newly required stage looks like from here: a
   requirement adds an obligation instead of closing a rule off.
4. **No cause to offer.** The topology is simply absent. Saying so is better
   than picking one of the above.

A search that stopped at its limits is flagged before any of this, because an
absence from an unfinished search may mean the topology was out of budget.

### Why the ledger records rules rather than prunes

The obvious place to look for a cause is the pruned states, and on real
specifications it is the wrong one. A rule declares a predicate over the
specification and `applicable_rules` consults it before the rule ever runs, so
a rule the specification forbids produces no state and therefore no prune. On
the bundled examples the search prunes nothing at all; every topology that
disappears does so because a rule stopped being applicable, several steps
before anything could be rejected.

The ledger therefore counts, per rule, the states it produced and the times it
governed the obligation in hand and was refused. A rule with refusals and no
applications is one the specification has closed off. Rejection counts by
cause, by violation code and by obligation kind are kept too, along with the
complete topologies that were built and then refused, bounded and flagged when
that bound is reached.

```console
topology-lantern generate spec.json --format json --ledger
```

The ledger is opt-in. Readers of this report reject unknown fields, which is
the right behaviour, so the default document is unchanged.

## Explanation and replay

Generate a report, copy a candidate ID, then run:

```console
topology-lantern explain generated-report.json TL-2a033a1ce04c
topology-lantern replay examples/low_voltage_diff_stage.json \
  generated-report.json TL-2a033a1ce04c
```

`explain` labels its content as unverified because it reads stored evidence.
`replay` uses the report's recorded requested limit to regenerate the complete
search result from the supplied spec. It strictly compares the tool identity,
search counters, rule catalog, candidate order, IDs, ranks, scores, topology,
facts, trace, metrics, violations, and canonical signatures before accepting
the report.

## Python API

```python
from topology_lantern import (
    DesignSpec,
    candidate_spice,
    explain_candidate,
    generate_candidates,
    verify_replay,
)

spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
result = generate_candidates(spec, limit=6)
for candidate in result.candidates:
    print(candidate.candidate_id, candidate.pareto_rank, candidate.metrics)

selected = result.candidates[0]
print(explain_candidate(selected))
print(candidate_spice(selected))
verify_replay(spec, selected)
```

Result objects are immutable dataclasses. `GenerationResult.as_dict()` is the
versioned, deterministic JSON representation.

## What a candidate does not prove

A candidate has no dimensions, device models, bias currents, component values,
operating-point solution, transfer function, stability result, noise result,
corner sweep, mismatch analysis, reliability verification, layout, or physical
design-rule result. `headroom_units` is a relative topology cost, not volts.
An active mirror rule records connectivity intent, not matching quality.

Before simulation, an engineer must select a technology, add trusted models,
size devices, calculate bias and swing, and review the conceptual assumptions.
Simulation and physical verification must run in an isolated, appropriate tool
environment.

See [docs/architecture.md](docs/architecture.md) for state invariants, search
semantics, canonicalization, and extension rules.

## License

TopologyLantern is available under the MIT License.
