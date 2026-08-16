"""Static checks against the repo's own current-facing docs/metadata --
independent of and in addition to CI's build-artifact inspection (which
checks the built wheel/sdist). These run locally and fast, without a
build step, so a stale link or an unfinalized audit-index placeholder is
caught before it ever reaches CI.

Round 5 / AG5-08 + AG5-09 + section 30. Historical audit documents
(docs/audits/remediation-round-*.md) are deliberately NOT checked here --
they legitimately record what an OLD, already-fixed finding looked like
(e.g. quoting the dead ``hermes-oss/promptgraph`` URL as the bug being
described), which is exactly the "historical text" the spec says must not
be blindly flagged. Only genuinely current-facing surfaces are checked:
README.md and the audit traceability index's own "Fix commit" columns.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_points_at_the_real_promptgraph_repo() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Human-Weapon/PromptGraph" in readme
    assert "hermes-oss/promptgraph" not in readme.lower()


def test_readme_does_not_reference_the_dead_agentgear_org() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "hermes-oss/agentgear" not in readme.lower()


def test_audit_index_has_no_unfinalized_placeholder_shas() -> None:
    """Round 5 / AG5-09: every finding's "Fix commit" column must name a
    real, immutable commit -- not a placeholder like "(this round)" or
    "uncommitted", which only make sense while a round is still being
    worked on, never in the pushed HEAD a sixth audit would review."""
    index = (_REPO_ROOT / "docs" / "audits" / "index.md").read_text(encoding="utf-8")
    for placeholder in ("(this round)", "uncommitted", "TBD SHA", "FIXME SHA"):
        assert placeholder not in index, f"unfinalized placeholder still present: {placeholder!r}"
