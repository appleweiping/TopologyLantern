# Circuit graph ingest and layout evidence

TopologyLantern 0.4 turns a deliberately small, documented SPICE connectivity
subset into a typed, deterministic hierarchy. The graph is an interchange and
review artifact. It is not a simulator input validator, a flattened electrical
network, or proof that a circuit meets its specification.

## Supported SPICE subset

The ingest accepts case-insensitive names and these records:

- `.subckt name port... [PARAMS: name=value...]` and the matching
  `.ends [name]`; `PARAMS:` is case-insensitive;
- relative `.include path` from files loaded with `load_spice`;
- `.global net...` (node `0` is always global);
- MOSFET (`M`), resistor (`R`), capacitor (`C`), independent current source
  (`I`), independent voltage source (`V`), diode (`D`), BJT (`Q`), and
  subcircuit instance (`X`) records;
- `X` instances with optional `PARAMS: name=value...` overrides, opaque
  `name=value` parameters, and `+` continuation lines;
- a bare `.end` as the terminal record of the entry source; and
- ignored metadata directives `.model`, `.option`, `.param`, and `.temp`.

The parser does not evaluate expressions, expand models, interpret units, run a
preprocessor, load a PDK, or launch an external tool. An unsupported directive
or device is rejected instead of being guessed. This narrow contract makes the
accepted language auditable.

`load_spice` resolves an include only below the entry netlist's directory.
Absolute paths, parent traversal, recursive includes, invalid UTF-8, and files
that resolve outside that directory are rejected. An included file is expanded
only on its first resolved-path occurrence. An included file cannot contain
`.end`; in the entry file, `.end` accepts no arguments and any later
non-comment content is rejected. Default hard limits cover per-file bytes,
cumulative bytes across includes, file count, logical lines, line and token
length, elements, subcircuits, and hierarchy depth. Every file read is bounded
to the smaller of its per-file allowance and the remaining cumulative budget,
plus one byte solely to detect overflow. Limits can be tightened with
`IngestLimits`, but cannot be disabled or made non-positive.
For in-memory ingest, the provenance `source` label is NFC-normalized and capped
at 4096 Unicode scalar values; control, format, surrogate, bidi-control, and
line-separator characters are rejected. A character-count lower bound is
checked before UTF-8 encoding, so an already-oversized source cannot trigger a
second unbounded allocation.

## Canonical hierarchy

Identifiers are NFC-normalized and Unicode-case-folded; controls, bidi format
characters, whitespace, and path-like punctuation are rejected. Parameter keys
are normalized the same way and opaque values are case-folded. Element and
scope arrays are sorted; subcircuit port order is preserved because instance
binding is positional. Only the top scope and its transitive reference closure
are emitted. An undefined reference, recursive reference, duplicate name, or
port-count mismatch is a hard error.

Subcircuit declarations retain their parameter defaults. Each instance keeps
both its explicit override map and a deterministic `effective_parameters` map
formed from defaults plus overrides. An override of an undeclared default is a
typed error; it is never reinterpreted as another port or connection. Values
remain opaque strings: this records parameter intent but does not evaluate or
substitute expressions.

Every scope, port, net, device, and instance gets a namespaced ID derived from
its normalized kind, definition scope, and name. Global nets use a shared
namespace across scopes. `graph_id` is a full SHA-256 over the semantic graph,
excluding source file names. Therefore changing whitespace, record order, path,
or letter case does not change the graph identity; changing connectivity,
parameters, port order, or a referenced definition does. Devices and instances
also carry source file and line provenance for diagnostics; provenance is
deliberately excluded from semantic identity.

This is a definition graph, not an occurrence-flattened graph. An instance has
an ID, a referenced scope ID, and an ordered port-to-parent-net connection map.
That representation keeps hierarchy and makes the reference closure explicit.
Device constraints apply at definition scope (and therefore to each occurrence
of that definition); every multi-subject placement constraint must stay within
one definition scope. Parent-scope instance IDs can be constrained together.
The version-1 JSON contract is
[`schemas/circuit-graph-1.schema.json`](schemas/circuit-graph-1.schema.json).

```console
topology-lantern ingest-spice examples/circuits/ota.sp --top ota --pretty
```

```python
from topology_lantern import circuit_graph_json, load_spice

graph = load_spice("examples/circuits/ota.sp", top="ota")
print(graph.graph_id)
print(circuit_graph_json(graph, pretty=True))
```

The synthetic clean-room corpus contains standalone current-mirror and
differential-pair blocks, every accepted primitive family, a hierarchical OTA,
and intentionally rejected adversarial inputs. The checked-in OTA golden graph
guards ordering, IDs, provenance, closure, binding, and serialization.

## Versioned layout intent

A user constraint document binds to exactly one `graph_id`. Unknown keys,
unknown graph IDs, wrong JSON types, repeated subjects, non-finite numbers, and
references to the wrong node class are rejected. Version 1 supports:

| Kind | Subjects | Required semantics |
| --- | --- | --- |
| `symmetry` | devices or instances | horizontal or vertical mirror axis |
| `common_centroid` | two equal, disjoint groups | horizontal, vertical, or both axes |
| `matching` | devices or instances | non-negative tolerance |
| `alignment` | devices or instances | equal `x` or equal `y` coordinate |
| `order` | ordered devices or instances | left/right or bottom/top direction |
| `keepout` | devices or instances | non-negative margin |
| `net_priority` | nets | integer priority from 0 through 100 |

The runtime contract and
[`schemas/layout-constraints-1.schema.json`](schemas/layout-constraints-1.schema.json)
both reject extra fields. Runtime validation additionally binds node references
to a particular graph and detects duplicate semantics, contradictory symmetry
or alignment axes, conflicting net priorities, directed-order cycles, and an
order that requires separation along an axis where the same pair is aligned.

## Evidence is not intent

`layout-evidence` can conservatively propose matching, symmetry, and
common-centroid candidates from local MOS connectivity. Each candidate has an
`origin` of `inferred`, a confidence in `[0, 1]`, and explicit evidence. Declared
constraints stay in the separate `user_constraints` array with an origin of
`user`; the command never merges or promotes inference into user intent.
Confidence is a deterministic rule strength for review ordering, not a
statistically calibrated probability.

Inference uses explicit `InferenceLimits` for total devices, pair evaluations,
and emitted candidates. Device and exact pair/candidate counts are checked in a
preflight pass before any inferred `LayoutConstraint` is created. Constraints
constructed through the public typed API are serialized and passed back through
the same graph binding, kind-shape, finite-number, schema, and conflict checks
before a report is emitted.

Current inference recognizes only three narrow motifs:

- same-model devices with shared source, bulk, and gate when one device is
  diode-connected (current-mirror matching candidate);
- same-model devices with shared source and bulk, distinct drain nets, and
  gates on distinct declared ports (differential matching and symmetry
  candidates); and
- equal-size parallel groups satisfying the differential evidence above
  (common-centroid candidate).

These are placement-review suggestions, not electrical equivalence, sizing
proof, or a command to a placer. A human or downstream policy must decide
whether to promote one into a user constraint.

```console
topology-lantern layout-evidence examples/circuits/ota.sp --top ota --pretty
topology-lantern layout-evidence examples/circuits/ota.sp --top ota \
  --constraints examples/circuits/ota-layout.json --output evidence.json
```

The report contract is
[`schemas/layout-constraint-report-1.schema.json`](schemas/layout-constraint-report-1.schema.json).
All three versioned schemas are also included under
`topology_lantern/schemas/` in the wheel.

```python
from topology_lantern import (
    layout_constraints_json,
    layout_inference_report,
    load_layout_constraints,
    load_spice,
)

graph = load_spice("examples/circuits/ota.sp", top="ota")
declared = load_layout_constraints("declared-layout.json", graph)
canonical_declarations = layout_constraints_json(declared, pretty=True)
report = layout_inference_report(graph, declared)
assert all(item.origin == "user" for item in report.user_constraints)
assert all(item.origin == "inferred" for item in report.inferred_constraints)
```

## Safe file output

Every CLI command writes to stdout unless `--output` is supplied. Output paths
are created atomically and never replace an existing file by default. Passing
`--force` requests atomic replacement, but cannot override the permanent input
protection: the destination may not alias any netlist (including resolved
includes), constraint document, design specification, or result report.
Resolved paths, platform case equivalence, hard links, and symbolic links are
checked. `--force` without `--output` is rejected.

## Performance probe

The benchmark repeatedly parses a selected netlist from disk and reports stable
graph/cardinality facts beside min, median, and p95 wall-clock time. Timing is
environment evidence, not a release guarantee.

```console
python benchmarks/circuit_ingest.py examples/circuits/ota.sp --top ota --repetitions 100
```
