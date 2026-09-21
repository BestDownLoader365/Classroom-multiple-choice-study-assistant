"""Timestamped, verified SQLite backups, shared by the app and the CLIs.

Every command that can modify a database — the startup migration, the explicit
migration CLI, the namespace rename and the course deletion — must be able to
produce the same artifact first, with the same naming scheme, the same atomicity
and the same verification.  Keeping one implementation here is what makes the
startup path as safe as the explicit one used to be.

Three properties matter and are all tested:

* **consistency** — the copy is taken with SQLite's own online backup API, not
  with ``shutil.copy2``: a plain file copy of a database that another process is
  writing can capture a torn page, while the API produces a transactionally
  consistent snapshot.
* **atomicity** — the snapshot is written to a hidden temporary name next to the
  target and moved into place with ``os.replace``, so a crash never leaves a
  partial file that *looks* like a usable backup.
* **usability** — the finished file is verified with ``PRAGMA quick_check``
  before it is published, and a backup that is not readable is deleted and
  reported instead of being handed to an operator as a recovery option.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

#: Suffix appended to the database name, before the UTC timestamp.
BACKUP_MARKER = ".bak-"

#: How many ``-N`` suffixes to try before giving up on a name collision.  A
#: collision only happens when a backup is taken twice in the same second *and*
#: the earlier file is still there.
MAX_NAME_ATTEMPTS = 100


class BackupError(RuntimeError):
    """Raised when a usable backup could not be produced.

    Callers must treat this as fatal for their own operation: the safety rule is
    that a failure here never leaves a database modified without a backup.
    """


def backup_name(database_path: Path, stamp: str, ordinal: int = 0) -> Path:
    """Return the backup path for one timestamp (``-1``, ``-2``, … on collision)."""
    suffix = f"{BACKUP_MARKER}{stamp}" + ("" if ordinal == 0 else f"-{ordinal}")
    return database_path.with_suffix(database_path.suffix + suffix)


def timestamped_backup(
    database_path: Path,
    *,
    verify: bool = True,
    stamp: str | None = None,
) -> Path:
    """Snapshot ``database_path`` next to itself and return the new path.

    The name carries the UTC timestamp (``mcq.db.bak-20260921T120000Z``) and an
    existing backup is **never** overwritten: a second backup inside the same
    second gets a ``-1`` suffix.  ``stamp`` exists so tests can be deterministic;
    production callers leave it unset.
    """
    database_path = Path(database_path)
    if not database_path.is_file():
        raise BackupError(f"无法备份：数据库不存在：{database_path}")
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    target = None
    for ordinal in range(MAX_NAME_ATTEMPTS):
        candidate = backup_name(database_path, stamp, ordinal)
        if not candidate.exists():
            target = candidate
            break
    if target is None:  # pragma: no cover - would need 100 backups in one second
        raise BackupError(
            f"无法备份：{database_path} 在 {stamp} 附近产生了过多同名备份。"
        )

    temporary = target.with_name(f".{target.name}.tmp")
    try:
        _snapshot(database_path, temporary)
        if verify:
            _verify(temporary)
        os.replace(temporary, target)
    except BaseException:
        # Never leave a half-written file behind: it would be indistinguishable
        # from a real backup to an operator in a hurry.
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(target, 0o600)
    _fsync_file(target)
    _fsync_directory(target.parent)
    return target


def _snapshot(source: Path, destination: Path) -> None:
    """Copy a consistent snapshot of ``source`` into ``destination``."""
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise BackupError(f"无法打开数据库进行备份：{source}（{exc}）") from exc
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    except sqlite3.Error as exc:
        raise BackupError(
            f"备份 {source} 失败（{exc}）。数据库未被修改，请检查磁盘空间与权限。"
        ) from exc
    finally:
        source_connection.close()


def _verify(path: Path) -> None:
    """Prove a freshly written backup is a readable database."""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise BackupError(f"备份无法打开校验：{path}（{exc}）") from exc
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"备份校验无法执行（quick_check）：{path}（{exc}）") from exc
    finally:
        connection.close()
    result = None if row is None else str(row[0])
    if result != "ok":
        raise BackupError(f"备份校验未通过（quick_check={result!r}）：{path}")


def _fsync_file(path: Path) -> None:
    """Persist a file's bytes, where the filesystem supports it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - defensive
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - some filesystems refuse this
        pass
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename that published the backup, where supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:  # pragma: no cover - defensive
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - some filesystems refuse this
        pass
    finally:
        os.close(descriptor)
