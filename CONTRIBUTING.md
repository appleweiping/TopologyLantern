# Contributing

TopologyLantern welcomes focused work that makes conceptual choices more
explicit, bounded, and reviewable.

## Setup

Use Python 3.11 or newer in an isolated environment:

```console
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python -m build
```

Runtime dependencies require prior discussion because deterministic offline
operation is part of the project contract.

## Rule contributions

Open an issue describing the obligation, conceptual circuit family, explicit
applicability conditions, facts produced, structural tradeoffs, and cases that
must be rejected. Do not claim performance without a model and validity range.

New behavior requires original fixtures and focused tests for applicability,
negative conditions, graph invariants, canonical identity, constraints, trace
content, replay, ordering, and search limits. Keep rule transforms independent
and deterministic. Update architecture documentation and the changelog when a
public schema, rule, metric, or CLI behavior changes.

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
