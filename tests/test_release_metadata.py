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

Round 5 traceability micro-fix: the audit index table's "Fix commit"
COLUMN must never contain a placeholder, but the DESCRIPTION column is
allowed -- and in AG5-09's own row, REQUIRED -- to name that placeholder
text as the historical subject of the finding it describes (a document
recording "this bug used to say X" must be allowed to literally say X). A
naive whole-file substring search cannot tell these two cases apart and
was itself the bug a prior finalization pass tripped over (it blanket-
replaced every occurrence of the placeholder text, including inside
AG5-09's own historical description, corrupting it into a factually wrong
claim). These checks parse the markdown table structurally and only
inspect the "Fix commit" column.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_PLACEHOLDERS = ("(this round)", "uncommitted", "TBD SHA", "FIXME SHA")
_SEPARATOR_ROW = re.compile(r"^\|[\s:-]+(\|[\s:-]+)*\|$")


def _parse_table_rows(text: str) -> list[list[str]]:
    """Parse every 5-column markdown table row (``| ID | Description |
    Classification | Fix commit | Regression test(s) |``) in the audit
    index, skipping header separator rows (``|---|---|---|---|---|``).
    Returns each row as a list of 5 stripped cell strings.
    """
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        if _SEPARATOR_ROW.fullmatch(stripped):
            continue
        cells = [cell.strip() for cell in stripped.split("|")[1:-1]]
        if len(cells) == 5 and cells[0] != "ID":  # skip the literal header row too
            rows.append(cells)
    return rows


def test_readme_points_at_the_real_promptgraph_repo() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Human-Weapon/PromptGraph" in readme
    assert "hermes-oss/promptgraph" not in readme.lower()


def test_readme_does_not_reference_the_dead_agentgear_org() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "hermes-oss/agentgear" not in readme.lower()


def test_security_policy_accurately_scopes_persistence_containment() -> None:
    """The policy must not promise a race-free filesystem sandbox when
    path_security.py explicitly documents best-effort TOCTOU reduction."""
    security = (_REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "best-effort TOCTOU" in security
    assert "sufficiently privileged local attacker" in security
    assert "writes outside its configured `state_dir`" not in security


def test_audit_index_fix_commit_column_has_no_unfinalized_placeholder() -> None:
    """Round 5 / AG5-09: every finding's "Fix commit" COLUMN must name a
    real, immutable commit (or an explicit non-SHA classification like
    "—" for a NOT REPRODUCIBLE finding) -- never a placeholder like
    "(this round)" or "uncommitted", which only make sense while a round
    is still being worked on, never in the pushed HEAD a sixth audit
    would review. Scoped to ONLY the Fix-commit column (index 3) -- the
    Description column is checked separately below, since it is allowed
    to name that same placeholder text historically.
    """
    index = (_REPO_ROOT / "docs" / "audits" / "index.md").read_text(encoding="utf-8")
    rows = _parse_table_rows(index)
    assert rows, "no table rows parsed -- check the parser against the current table format"
    for row_id, _description, _classification, fix_commit, _regression in rows:
        for placeholder in _PLACEHOLDERS:
            assert placeholder not in fix_commit, (
                f"{row_id}'s Fix commit column still contains a placeholder: {fix_commit!r}"
            )


def test_audit_index_fix_commit_column_is_a_sha_or_intentional_classification() -> None:
    """Every Fix commit cell is either a backtick-wrapped short hex SHA
    (`a7e59b7`) or the literal em-dash "—" used for a finding explicitly
    classified NOT REPRODUCIBLE (no fix commit exists because there was
    nothing to fix) -- never blank or otherwise malformed."""
    index = (_REPO_ROOT / "docs" / "audits" / "index.md").read_text(encoding="utf-8")
    rows = _parse_table_rows(index)
    sha_pattern = re.compile(r"^`[0-9a-f]{7,40}`(\s*/\s*`[0-9a-f]{7,40}`)?$")
    for row_id, _description, _classification, fix_commit, _regression in rows:
        assert fix_commit == "—" or sha_pattern.match(fix_commit), (
            f"{row_id}'s Fix commit column is neither a real SHA nor the NOT-REPRODUCIBLE "
            f"marker: {fix_commit!r}"
        )


def test_ag5_09_description_historically_names_the_placeholder_text() -> None:
    """The AG5-09 finding IS the fact that the index contained
    "(this round)" placeholders -- its own Description column must keep
    saying so. This is the positive counterpart to the placeholder-
    absence check above: a description that no longer names what the bug
    actually was is just as wrong as a live placeholder would be."""
    index = (_REPO_ROOT / "docs" / "audits" / "index.md").read_text(encoding="utf-8")
    rows = _parse_table_rows(index)
    ag5_09_rows = [row for row in rows if row[0] == "AG5-09"]
    assert len(ag5_09_rows) == 1, f"expected exactly one AG5-09 row, found {len(ag5_09_rows)}"
    _row_id, description, _classification, fix_commit, _regression = ag5_09_rows[0]
    assert "(this round)" in description, (
        f"AG5-09's description no longer names the historical placeholder text: {description!r}"
    )
    assert fix_commit == "`a7e59b7`", (
        f"AG5-09's Fix commit column should be the real Round 5 code-fix SHA, got {fix_commit!r}"
    )
