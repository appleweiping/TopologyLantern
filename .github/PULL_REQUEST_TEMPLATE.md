## Change

Describe the design intent, obligation, graph edit, or invariant being changed.

## Evidence

- [ ] Applicability, rejection, boundary, and graph tests added or updated
- [ ] Canonical identity, ranking order, and replay considered
- [ ] No electrical performance is claimed without an explicit model
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest` passes with branch coverage
- [ ] `python -m build` succeeds

## Search and security boundary

State the effect on state branching, limits, determinism, file access, and any
new input surface. Confirm generation does not execute submitted content.
