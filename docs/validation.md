# Benchmark validation

The benchmark command regenerates candidates from the source specification and
binds their canonical signatures into a strict version-1 JSON contract.
Determinism is checked by byte-stable canonical content and SHA-256. Tests
verify candidate order, IDs, count, CLI output, and repeat generation.

This validates software reproducibility only. No PDK, device model, simulator,
silicon measurement, or electrical sign-off data is used.

Specification and result-report JSON inputs are bounded to 1 MiB and parsed
with duplicate-key, non-finite-number, 64-level nesting, and 10,000-value
limits. Parser recursion, numeric overflow, and malformed UTF-8 are returned as
ordinary input errors rather than escaping through the CLI.
