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
