# Replayable rule-sequence statistical baseline

TopologyLantern 0.4 includes a deliberately small end-to-end baseline for
experiments on its finite rewrite space. It is an auditable statistical
baseline, not a trained neural model, circuit-performance predictor, or claim
of parity with research-scale generative EDA systems.

## Compact and reversible representation

`CompactRuleSequence` stores four bounded fields in a `tlrs1` record: the
design-specification SHA-256, final topology signature, and ordered rewrite-rule
IDs. The compact record is reversible only together with its exact design
specification. Decoding replays every rule through the normal predicates,
requires all obligations to be resolved, reruns final structural constraints,
and checks the topology signature. It does not deserialize a trusted topology
blob.

```python
from topology_lantern import encode_rule_sequence, generate_candidates, replay_compact_sequence
from topology_lantern.spec import DesignSpec

spec = DesignSpec.from_json("examples/low_voltage_diff_stage.json")
candidate = generate_candidates(spec, limit=1).candidates[0]
compact = encode_rule_sequence(spec, candidate).as_text()
assert replay_compact_sequence(spec, compact).topology == candidate.topology
```

## Augmentation and leakage boundary

`build_sequence_examples(..., augment_polarity=True)` optionally creates the
structural polarity dual when both MOS families are allowed. It does not claim
that the two circuits have equivalent simulated behavior. The base and dual
share a `lineage_id`; cosmetic specification names, allowed-device order, and
polarity are removed from that grouping digest. `split_sequence_examples`
assigns complete lineages, never individual candidates, to training or held-out
sets. Training and evaluation independently fail if any lineage crosses the
boundary.

This conservative grouping prevents the built-in augmentation from leaking.
It cannot discover undocumented relationships between independently supplied
third-party datasets; callers must assign or pre-group those lineages before
claiming a leakage-free research evaluation.

## Checkpoint and constrained sampling

Training counts adjacent verified rule IDs. The version-1 checkpoint records
canonical positive transition counts, training lineages, specification
fingerprints, sequence hashes, a model-kind label, and a SHA-256 over the whole
checkpoint body. There are no tensors, gradient optimization, or hidden
weights. Loading rejects unknown fields, invalid types, inconsistent start
counts, non-canonical arrays, and digest tampering.

At sampling time, Laplace-smoothed transition counts rank only the rules that
the current obligation and design specification permit. A SHA-256-derived,
seeded ticket makes each draw deterministic across runs. Every draw follows the
ordinary partial and final structural checks and is either a replayable `tlrs1`
sequence or an explicit rejection reason. The requested draw budget is exact;
rejected paths are never silently replaced by extra attempts.

```console
topology-lantern baseline-train examples/low_voltage_diff_stage.json \
  --heldout-spec examples/compensated_pmos_stage.json \
  --augment-polarity --output baseline-checkpoint.json

topology-lantern baseline-sample baseline-checkpoint.json \
  examples/compensated_pmos_stage.json --draws 32 --seed 7 \
  --output samples.json

topology-lantern baseline-evaluate baseline-checkpoint.json \
  examples/compensated_pmos_stage.json --augment-polarity \
  --draws-per-spec 32 --seed 7 --output heldout-metrics.json
```

`baseline-evaluate` reports exact denominators for constraint-valid draw rate,
exact held-out sequence draw rate, and distinct held-out sequence coverage. The
metrics concern the finite structural rule catalog only. They are not gain,
bandwidth, noise, yield, PVT, layout, or silicon metrics.

The public contracts are:

- `schemas/rule-sequence-checkpoint-1.schema.json`;
- `schemas/rule-sequence-samples-1.schema.json`; and
- `schemas/rule-sequence-evaluation-1.schema.json`.

They ship inside wheels under `topology_lantern/schemas/`.
