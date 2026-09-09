# Changelog

## 0.5.0 - 2026-09-09

### Added

- Reversible terminal-labelled Euler-trail sequences with minimum trail-count
  construction, deterministic traversal augmentation, exact graph reconstruction,
  a bounded versioned wire format, API/CLI, and exhaustive small-multigraph oracles.
- Lossless compact owner/net and explicit pin/net representations for typed
  circuit graphs, with content identities, stable incidence IDs, cross-scope
  global-net and instance validation, mutual round-trip checks, strict bounded
  JSON loading, lossless CLI transcoding, a versioned JSON schema,
  documentation, and adversarial tests.
- A trusted-base DCO gate whose bounded paginated commit set is bound to the
  final pull-request base/head/count with retarget reruns and pending-status
  reset, fail-closed wheel/sdist member auditing, verified
  release-commit ancestry and signature, a pinned SPDX SBOM whose required
  document-header profile and release-specific file relationships bind the exact
  wheel RECORD and installed-tree contents, an exact four-asset allowlist, and
  checksum revalidation before attestation and publication.

## 0.4.0 - 2026-09-07

### Added

- A resource-bounded, dependency-free hierarchical SPICE connectivity ingest
  with normalized primitives, ports and nets, explicit reference closure,
  positional instance binding, global-net identity, deterministic node IDs,
  and a semantic SHA-256 graph identity.
- Version-1 circuit-graph, layout-constraint, and layout-evidence JSON schemas,
  plus strict runtime node binding and contradiction checks for symmetry,
  common-centroid, matching, alignment, order, keepout, and net-priority intent.
- Conservative current-mirror, differential-pair, and parallel-finger layout
  candidates with confidence and topology evidence. Inference and user intent
  remain separate in both the typed API and serialized report.
- `ingest-spice` and `layout-evidence` CLI commands, a Python API, synthetic
  current-mirror/differential-pair/hierarchical-OTA corpus, checked-in golden
  graph, resource/error/property tests, and a repeatable ingest benchmark.
- Independent per-file and cumulative include read bounds; terminal `.end`
  semantics; NFC/control/bidi identifier defenses; source file/line provenance;
  and case-insensitive `PARAMS:` defaults plus explicit/effective overrides.
- Preflight device, pair-evaluation, and inferred-candidate caps; full public
  constraint revalidation; reverse-order semantic normalization; and bounded
  report arrays.
- Atomic no-clobber output for every CLI command with explicit `--force` and
  permanent input/constraint/spec/report alias protection. Versioned schemas
  are included in the wheel.
- A compact replay-verified `tlrs1` rule sequence, polarity-dual structural
  augmentation with lineage-safe splitting, digest-bound bigram checkpoint,
  exact-budget constraint-guided sampling, and held-out validity/exact-match/
  coverage reports. This is labelled throughout as a deterministic statistical
  baseline rather than a trained ML or analog-performance model.
- Author-matching DCO verification for every pull-request commit,
  cryptographic verification of signed release tags against the repository
  trust file, and pinned-tool SPDX 2.3 SBOM plus verified SHA-256 release
  checksums alongside GitHub build provenance attestations.
- Bounded, NFC-normalized in-memory source labels and terminal-safe escaping of
  Unicode format, bidi-control, and surrogate characters in stored report
  explanations.

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
