from __future__ import annotations

import re
from pathlib import Path


def test_actions_are_pinned_to_full_commits() -> None:
    workflows = tuple(Path(".github/workflows").glob("*.yml"))
    assert workflows
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("uses:"):
                reference = line.partition("uses:")[2].strip()
                assert re.fullmatch(r"[^\s@]+@[0-9a-f]{40}(?:\s+#.*)?", reference), reference


def test_release_requires_main_push_ci_for_the_exact_signed_commit() -> None:
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "git verify-tag" in release
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in release
    assert '.event == "push"' in release or '.event == \\"push\\"' in release
    assert '.head_branch == "main"' in release or '.head_branch == \\"main\\"' in release
    assert '.head_sha == \\"$GITHUB_SHA\\"' in release
    assert release.index("git verify-tag") < release.index("Run release quality gate")
    assert release.index("git show origin/main:.github/allowed_signers") < release.index(
        "git verify-tag"
    )
    assert "path: .release-smoke" in release
    assert "bind-installed-wheel" in release
    assert release.count("verify-release") == 3
