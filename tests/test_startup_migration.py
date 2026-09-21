"""Startup schema migration safety: probes, policies, backups, concurrency.

The behaviour under test replaced an unconditional "migrate whatever we find on
startup, with no backup".  Every case here checks a *state*, not just a return
value: the database's own bytes or schema version, the presence/absence and
validity of the timestamped backup, and what the caller was told.

Databases are always copies built inside ``tmp_path``; the repository's own
fixtures are never touched.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading

import pytest

from app.config import (
    AUTO_MIGRATE_VARIABLE,
    ENVIRONMENT_VARIABLE,
    ConfigurationError,
    McqEnvironment,
    resolve_startup_migration_policy,
)
from app.repositories import Database
from app.repositories import database_backup
from app.repositories.database_backup import BackupError, timestamped_backup
from app.repositories.schema_migrations import (
    LEGACY_COURSE_KEY,
    SCHEMA_VERSION,
    STAGE_AFTER_BACKUP,
    STAGE_BEFORE_COMMIT,
    SCHEMA_VERSION_KEY,
    STAGE_AFTER_CREATE,
    StartupMigrationPolicy,
    StartupMigrationRefused,
    ensure_schema,
    probe_schema,
    read_meta,
)
from tests.test_course_migration import build_legacy_database

#: Every environment variable the resolution reads.
_MANAGED = (ENVIRONMENT_VARIABLE, AUTO_MIGRATE_VARIABLE, "MCQ_SECRET_KEY")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in _MANAGED:
        monkeypatch.delenv(name, raising=False)
    yield


def _legacy_copy(tmp_path):
    """A *copy* of the legacy fixture, so the original stays untouched."""
    original = build_legacy_database(tmp_path / "original.db")
    work = tmp_path / "mcq.db"
    shutil.copy2(original, work)
    return work


def _schema_version(database_path) -> int | None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return read_meta(connection, SCHEMA_VERSION_KEY)
    finally:
        connection.close()


def _backups(directory):
    return sorted(directory.glob("*.bak-*"))


def _inventory(database_path) -> tuple[tuple[str, ...], dict[str, int]]:
    """Return the table names and per-table row counts of a database.

    Backups are compared *logically* rather than byte-for-byte on purpose: the
    online backup API copies pages, and SQLite's file-change counter is part of
    the file header, so two logically identical databases legitimately differ in a
    few header bytes.  A logical comparison proves the same thing (the backup
    holds the pre-migration data) without depending on SQLite's internals.
    """
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = tuple(
            sorted(
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )
        )
        counts = {
            name: int(
                connection.execute(f'SELECT COUNT(*) AS total FROM "{name}"').fetchone()[
                    "total"
                ]
            )
            for name in tables
        }
        return tables, counts
    finally:
        connection.close()


# ------------------------------------------------------------------ probe


def test_probe_reports_a_fresh_database_as_needing_nothing(tmp_path):
    probe = probe_schema(tmp_path / "absent.db")
    assert probe.database_existed is False
    assert probe.fresh is True
    assert probe.needs_migration is False
    assert not (tmp_path / "absent.db").exists(), "probing must not create the file"


def test_probe_reports_a_current_database_as_needing_nothing(tmp_path):
    database = tmp_path / "mcq.db"
    ensure_schema(database, policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE)
    probe = probe_schema(database)
    assert probe.database_existed is True
    assert probe.schema_version == SCHEMA_VERSION
    assert probe.missing_tables == ()
    assert probe.needs_migration is False


def test_probe_reports_a_legacy_database_as_needing_migration(tmp_path):
    probe = probe_schema(_legacy_copy(tmp_path))
    assert probe.needs_migration is True
    assert probe.schema_version is None


def test_probe_reports_a_current_database_with_a_missing_table(tmp_path):
    """Same version but incomplete layout still needs DDL, so it still backs up."""
    database = tmp_path / "mcq.db"
    ensure_schema(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE question_registry")
        connection.commit()
    finally:
        connection.close()

    probe = probe_schema(database)
    assert probe.schema_version == SCHEMA_VERSION
    assert "question_registry" in probe.missing_tables
    assert probe.needs_migration is True


# ------------------------------------------------------------------ backups


def test_timestamped_backup_is_consistent_verified_and_never_overwrites(tmp_path):
    database = _legacy_copy(tmp_path)
    first = timestamped_backup(database, stamp="20260101T000000Z")
    second = timestamped_backup(database, stamp="20260101T000000Z")

    assert first.name == "mcq.db.bak-20260101T000000Z"
    assert second.name == "mcq.db.bak-20260101T000000Z-1"
    for backup in (first, second):
        assert backup.is_file()
        connection = sqlite3.connect(backup)
        try:
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            connection.close()
    # No half-written temporary file is left behind.
    assert list(tmp_path.glob(".*.tmp")) == []


def test_backup_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    database = _legacy_copy(tmp_path)

    def explode(source, destination):  # noqa: ANN001
        destination.write_bytes(b"half a database")
        raise BackupError("disk full")

    monkeypatch.setattr(database_backup, "_snapshot", explode)
    with pytest.raises(BackupError):
        timestamped_backup(database, stamp="20260101T000000Z")
    assert list(tmp_path.glob("*.bak-*")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_backup_of_a_missing_database_is_refused(tmp_path):
    with pytest.raises(BackupError):
        timestamped_backup(tmp_path / "absent.db")


def test_unverifiable_backup_is_rejected(tmp_path, monkeypatch):
    database = _legacy_copy(tmp_path)

    def corrupt(source, destination):  # noqa: ANN001
        destination.write_bytes(b"not a database at all" * 32)

    monkeypatch.setattr(database_backup, "_snapshot", corrupt)
    with pytest.raises(BackupError) as excinfo:
        timestamped_backup(database, stamp="20260101T000000Z")
    assert "quick_check" in str(excinfo.value)
    assert list(tmp_path.glob("*.bak-*")) == []


# ----------------------------------------------------------------- policies


def test_current_schema_never_creates_a_backup(tmp_path):
    """A normal startup must stay write-free: no backup, no extra files."""
    database = tmp_path / "mcq.db"
    first = ensure_schema(database)
    before = database.read_bytes()

    second = ensure_schema(database)
    assert _backups(tmp_path) == []
    assert first.migrated is False
    assert second.migrated is False
    assert second.backup_path is None
    assert database.read_bytes() == before


def test_fresh_database_never_creates_a_backup(tmp_path):
    database = tmp_path / "mcq.db"
    info = ensure_schema(database)
    assert info.database_existed is False
    assert info.migrated is False
    assert info.backup_path is None
    assert _backups(tmp_path) == []


def test_refuse_policy_aborts_without_touching_the_database(tmp_path):
    database = _legacy_copy(tmp_path)
    before = database.read_bytes()

    with pytest.raises(StartupMigrationRefused) as excinfo:
        ensure_schema(database, policy=StartupMigrationPolicy.REFUSE)

    message = str(excinfo.value)
    assert str(database) in message
    assert "migrate_courses.py" in message
    assert "MCQ_ENV=development" in message
    # Nothing was written and no backup was taken.
    assert database.read_bytes() == before
    assert _backups(tmp_path) == []
    assert probe_schema(database).needs_migration is True


def test_backup_and_migrate_takes_one_verified_backup_then_migrates(tmp_path):
    database = _legacy_copy(tmp_path)
    before = _inventory(database)

    info = ensure_schema(database, policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE)

    assert info.migrated is True
    assert info.backup_path is not None
    assert info.backup_path.is_file()
    # The backup holds the *pre*-migration data, and is itself readable.
    assert _inventory(info.backup_path) == before
    assert _schema_version(database) == str(SCHEMA_VERSION)
    connection = sqlite3.connect(info.backup_path)
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert read_meta(connection, LEGACY_COURSE_KEY) is None
    finally:
        connection.close()


def test_external_backup_policy_migrates_without_a_second_backup(tmp_path):
    """The CLI already printed a backup, so ensure_schema must not duplicate it."""
    database = _legacy_copy(tmp_path)
    info = ensure_schema(
        database, policy=StartupMigrationPolicy.MIGRATE_AFTER_EXTERNAL_BACKUP
    )
    assert info.migrated is True
    assert info.backup_path is None
    assert _backups(tmp_path) == []


def test_backup_failure_aborts_before_any_migration(tmp_path):
    """The safety rule: no backup, no migration.  Ever."""
    database = _legacy_copy(tmp_path)
    before = database.read_bytes()

    def explode(_path):  # noqa: ANN001
        raise BackupError("simulated disk failure")

    with pytest.raises(BackupError):
        ensure_schema(
            database,
            policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE,
            backup=explode,
        )

    assert database.read_bytes() == before, "the database must be un-migrated"
    assert probe_schema(database).needs_migration is True
    assert _backups(tmp_path) == []


def test_migration_failure_leaves_a_valid_backup_and_an_unmigrated_database(tmp_path):
    """After a failed migration the state is explainable and recoverable."""
    database = _legacy_copy(tmp_path)
    before = _inventory(database)

    def abort(stage: str) -> None:
        if stage == STAGE_BEFORE_COMMIT:
            raise RuntimeError("simulated crash inside the migration")

    with pytest.raises(RuntimeError):
        ensure_schema(
            database,
            policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE,
            failpoint=abort,
        )

    backups = _backups(tmp_path)
    assert len(backups) == 1
    assert _inventory(backups[0]) == before
    # The transaction rolled back: the database still needs the migration.
    assert probe_schema(database).needs_migration is True

    # Re-running reaches the documented end state.
    info = ensure_schema(database, policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE)
    assert info.migrated is True
    assert len(_backups(tmp_path)) == 2


def test_crash_right_after_the_backup_is_recoverable(tmp_path):
    database = _legacy_copy(tmp_path)
    before = _inventory(database)

    def abort(stage: str) -> None:
        if stage == STAGE_AFTER_BACKUP:
            raise SystemExit(9)  # models a process killed between backup and commit

    with pytest.raises(SystemExit):
        ensure_schema(
            database,
            policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE,
            failpoint=abort,
        )
    backups = _backups(tmp_path)
    assert len(backups) == 1 and _inventory(backups[0]) == before
    assert probe_schema(database).needs_migration is True

    info = ensure_schema(database, policy=StartupMigrationPolicy.BACKUP_AND_MIGRATE)
    assert info.migrated is True


def test_migration_is_idempotent_across_repeated_starts(tmp_path):
    database = _legacy_copy(tmp_path)
    assert ensure_schema(database).migrated is True
    assert ensure_schema(database).migrated is False
    assert ensure_schema(database).migrated is False
    assert len(_backups(tmp_path)) == 1, "only the first start needs a backup"


def test_in_memory_database_is_treated_as_fresh(tmp_path):
    """``:memory:`` is not a supported deployment, but it must not crash a caller.

    Every ``Database.connect()`` opens its own connection, so a memory path cannot
    back a real deployment.  The probe therefore reports "nothing to migrate"
    instead of trying to snapshot a file that is not there.
    """
    probe = probe_schema(":memory:")
    assert probe.database_existed is False
    assert probe.needs_migration is False
    assert ensure_schema(tmp_path / "mcq.db").backup_path is None


def test_concurrent_starts_take_exactly_one_backup(tmp_path):
    """Two workers starting at once must not double-backup or corrupt anything."""
    database = _legacy_copy(tmp_path)
    before = _inventory(database)
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def start() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(ensure_schema(database))
        except BaseException as exc:  # noqa: BLE001 - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    backups = _backups(tmp_path)
    assert len(backups) == 1, "the backup decision must be taken once"
    assert _inventory(backups[0]) == before
    assert _schema_version(database) == str(SCHEMA_VERSION)
    # Exactly one of the two runs actually migrated the database.
    assert sum(1 for info in results if info.migrated) == 1
