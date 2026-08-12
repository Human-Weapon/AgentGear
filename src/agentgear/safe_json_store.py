"""Atomic, locked, path-contained JSON store — shared persistence primitive.

Used by the checkpoint store and heartbeat writer so concurrency / path /
corruption guarantees are implemented exactly once.

Raises only the neutral ``PersistenceError`` hierarchy (no domain-specific
errors), and re-validates lock/temp/dest containment immediately before
creating any artifact so a post-construction junction swap leaves ZERO
files outside ``trusted_root``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .exceptions import CorruptStorageError, PathEscapeError, PersistenceError, StorageLockError
from .path_security import assert_path_family_contained

_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.05
_REPLACE_RETRY_ATTEMPTS = 20
_REPLACE_RETRY_DELAY_S = 0.02


def _replace_with_retry(src: Path, dst: Path) -> None:
    """Atomically replace ``dst`` with ``src``, retrying transient
    ``PermissionError`` (Windows ``ERROR_ACCESS_DENIED`` / ``WinError 5``).

    Even while the caller holds the store's own file lock, Windows can
    still momentarily deny a rename/replace on a just-written file —
    typically because antivirus real-time scanning or the search indexer
    briefly opened it. This is purely transient OS-level contention on a
    file we already own exclusively; a short bounded retry resolves it
    without masking any real containment or logic error (those raise
    ``PathEscapeError``/other exceptions well before this point).
    """
    last_exc: PermissionError | None = None
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            src.replace(dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt < _REPLACE_RETRY_ATTEMPTS - 1:
                time.sleep(_REPLACE_RETRY_DELAY_S)
    assert last_exc is not None
    raise last_exc


class FileLock:
    """Cross-platform exclusive file lock (Windows msvcrt / POSIX fcntl)."""

    def __init__(
        self,
        lock_path: Path,
        timeout: float = _LOCK_TIMEOUT_S,
        *,
        pre_create_check: Callable[[Path], None] | None = None,
    ) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self._fh = None
        self._pre_create_check = pre_create_check

    def acquire(self) -> None:
        if self._pre_create_check is not None:
            self._pre_create_check(self.lock_path)

        parent = self.lock_path.parent
        if self._pre_create_check is not None:
            self._pre_create_check(parent)
        parent.mkdir(parents=True, exist_ok=True)

        if self._pre_create_check is not None:
            self._pre_create_check(self.lock_path)
            self._pre_create_check(parent)

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if self._pre_create_check is not None:
                    self._pre_create_check(self.lock_path)
                self._fh = open(self.lock_path, "a+b")  # noqa: SIM115
                if os.name == "nt":
                    import msvcrt

                    self._fh.seek(0)
                    if self._fh.tell() == 0:
                        self._fh.write(b"\0")
                        self._fh.flush()
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except PathEscapeError:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
                raise
            except OSError:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
                if time.monotonic() >= deadline:
                    raise StorageLockError(
                        f"Could not acquire lock on {self.lock_path} within {self.timeout}s"
                    ) from None
                time.sleep(_LOCK_POLL_S)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class SafeJsonStore:
    """Process-safe JSON document store with path containment."""

    def __init__(
        self,
        path: str | Path,
        *,
        trusted_root: str | Path | None = None,
        default: Callable[[], Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.trusted_root = Path(trusted_root) if trusted_root is not None else None
        self._default = default or (lambda: {})
        if self.trusted_root is not None:
            self.assert_contained(self.path)

    def assert_contained(self, target: Path) -> None:
        if self.trusted_root is None:
            return
        assert_path_family_contained(target, trusted_root=self.trusted_root)

    def _check_write_paths(self, *extra: Path) -> None:
        if self.trusted_root is None:
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        targets = [self.path, lock_path, self.path.parent, *extra]
        assert_path_family_contained(*targets, trusted_root=self.trusted_root)

    def read(self) -> Any:
        """Read JSON from disk (no lock). Returns default if missing."""
        if not self.path.exists():
            return self._default()
        if self.trusted_root is not None:
            self.assert_contained(self.path)
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            return self._quarantine_and_raise(exc)

    def _quarantine_and_raise(self, exc: BaseException) -> Any:
        quarantined = self.path.with_suffix(self.path.suffix + ".corrupt")
        if self.trusted_root is not None:
            try:
                self.assert_contained(quarantined)
            except PathEscapeError:
                raise CorruptStorageError(
                    f"JSON store at {self.path} is corrupt: {exc}.",
                    quarantined_path=None,
                ) from exc
        try:
            if self.path.exists():
                if quarantined.exists():
                    quarantined = self.path.with_suffix(
                        self.path.suffix + f".corrupt.{os.getpid()}"
                    )
                self.path.rename(quarantined)
            else:
                quarantined = None  # type: ignore[assignment]
        except OSError:
            quarantined = None  # type: ignore[assignment]
        raise CorruptStorageError(
            f"JSON store at {self.path} is corrupt: {exc}. Quarantined.",
            quarantined_path=str(quarantined) if quarantined else None,
        ) from exc

    def quarantine_invalid(self, reason: str) -> None:
        """Quarantine current file and raise CorruptStorageError (schema invalid)."""
        self._quarantine_and_raise(ValueError(reason))

    def write_atomic(self, data: Any) -> None:
        """Write JSON atomically with a unique temp file (caller must hold lock)."""
        self._check_write_paths()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_write_paths()

        payload = json.dumps(data, indent=2, default=str)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=f".{os.getpid()}.tmp",
            dir=str(self.path.parent),
        )
        try:
            self._check_write_paths(Path(tmp_name))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            self._check_write_paths(Path(tmp_name))
            _replace_with_retry(Path(tmp_name), self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def update(self, mutator: Callable[[Any], Any]) -> Any:
        """Lock -> read -> mutator(data) -> write. Returns the new data."""
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._check_write_paths()

        def _pre(p: Path) -> None:
            if self.trusted_root is not None:
                assert_path_family_contained(p, trusted_root=self.trusted_root)

        with FileLock(lock_path, pre_create_check=_pre if self.trusted_root else None):
            self._check_write_paths()
            try:
                current = self.read()
            except CorruptStorageError:
                raise
            new_data = mutator(current)
            try:
                self.write_atomic(new_data)
            except PathEscapeError:
                raise
            except OSError as exc:
                raise PersistenceError(f"Cannot write store {self.path}: {exc}") from exc
            return new_data
