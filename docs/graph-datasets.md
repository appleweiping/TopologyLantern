# Leakage-safe graph dataset preparation

TopologyLantern's development dataset API keeps graph renames and Euler
traversals tied to the circuit that produced them. It partitions complete
leakage groups before training, validation, or test assignment and rejects a
stack that would place one group in more than one partition.

This is an in-memory version-1 contract. It does not yet define a JSON wire
format or CLI. It prepares validated connectivity data; it does not simulate a
circuit, prove electrical equivalence, or establish graph isomorphism.

## Records and exact derivations

A root record contains one canonical `CircuitGraph`. A rename child contains the
complete `RenamedCircuit`, and a traversal child contains the complete
`CircuitGraphSequence`. Builders validate each operation against its immediate
parent:

```python
from topology_lantern import (
    compact_graph,
    encode_graph_sequence,
    load_spice,
    rename_devices,
    renamed_dataset_record,
    root_dataset_record,
    traversal_dataset_record,
    validate_graph_dataset,
)

graph = load_spice("examples/circuits/ota.sp", top="ota")
root = root_dataset_record(graph)

renaming = rename_devices(graph, seed=7)
renamed = renamed_dataset_record(root, renaming)

sequence = encode_graph_sequence(compact_graph(renaming.graph), seed=9)
traversed = traversal_dataset_record(renamed, sequence)

records = validate_graph_dataset((root, renamed, traversed))
```

Every record separates these identities:

- `root_lineage_id` is the semantic identity of the original root graph and is
  copied through every derivation;
- `root_source_evidence_id` also binds the root's source paths and source
  locations;
- `immediate_graph_id` identifies the graph represented by this particular
  record;
- `immediate_source_evidence_id` binds every field of that immediate graph;
- `derivation_evidence_id` binds the root, complete rename manifest, or exact
  Euler sequence as appropriate; and
- `record_id` binds the complete record metadata above.

These SHA-256 values are consistency and content identities, not signatures or
proof of authorship. Validation requires a bounded, acyclic, single-parent
derivation graph. Every non-root parent must be present; lineage must propagate
unchanged; rename restoration and traversal decoding must reproduce the exact
parent graph. Missing parents, cycles, reordered or tampered evidence, and a
chain deeper than 64 operations fail closed.

## Conservative leakage bucket

`conservative_leakage_bucket(graph)` builds a typed hierarchical multigraph and
runs bounded one-dimensional Weisfeiler-Lehman color refinement. Initial colors
and labeled links preserve:

- the top definition and definition containment;
- ordered external ports by ordinal;
- local, port, and globally marked net structure, including a global net shared
  between definitions;
- primitive kind and ordered terminal roles; and
- hierarchy-instance connections by referenced definition and formal-port
  ordinal.

Device, instance, scope, port, net, model, parameter, source-path, and source
location strings do not enter the bucket. Port order and the equality pattern of
globally shared nets remain structural even though their names do not. Therefore
seeded device renames, consistent internal-net renames, sizing sweeps, parameter
overrides, and model aliases remain together. Ignoring those fields intentionally
merges some unrelated circuits too; that loses split capacity but cannot create
leakage.

Refinement stops when the color partition no longer splits or after 64 rounds.
The result records whether the round budget was exhausted. A truncated result
is still a deterministic conservative bucket; it is not upgraded to a topology
identity. Even an untruncated equal bucket is not an isomorphism certificate:
color refinement can assign the same bucket to non-isomorphic graphs. Bucket
equality means only "keep together". Bucket inequality does not prove circuits
unrelated, and neither outcome proves electrical equivalence.

## Group-before-split and safe stacking

`split_graph_dataset` first forms union-find components. Records join when they
share any root lineage, root source evidence, immediate graph identity,
conservative leakage bucket, or explicit parent edge. It then hashes the sorted
complete component with an unsigned 64-bit seed and assigns the whole component
according to exact integer `(train, validation, test)` weights:

```python
from topology_lantern import split_graph_dataset, stack_dataset_splits

split = split_graph_dataset(records, seed=23, weights=(8, 1, 1))
assert len(split.train) + len(split.validation) + len(split.test) == len(records)

# Stacking preserves existing assignments and rejects cross-partition leakage.
combined = stack_dataset_splits(split)
```

Weights are bounded integers rather than floating-point ratios. Component
assignment is independent of input ordering and deterministic for the complete
record set, but exact target ratios are not promised because a leakage component
is indivisible. Adding records can join components and should be treated as a new
split operation. `stack_dataset_splits` does not reassign: it requires identical
seed and weights, rejects duplicate records, revalidates every payload and
derivation, and fails if a combined leakage component spans partitions.

## Resource envelope

All public operations reject mutable collection shapes and preflight counts and
UTF-8 bytes before building compact views or JSON-shaped evidence. One graph or
sequence is bounded to 512 scopes, 32 source paths, 100,000 owners, 100,000 nets,
500,000 graph incidences or combined traversal trails and steps, 4,096 entries in
one parameter map, 1,000,000 nested pairs, and 64 MiB of text. A dataset has at
most 10,000 records, one million aggregate vertices, two million aggregate
incidences or traversal-work items, four million aggregate nested pairs, and 256
MiB of aggregate text. Derivation depth and WL rounds are separately bounded at
64. Stacking preflights these aggregate limits across all input splits before it
replays any derivation or builds a WL view.

The contract intentionally favors false-positive grouping over false-negative
leakage. Dataset authors should partition roots before generating augmentation;
validation and stacking are the enforcement backstop, not a replacement for
preserving lineage at ingestion.
