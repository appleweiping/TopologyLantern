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

Every pull-request commit must carry a Developer Certificate of Origin
`Signed-off-by` trailer that exactly matches its Git author name and email.
Create it with `git commit -s`; if you amend or rebase commits, preserve the
matching trailer on each rewritten commit. CI checks the complete PR commit
range rather than only the tip commit. The required DCO workflow runs from the
protected base revision, fetches bounded commit metadata through the GitHub
API, and binds the unique ordered list and declared count to the PR head before
checking final trailer paragraphs; a pull request cannot replace its verifier.
The base repository/ref/SHA, head SHA and count are checked before and after
download and again before publishing `DCO / commits`. Retarget edits rerun the
gate and reset the event head to pending, so an earlier pass cannot stand in for
a different commit set. Protected main requires this trusted status; the former
PR-checkout CI DCO job has been retired.

Releases are made only from signed tags whose commits are reachable from
protected `main` and have a GitHub-verified signature. The release workflow
audits every wheel/sdist member, smoke-tests the isolated wheel, catalogs that
installed tree into a pinned-tool SPDX 2.3 SBOM, and rechecks the exact asset
checksums before provenance attestation and immutable publication.

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
