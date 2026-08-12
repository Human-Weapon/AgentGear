"""Path containment validation.

Ensures that paths used by persistent writers (checkpoints, heartbeats) do
not escape their intended base directory through symlinks, Windows
junctions, or reparse points.

Threat model (documented): we re-validate containment immediately before
creating lock/temp/destination artifacts. A fully race-free guarantee
against a privileged concurrent attacker swapping directory entries between
every syscall is not promised on all platforms; the contract is best-effort
TOCTOU reduction so that rejected operations leave ZERO artifacts outside
trusted_root under the tested junction/symlink swap scenarios.
"""

from __future__ import annotations

import os
from pathlib import Path

from .exceptions import PathEscapeError


def resolve_canonical(path: str | Path) -> Path:
    """Resolve a path to its canonical form, following symlinks/junctions."""
    resolved = os.path.realpath(str(Path(path)))
    return Path(resolved)


def normalize_path_key(path: str | Path) -> str:
    """Normalize for comparison (case on Windows, slash style)."""
    s = os.path.normpath(str(path))
    if os.name == "nt":
        s = os.path.normcase(s)
    return s


def validate_contained(target: str | Path, base: str | Path) -> Path:
    """Validate that ``target`` resolves to a path inside ``base``.

    Both paths are canonicalised (symlinks/junctions resolved) before
    comparison. Raises ``PathEscapeError`` if the target escapes. Returns
    the canonical target path on success.
    """
    base_canonical = resolve_canonical(base)
    target_canonical = resolve_canonical(target)

    base_cmp = normalize_path_key(base_canonical)
    tgt_cmp = normalize_path_key(target_canonical)

    try:
        Path(tgt_cmp).relative_to(Path(base_cmp))
    except ValueError as exc:
        raise PathEscapeError(
            f"Path '{target}' resolves to '{target_canonical}' which is "
            f"outside the allowed base '{base_canonical}'."
        ) from exc

    return target_canonical


def assert_path_family_contained(*targets: str | Path, trusted_root: str | Path) -> None:
    """Validate every target (and deepest existing ancestor) stays in root."""
    root = resolve_canonical(trusted_root)
    for target in targets:
        t = Path(target)
        cur = t
        while True:
            if cur.exists():
                validate_contained(cur, root)
                break
            if cur.parent == cur:
                break
            cur = cur.parent

        resolved = resolve_canonical(t)
        validate_contained(resolved, root)

        base_cmp = normalize_path_key(root)
        tgt_cmp = normalize_path_key(resolved)
        try:
            Path(tgt_cmp).relative_to(Path(base_cmp))
        except ValueError as exc:
            raise PathEscapeError(
                f"Path '{target}' resolves to '{resolved}' outside '{root}'."
            ) from exc


def safe_join(base: str | Path, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and validate the result is contained."""
    base_path = Path(base)
    joined = base_path.joinpath(*parts)
    return validate_contained(joined, base_path)
