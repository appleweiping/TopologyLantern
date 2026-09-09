# Reversible circuit graph sequences

The version-1 Euler sequence encodes an actual terminal-labelled circuit graph.
It preserves scope definitions, ports, model names, parameter overrides, source
locations, and isolated nets as metadata. Connectivity appears only as ordered
traversal steps. Each step identifies one incidence, its terminal ordinal and
name, and the next vertex. The starting vertex and alternating owner/net walk
determine both endpoints; there is no second edge table.

```console
topology-lantern connectivity-graph examples/circuits/ota.sp --top ota \
  --view compact --output ota-compact.json
topology-lantern encode-graph-sequence ota-compact.json --seed 7 \
  --output ota-sequence.json
topology-lantern decode-graph-sequence ota-sequence.json \
  --output ota-reconstructed.json
```

```python
from topology_lantern import (
    compact_graph,
    decode_graph_sequence,
    encode_graph_sequence,
    load_spice,
)

graph = compact_graph(load_spice("examples/circuits/ota.sp", top="ota"))
sequence = encode_graph_sequence(graph, seed=7)
assert decode_graph_sequence(sequence) == graph
```

## Traversal and reconstruction

The encoder finds each connected component of each definition scope. For a
component with `2k` odd-degree vertices it adds `k` temporary pairing edges,
constructs an Euler circuit using iterative Hierholzer traversal, and removes
the temporary edges. The result contains exactly `k` open trails. An even-degree
component has one closed trail. This reaches the lower bound of
`max(1, odd_vertices / 2)` trails for every component containing edges.
Temporary pairing edges are never serialized. Each original incidence appears
exactly once, including distinct terminals tied to the same net.

The traversal uses SHA-256 priorities derived from an unsigned 64-bit seed.
Changing the seed provides reproducible traversal augmentation without changing
the reconstructed source graph. Sorting costs `O(E log E + V log V)` in the
worst case; component discovery and traversal use `O(V + E)` time and storage.
No recursive traversal or permutation enumeration is used. Isolated vertices
remain in the metadata and need no empty trail.

The decoder verifies every step and rebuilds the compact graph, then runs its
full incidence, terminal, global-net, hierarchy, parameter, and content-identity
validation. A separate connected-component degree check requires the minimum
trail count; splitting a valid trail into redundant pieces is rejected even
if a caller recalculates the sequence hash. Reordering a valid traversal is
allowed; losing, duplicating, or
rewiring an incidence fails reconstruction. The `source_graph_id` identifies
semantics and the `sequence_id` identifies the complete sequence including its
seed and metadata. Neither hash proves electrical feasibility.

## Limits and dataset use

The wire envelope is 64 MiB with at most 512 scopes and 500,000 total incidence
steps. The existing compact graph limits additionally bound owners, nets,
parameters, and source labels. JSON readers reject unknown members, duplicate
members, invalid UTF-8, non-finite numbers, and inappropriate scalar types.
Public dataclass decoding and serialization pass through the same checks.
File output uses the CLI's atomic output and permanent input-alias protection.

All traversals of one source graph must stay in the same train/validation/test
partition. A different `sequence_id` is not evidence of a new topology. This
codec prepares graph data; the earlier `tlrs1` rule-sequence baseline operates
on a finite rewrite catalog and is a separate format.

### Reversible device renaming (development)

`rename_devices(graph, seed=7)` creates another identifier view of a circuit
before Euler traversal. SHA-256 priorities choose a scope-local ordering;
primitive-kind prefixes, the complete unsigned 64-bit seed and unique ranks
form the new names. The generated graph is validated like any ingested graph.
No global random state or platform floating-point functions affect the names.

```python
from topology_lantern import rename_devices, restore_device_names

original_circuit = load_spice("examples/circuits/ota.sp", top="ota")
renamed = rename_devices(original_circuit, seed=7)
assert restore_device_names(renamed) == original_circuit
sequence = encode_graph_sequence(compact_graph(renamed.graph), seed=9)
```

The immutable `RenamedCircuit` records the original semantic graph ID, a
full-source evidence digest, seed, renamed graph and one `DeviceRename` per
original primitive. It does not embed the original graph. Restoration checks
complete unique mapping coverage, original name/ID relationships, reconstructed
graph identity, the source digest and exact deterministic replay, rejecting
reordered or inconsistent evidence. The digest covers source paths and locations
that the semantic graph ID deliberately excludes; it is a consistency check, not
authentication or a signature. Names and IDs alone change:
nets, ports, hierarchy instance names, model names, expression strings and source
file/line locations are preserved. Thus this is connectivity-data augmentation,
not a source-netlist rewrite: expressions that refer to device names are not
rewritten, and electrical simulation equivalence is not implied.

Augmentation preflights the same finite graph envelope before constructing a
compact copy: at most 512 scopes, 32 source paths, 100,000 owners and nets,
500,000 incidences, 4,096 parameters in any one map, and 1,000,000 nested
terminal, connection and parameter pairs in total. Every container and pair must
have the immutable tuple shape used by the canonical graph contract.

`RenamedCircuit.source_graph_id` identifies the immediate graph that was
renamed. Likewise, `CircuitGraphSequence.source_graph_id` identifies the
immediate graph that was traversed, so a sequence of a renamed graph contains
the renamed graph ID. The graph-dataset record supplies a separate immutable
`root_lineage_id` and groups all linked renames and traversals before
partitioning. Their changed graph or sequence hashes do not make them new
independent circuits. See the [graph-dataset guide](graph-datasets.md) for the
versioned partition, stack, lineage, and conservative leakage-group contract.

The [Euler sequence schema](schemas/euler-sequence-1.schema.json) defines the
wire shape and reuses the installed connectivity schema for metadata. Runtime
checks prove connectivity and identity constraints that JSON Schema alone does
not express. The tests exhaustively check 64 small multigraphs at two seeds
against a separate connected-component/degree oracle, as well as hierarchy,
parallel terminal incidence, malformed documents, and CLI reconstruction.
