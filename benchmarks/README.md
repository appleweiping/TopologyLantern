# Analog topology-and-sizing benchmark

`manifest.json` is deterministically generated from the MIT-licensed local
`examples/low_voltage_diff_stage.json` input with:

```console
topology-lantern benchmark examples/low_voltage_diff_stage.json --limit 4 --pretty
```

It records four structural candidates, bounded sizing domains, five expected
proxy metrics, and a SHA-256 digest over the canonical contract body. The
metrics are algorithm-test proxies, not simulated circuit performance.

The public Draft 2020-12 schema is
`docs/schemas/analog-sizing-benchmark-1.schema.json`. Schema validation checks
the portable document shape; deterministic replay and digest verification in
the runtime are deliberately stricter.

`python benchmarks/scaling.py --limits 1,2,4 --repetitions 5` records
environment and workload hashes, candidate/exploration invariants, and
informational timing over increasing search limits. It also records the installed
distribution version and independent content hashes for the imported Python package tree
and executing harness. Timing is never a pass/fail criterion.

`python benchmarks/circuit_ingest.py examples/circuits/ota.sp --top ota
--repetitions 100` records the graph ID, graph cardinality, and min/median/p95
wall-clock time for the hierarchical SPICE ingest. The graph identity is checked
on every repetition, so the timing loop also detects non-deterministic output.

`python benchmarks/graph_sequence.py examples/circuits/ota.sp --top ota
--repetitions 5` checks exact reconstruction of the compact graph from Euler
trails on every repetition. It records implementation/harness hashes, semantic
and sequence identities, scope/owner/incidence/trail counts, serialized bytes,
and encode/decode timings. The same command accepts larger user netlists within
the documented ingest limits. Timings remain machine-specific observations.

For a reproducible generated workload without saving thousands of fixture lines,
run `python benchmarks/graph_sequence.py --chain-owners 10000 --repetitions 5`.
The harness constructs an original resistor chain in memory: 10,000 owners,
20,000 distinct terminal incidences, and one minimal open trail. It supports
1–10,000 owners; semantic/implementation/harness hashes bind the reported work.
