# Changelog

## Unreleased

## 0.3.0 - 2026-09-07

### Added

- `topology-lantern diff`: compare the topologies two specifications admit, matched by the
  identifier-independent signature rather than by candidate number, which follows the ranking
  and moves whenever anything else does. Reports shared topologies with their rank movement,
  and those present on only one side.
- A missing topology is explained at one of four strengths, kept distinct rather than blended.
  A refusal names the constraints that failed a graph the other search actually built. A
  closed-off rule is proved from the missing topology's own trace: it uses a rule the other
  specification governed and never permitted. A rule that every other topology carries and this
  one lacks is reported in weaker wording, because it describes that run's output rather than
  its specification, and it is what a newly required stage looks like. Anything else is
  reported as an absence with no cause to offer.
- A search ledger recording, per rule, the states it produced and the times the specification
  refused it, plus rejection counts by cause, by violation code and by obligation kind, and the
  complete topologies that were built and then refused. The refused record is bounded and says
  when the bound was reached, since an unrecorded signature is not evidence of anything.
- `--ledger` adds that block to a JSON report. It is opt-in because readers of this report
  reject unknown fields, so the default document is unchanged.
- `topology_lantern.compare` and `topology_lantern.ledger` as a Python API: `diff_results`,
  `render_diff`, `SearchLedger`, `RuleLedger`, and `LedgerRecorder`.

## 0.2.0 - 2026-08-31

- Add a deterministic, versioned analog topology-and-sizing benchmark contract.
- Add strict interoperability documentation and validation tests.
- Add bounded strict JSON/report loading, scaling evidence, and synchronized runtime version metadata.

All notable changes are recorded here. Versions follow semantic versioning.

## 0.1.0 - 2026-08-31

### Added

- Strict versioned design-intent schema and canonical fingerprint.
- Immutable typed topology graph, obligations, facts, and trace records.
- Independent input, bias, load, compensation, and output rewrite rules.
- Incremental and final structural constraints with review notes.
- Bounded deterministic best-first search and graph de-duplication.
- Pareto fronts, explicit weighted ranking, JSON, text, and SPICE review output.
- Trace explanation and signature-checked deterministic replay.
