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

from .exceptions import InvalidIdentifierError, PathEscapeError

# Round 4 / NEW-08: a persistence identifier (execution_id) becomes a
# literal filename/directory-name component (`{execution_id}.heartbeat.
# json`, `{execution_id}.checkpoints/segment-NNNNNN.json`). A
# permanently-invalid one (too long, illegal characters) must fail
# immediately rather than reach the file-lock retry loop and eventually
# surface a confusing StorageLockError ~10 seconds later. Scope
# deliberately kept to what was actually reproduced as broken (length,
# illegal characters) -- NOT chasing Windows reserved device names
# (CON/NUL/...) or trailing dot/space edge cases, which are real but
# much rarer and not worth the added complexity for v0.1.0.
_MAX_PERSISTENCE_ID_LENGTH = 150
_ILLEGAL_FILENAME_CHARS = frozenset('<>:"/\\|?*') | {chr(c) for c in range(32)}


def validate_persistence_safe_id(name: str, value: str) -> str:
    """Reject an identifier that can never work as a filename/directory-
    name component, before it ever reaches a filesystem call."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidIdentifierError(f"{name} must be a non-empty, non-blank string")
    if len(value) > _MAX_PERSISTENCE_ID_LENGTH:
        raise InvalidIdentifierError(
            f"{name} must be at most {_MAX_PERSISTENCE_ID_LENGTH} characters for use as a "
            f"filename component, got {len(value)}"
        )
    illegal = _ILLEGAL_FILENAME_CHARS & set(value)
    if illegal:
        raise InvalidIdentifierError(
            f"{name} contains characters that are not safe in a filename: {sorted(illegal)!r}"
        )
    return value


def bind_persistence_root(path: str | Path) -> Path:
    """Round 5 / AG5-06: lexically bind a (possibly relative) persistence
    root to an ABSOLUTE path at the moment this is called, using the
    process's CURRENT working directory -- so a later ``os.chdir()``
    elsewhere in the process can never silently redirect an
    already-constructed writer/store to a different location on disk than
    the one its caller actually named.

    This is deliberately LEXICAL binding only (``os.path.abspath``: makes
    the path absolute and normalizes ``.``/``..`` components) -- it does
    NOT follow symlinks/junctions, and is NOT a substitute for
    ``resolve_via_nearest_existing_ancestor``'s per-operation SECURITY
    canonicalization, which still runs on every read/write to catch a
    directory entry swapped for a symlink/junction AFTER construction. The
    two serve different purposes (stable identity vs. per-operation
    tamper detection) and both are needed -- resolving symlinks once at
    construction and trusting that forever would defeat the latter.
    """
    return Path(os.path.abspath(str(path)))


def resolve_canonical(path: str | Path) -> Path:
    """Resolve a path to its canonical form, following symlinks/junctions."""
    resolved = os.path.realpath(str(Path(path)))
    return Path(resolved)


def resolve_via_nearest_existing_ancestor(path: str | Path) -> Path:
    """Round 4 / NEW-08: canonicalize a path that may not exist yet (or
    have any existing ancestor beyond some point) WITHOUT relying on
    ``os.path.realpath``'s behavior for fully-nonexistent multi-level
    paths, which is not consistent enough to compare against a
    similarly-nonexistent root -- comparing two independently-realpath'd
    nonexistent paths could spuriously say a genuinely-contained path
    escapes (or, worse, the reverse).

    Walks up to the nearest ancestor that DOES exist, canonicalizes only
    that real, existing portion (still following any real symlink/
    junction in it -- this is exactly as security-sensitive as before),
    then re-appends the nonexistent trailing components VERBATIM
    (normalized, never resolved -- a component that doesn't exist cannot
    itself be a symlink/junction, so there is nothing to resolve).
    """
    p = Path(path)
    tail: list[str] = []
    cur = p
    while not cur.exists():
        parent = cur.parent
        if parent == cur:  # reached a filesystem root that still doesn't exist
            break
        tail.append(cur.name)
        cur = parent
    resolved = resolve_canonical(cur)
    for name in reversed(tail):
        resolved = resolved / name
    return resolved


def normalize_path_key(path: str | Path) -> str:
    """Normalize for comparison (case on Windows, slash style)."""
    s = os.path.normpath(str(path))
    if os.name == "nt":
        s = os.path.normcase(s)
    return s


def validate_contained(target: str | Path, base: str | Path) -> Path:
    """Validate that ``target`` resolves to a path inside ``base``.

    Both paths are canonicalised via the nearest EXISTING ancestor
    (Round 4 / NEW-08: symlinks/junctions in the existing portion are
    still resolved; a nonexistent trailing tail is preserved verbatim
    rather than fed to ``os.path.realpath``, whose behavior for a fully
    nonexistent multi-level path is not consistent enough to compare
    against an equally nonexistent base). Raises ``PathEscapeError`` if
    the target escapes. Returns the canonical target path on success.
    """
    base_canonical = resolve_via_nearest_existing_ancestor(base)
    target_canonical = resolve_via_nearest_existing_ancestor(target)

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
    """Validate every target stays in root, root and target both resolved
    via the nearest-existing-ancestor rule (see ``validate_contained``)."""
    root = resolve_via_nearest_existing_ancestor(trusted_root)
    for target in targets:
        resolved = resolve_via_nearest_existing_ancestor(target)
        validate_contained(resolved, root)


def safe_join(base: str | Path, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and validate the result is contained."""
    base_path = Path(base)
    joined = base_path.joinpath(*parts)
    return validate_contained(joined, base_path)
