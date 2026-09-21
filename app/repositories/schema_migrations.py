"""Transactional, lossless, idempotent multi-course namespace migration.

This module owns the *only* place where an existing database's table layout is
rebuilt.  The migration is deliberately narrow:

* it adds the ``course_id`` namespace column to every learner table and moves
  every existing row into the fixed
  :data:`~app.models.course.LEGACY_COURSE_ID` namespace;
* it replaces the ``question_bank_state`` singleton with one row per course;
* it widens the ``question_registry`` primary key to ``(course_id,
  question_id)``;
* it records the schema version and the persisted legacy-course assignment.

It explicitly does **not** run retention cleanup, weak-knowledge backfill,
question-bank diffing, registry reconciliation, content cleanup, fingerprint
recomputation or score rewriting.  Namespace migration and question-bank
reconciliation are two independent steps, in that order.

Transaction shape::

    dedicated connection
      PRAGMA foreign_keys = OFF (before the transaction)
      BEGIN IMMEDIATE
        re-read the schema version *after* taking the lock
        create the new tables
        copy every old row
        field-level validation
        replace the old tables
        write schema version + persisted legacy course id
        PRAGMA foreign_key_check (full parent/child check, before COMMIT)
      COMMIT
      create the indexes (idempotent, retried by the next startup)

The full ``PRAGMA foreign_key_check`` deliberately runs *inside* the transaction.
It is an explicit check, so the connection's ``PRAGMA foreign_keys = OFF`` does
not affect it, and running it before ``COMMIT`` is what makes the failure report
true: the caller rolls back, so "No data was changed" is a fact and not a claim.
A fresh database takes no transaction at all — it has nothing to copy and nothing
to check, so :func:`_create_current_schema` writes the same empty layout through
the same DDL and every statement autocommits.

Any failure inside the transaction rolls the whole thing back; re-running always
reaches the same state.  A pre-existing parent/child violation in the old data
aborts the migration with a report instead of silently dropping rows.
"""

import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from app.models import LEGACY_COURSE_ID

from . import database_backup
from .database_backup import BackupError, timestamped_backup

#: Schema version of the multi-course layout.
SCHEMA_VERSION = 2

#: Schema version of the pre-multi-course layout (single global namespace).
LEGACY_SCHEMA_VERSION = 1

SCHEMA_META_TABLE = "schema_meta"
SCHEMA_VERSION_KEY = "schema_version"
LEGACY_COURSE_KEY = "legacy_course_id"
DEFAULT_COURSE_KEY = "default_course_id"

MIGRATION_TABLE_SUFFIX = "__course_migration"

#: Fault-injection stage names, used by the migration rehearsal tests.
STAGE_AFTER_CREATE = "after_create"
STAGE_AFTER_COPY = "after_copy"
STAGE_BEFORE_REPLACE = "before_replace"
STAGE_BEFORE_VERSION = "before_version"
STAGE_BEFORE_COMMIT = "before_commit"
#: Fired right after a startup backup was written and verified, before the
#: migration transaction starts.
STAGE_AFTER_BACKUP = "after_backup"


class SchemaMigrationError(RuntimeError):
    """Raised when the database cannot be migrated safely.

    The migration must never delete or reinterpret data to make itself pass, so
    an unresolvable precondition is reported and the transaction is rolled back.
    """


class StartupMigrationRefused(SchemaMigrationError):
    """Raised when the startup path is not allowed to migrate an existing database.

    A refusal is the *point* of the policy: the process may not modify a database
    whose schema it did not create, unless the deployment said it may (and then
    only after a verified backup).
    """


class StartupMigrationPolicy(str, Enum):
    """What a caller allows ``ensure_schema()`` to do to an existing database.

    ``BACKUP_AND_MIGRATE`` is the default because it is strictly safer than the
    historical behaviour: the database is snapshotted and the snapshot verified
    before anything is rebuilt, and a failed backup aborts the whole operation.
    ``REFUSE`` exists for operators who want the explicit CLI step to be the only
    way an old schema ever changes, and it is what an *undeclared* environment
    resolves to.

    ``MIGRATE_AFTER_EXTERNAL_BACKUP`` is for callers that already produced and
    reported a backup in the same process — the migration CLI, which prints the
    backup path itself.  No application startup path may use it.
    """

    REFUSE = "refuse"
    BACKUP_AND_MIGRATE = "backup-and-migrate"
    MIGRATE_AFTER_EXTERNAL_BACKUP = "migrate-after-external-backup"


@dataclass(frozen=True)
class SchemaProbe:
    """What a read-only inspection of one database file found.

    ``needs_migration`` is deliberately broader than "the stored version is old":
    a database whose version is current but that is missing one of
    :data:`TABLE_BODIES` also needs DDL, and DDL is exactly the operation the
    backup policy exists for.
    """

    database_path: Path
    database_existed: bool
    schema_version: int | None
    missing_tables: tuple[str, ...]

    @property
    def fresh(self) -> bool:
        """Whether this is a database this build will create from scratch."""
        return not self.database_existed

    @property
    def needs_migration(self) -> bool:
        """Whether the write path would have to change the table layout."""
        if not self.database_existed:
            return False
        if self.schema_version is None or self.schema_version < SCHEMA_VERSION:
            return True
        return bool(self.missing_tables)


@dataclass(frozen=True)
class SchemaInfo:
    """The database layout this process found (and possibly produced)."""

    schema_version: int
    legacy_course_id: str
    database_existed: bool
    migrated: bool
    #: The verified snapshot taken before the migration, when one was needed.
    backup_path: Path | None = None


def probe_schema(database_path: Path) -> SchemaProbe:
    """Inspect a database file read-only and report whether it needs DDL.

    Split out of :func:`ensure_schema` so a caller can decide *before* opening a
    writable connection — and so the decision that triggers a backup can never
    disagree with the decision that performs the migration.  Read-only by
    construction (``mode=ro``) and cheap: it only reads ``sqlite_master`` and two
    ``schema_meta`` rows.
    """
    database_path = Path(database_path)
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return SchemaProbe(database_path, False, None, ())
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = _table_names(connection)
        if not tables:
            # An existing file with no tables is created from scratch.
            return SchemaProbe(database_path, False, None, ())
        version = _read_schema_version(connection)
        missing = tuple(name for name in TABLE_BODIES if name not in tables)
        return SchemaProbe(database_path, True, version, missing)
    finally:
        connection.close()


@contextmanager
def migration_lock(database_path: Path):
    """Serialise the backup-and-migrate decision across sibling processes.

    Every worker of a multi-process deployment runs ``ensure_schema`` on startup,
    so without a lock two of them could both decide to back up and both migrate.
    ``BEGIN IMMEDIATE`` inside the migration already prevents *database* damage;
    this lock additionally makes the *backup* decision once, so a rolling restart
    of N workers produces one backup instead of N.

    The lock file lives next to the database and is deliberately left in place:
    unlinking an flock'ed file would let a third process lock a fresh inode while
    the first still holds the old one.  On a platform without ``fcntl`` the lock
    degrades to a no-op — the worst outcome is then an extra backup file, never a
    half-migrated database, because the migration itself stays transactional.
    """
    lock_path = Path(f"{database_path}.migrate.lock")
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield lock_path
        return
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _refusal_message(probe: SchemaProbe, database_path: Path) -> str:
    """Explain a refused startup migration with the commands that fix it."""
    current = probe.schema_version if probe.schema_version is not None else "unknown"
    details = f"（当前 schema_version={current}，目标 {SCHEMA_VERSION}）"
    if probe.missing_tables:
        details += f"，缺少表：{', '.join(probe.missing_tables)}"
    return (
        f"检测到需要迁移的数据库{details}：{database_path}\n"
        "当前运行环境不允许在启动时自动迁移（未声明 MCQ_ENV，或已显式设置为 refuse），"
        "以免在没有备份的情况下改动部署数据。\n"
        "请先停服务并显式迁移：\n"
        f"    python scripts/migrate_courses.py --db {database_path}\n"
        "（它会先生成带时间戳的备份副本并校验，然后才执行迁移；"
        "细节见 docs/MULTI_COURSE_MIGRATION.md 第 1 节）\n"
        "如果这是开发机并且希望启动时自动备份并迁移，"
        "设置 MCQ_ENV=development（或 MCQ_AUTO_MIGRATE=backup-and-migrate）后重试。"
    )

def ensure_schema(
    database_path: Path,
    *,
    legacy_course_id: str = LEGACY_COURSE_ID,
    failpoint: Callable[[str], None] | None = None,
    policy: StartupMigrationPolicy = StartupMigrationPolicy.BACKUP_AND_MIGRATE,
    backup: Callable[[Path], Path] = timestamped_backup,
) -> SchemaInfo:
    """Create a fresh database or migrate an existing one, then return its layout.

    The order of operations is the safety contract:

    1. a read-only :func:`probe_schema` decides whether the table layout has to
       change at all — a fresh database or an up-to-date one sees no backup and
       no lock, so a normal startup writes nothing extra;
    2. when it does, the policy decides what is allowed: refuse outright
       (:class:`StartupMigrationRefused`), back up first, or migrate because the
       caller already produced a backup;
    3. the backup is taken and verified **before** the migration transaction
       starts, and a failure there propagates — the database is never modified
       without one;
    4. the migration itself stays a single ``BEGIN IMMEDIATE`` transaction, so a
       failure anywhere inside it rolls back to the un-migrated (and now backed
       up) state.

    ``failpoint`` is only used by the migration rehearsal tests to abort at a
    named stage; production never passes it.  ``backup`` is injectable for the
    same reason.
    """
    database_path = Path(database_path)
    probe = probe_schema(database_path)
    database_existed = probe.database_existed

    connection = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        if database_path.exists():
            os.chmod(database_path, 0o600)
        # Foreign keys are a per-connection setting and cannot be changed inside
        # a transaction, so it is switched off here, before BEGIN IMMEDIATE.
        connection.execute("PRAGMA foreign_keys = OFF")
        if not _table_names(connection):
            # A fresh database deliberately returns here: there is nothing to
            # migrate and nothing to check, because the tables were just created
            # empty by the very DDL the migration path uses, so the full
            # parent/child check would be vacuous.  Every statement autocommits
            # (``isolation_level=None``); a crash halfway through would leave some
            # tables behind, and the next startup heals that through
            # ``_ensure_support_tables``.
            _create_current_schema(connection)
            _write_meta(connection, SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
            _write_meta(connection, LEGACY_COURSE_KEY, legacy_course_id)
            _fire(failpoint, STAGE_BEFORE_COMMIT)
            return SchemaInfo(SCHEMA_VERSION, legacy_course_id, False, False)

        backup_path: Path | None = None
        if probe.needs_migration:
            if policy is StartupMigrationPolicy.REFUSE:
                raise StartupMigrationRefused(_refusal_message(probe, database_path))
            with migration_lock(database_path):
                # Re-probe under the lock: a sibling worker may have migrated
                # while we waited, and this process must not take a second backup
                # of an already-current database.
                probe = probe_schema(database_path)
                if probe.needs_migration and (
                    policy is StartupMigrationPolicy.BACKUP_AND_MIGRATE
                ):
                    backup_path = backup(database_path)
                    _fire(failpoint, STAGE_AFTER_BACKUP)
                return _migrate_existing(
                    connection,
                    legacy_course_id,
                    failpoint,
                    database_existed,
                    backup_path,
                )
        return _migrate_existing(
            connection, legacy_course_id, failpoint, database_existed, None
        )
    finally:
        connection.close()


def _migrate_existing(
    connection: sqlite3.Connection,
    legacy_course_id: str,
    failpoint: Callable[[str], None] | None,
    database_existed: bool,
    backup_path: Path | None,
) -> SchemaInfo:
    """Run the idempotent migration transaction on an already-existing database.

    Reaching here after a sibling migrated first means the version check inside
    the transaction simply finds nothing to do; the ``_ensure_support_tables``
    branch still brings a same-version database up to the full current layout.

    The full parent/child check and the ``COMMIT`` live in the same transaction,
    so a violation that check reports leaves the database exactly as it was.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the lock: another worker may have migrated already.
        version = _read_schema_version(connection)
        migrated = False
        if version is None or version < SCHEMA_VERSION:
            _migrate_namespace(connection, legacy_course_id, failpoint)
            _write_meta(connection, SCHEMA_VERSION_KEY, str(SCHEMA_VERSION))
            _write_meta(connection, LEGACY_COURSE_KEY, legacy_course_id)
            migrated = True
        else:
            _ensure_support_tables(connection, legacy_course_id)
        # The full parent/child check belongs inside the transaction: it is an
        # explicit check (``PRAGMA foreign_keys = OFF`` does not affect it) and
        # running it before COMMIT is what makes its "No data was changed" report
        # true, because the caller then rolls the layout back.
        _assert_foreign_keys(connection)
        _fire(failpoint, STAGE_BEFORE_COMMIT)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    # Index creation stays after the commit: ``CREATE INDEX IF NOT EXISTS`` is
    # idempotent and validates nothing, so keeping it out of the write
    # transaction shortens the exclusive lock the copy already holds.  If it ever
    # fails, the layout is still consistent and the next startup retries it,
    # because every path through this function ends up calling it.
    _create_indexes(connection)
    stored_legacy = read_meta(connection, LEGACY_COURSE_KEY) or legacy_course_id
    return SchemaInfo(
        SCHEMA_VERSION, stored_legacy, database_existed, migrated, backup_path
    )


# --------------------------------------------------------------------------- DDL

#: Table bodies of the current (multi-course) schema.  They are shared by the
#: fresh-database path and the migration path so both always produce exactly
#: the same layout.
TABLE_BODIES: dict[str, str] = {
    SCHEMA_META_TABLE: """
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    """,
    "courses": """
        course_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        title_zh TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    """,
    "users": """
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    """,
    "quiz_progress": """
        learner_id TEXT NOT NULL,
        course_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('normal', 'review')),
        bank_version TEXT NOT NULL,
        state TEXT,
        PRIMARY KEY (learner_id, course_id, mode),
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
    "attempts": """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        learner_id TEXT NOT NULL,
        course_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('normal', 'review', 'mock_exam')),
        selected_answers TEXT NOT NULL,
        is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
        answered_at TEXT NOT NULL,
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
    "wrong_questions": """
        learner_id TEXT NOT NULL,
        course_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        wrong_count INTEGER NOT NULL DEFAULT 1,
        review_streak INTEGER NOT NULL DEFAULT 0,
        mastered INTEGER NOT NULL DEFAULT 0 CHECK (mastered IN (0, 1)),
        srs_level INTEGER NOT NULL DEFAULT 0,
        next_review_at TEXT,
        last_wrong_at TEXT NOT NULL,
        last_reviewed_at TEXT,
        PRIMARY KEY (learner_id, course_id, question_id),
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
    "weak_knowledge_points": """
        learner_id TEXT NOT NULL,
        course_id TEXT NOT NULL,
        chapter_id TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        verified_question_ids TEXT NOT NULL DEFAULT '[]',
        last_wrong_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (learner_id, course_id, chapter_id),
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
    "auth_rate_limits": """
        scope TEXT NOT NULL,
        identifier_hash TEXT NOT NULL,
        window_started_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        attempt_count INTEGER NOT NULL,
        PRIMARY KEY (scope, identifier_hash)
    """,
    "exam_sessions": """
        id TEXT PRIMARY KEY,
        course_id TEXT NOT NULL,
        learner_id TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('in_progress', 'submitted', 'expired')),
        question_count INTEGER NOT NULL,
        time_limit_seconds INTEGER,
        option_seed TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT NOT NULL,
        deadline_at TEXT,
        submitted_at TEXT,
        current_position INTEGER NOT NULL DEFAULT 0,
        correct_count INTEGER,
        duration_seconds INTEGER,
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
    "exam_questions": """
        exam_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        question_id TEXT NOT NULL,
        selected_answers TEXT,
        is_correct INTEGER CHECK (is_correct IN (0, 1)),
        answered_at TEXT,
        grading_fingerprint TEXT,
        PRIMARY KEY (exam_id, position),
        UNIQUE (exam_id, question_id),
        FOREIGN KEY (exam_id) REFERENCES exam_sessions(id) ON DELETE RESTRICT
    """,
    "question_bank_state": """
        course_id TEXT PRIMARY KEY,
        bank_version TEXT NOT NULL,
        generation INTEGER NOT NULL,
        catalogue_fingerprint TEXT,
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
    "question_registry": """
        course_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
        question_type TEXT NOT NULL,
        option_ids TEXT NOT NULL,
        correct_answers TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL,
        placement_fingerprint TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        retired_at TEXT,
        PRIMARY KEY (course_id, question_id),
        FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT
    """,
}

INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_attempts_learner_course_question "
    "ON attempts(learner_id, course_id, question_id, answered_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_course_answered "
    "ON attempts(course_id, answered_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_wrong_questions_learner_course "
    "ON wrong_questions(learner_id, course_id, mastered, next_review_at)",
    "CREATE INDEX IF NOT EXISTS idx_weak_points_learner_course "
    "ON weak_knowledge_points(learner_id, course_id, active)",
    "CREATE INDEX IF NOT EXISTS idx_exam_sessions_learner_course "
    "ON exam_sessions(learner_id, course_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_exam_sessions_course_status "
    "ON exam_sessions(course_id, status, deadline_at)",
)


#: Legacy tables that existed before the namespace migration, with the
#: ``(target_column, legacy_expression)`` projection used when copying rows.
#: A column missing from the old table falls back to its documented default.
LEGACY_COPY_PROJECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "quiz_progress": (
        ("learner_id", "learner_id"),
        ("mode", "mode"),
        ("bank_version", "bank_version"),
        ("state", "state"),
    ),
    "attempts": (
        ("id", "id"),
        ("learner_id", "learner_id"),
        ("question_id", "question_id"),
        ("mode", "mode"),
        ("selected_answers", "selected_answers"),
        ("is_correct", "is_correct"),
        ("answered_at", "answered_at"),
    ),
    "wrong_questions": (
        ("learner_id", "learner_id"),
        ("question_id", "question_id"),
        ("wrong_count", "wrong_count"),
        ("review_streak", "review_streak"),
        ("mastered", "mastered"),
        ("srs_level", "srs_level"),
        ("next_review_at", "next_review_at"),
        ("last_wrong_at", "last_wrong_at"),
        ("last_reviewed_at", "last_reviewed_at"),
    ),
    "weak_knowledge_points": (
        ("learner_id", "learner_id"),
        ("chapter_id", "chapter_id"),
        ("active", "active"),
        ("verified_question_ids", "verified_question_ids"),
        ("last_wrong_at", "last_wrong_at"),
        ("updated_at", "updated_at"),
    ),
    "exam_sessions": (
        ("id", "id"),
        ("learner_id", "learner_id"),
        ("status", "status"),
        ("question_count", "question_count"),
        ("time_limit_seconds", "time_limit_seconds"),
        ("option_seed", "option_seed"),
        ("created_at", "created_at"),
        ("started_at", "started_at"),
        ("deadline_at", "deadline_at"),
        ("submitted_at", "submitted_at"),
        ("current_position", "current_position"),
        ("correct_count", "correct_count"),
        ("duration_seconds", "duration_seconds"),
    ),
    "exam_questions": (
        ("exam_id", "exam_id"),
        ("position", "position"),
        ("question_id", "question_id"),
        ("selected_answers", "selected_answers"),
        ("is_correct", "is_correct"),
        ("answered_at", "answered_at"),
        ("grading_fingerprint", "grading_fingerprint"),
    ),
    "question_registry": (
        ("question_id", "question_id"),
        ("status", "status"),
        ("question_type", "question_type"),
        ("option_ids", "option_ids"),
        ("correct_answers", "correct_answers"),
        ("content_fingerprint", "content_fingerprint"),
        ("placement_fingerprint", "placement_fingerprint"),
        ("first_seen_at", "first_seen_at"),
        ("last_seen_at", "last_seen_at"),
        ("retired_at", "retired_at"),
    ),
    # The pre-multi-course singleton row becomes the legacy course's row.
    "question_bank_state": (
        ("bank_version", "bank_version"),
        ("generation", "generation"),
        ("catalogue_fingerprint", "catalogue_fingerprint"),
    ),
}

#: Legacy column replacements used when an older table lacks the column.
LEGACY_COLUMN_DEFAULTS: dict[str, str] = {
    "srs_level": "0",
    "next_review_at": "NULL",
    "grading_fingerprint": "NULL",
    "placement_fingerprint": "NULL",
    "catalogue_fingerprint": "NULL",
}

#: Tables carrying the ``course_id`` namespace column after the migration.
COURSE_SCOPED_TABLES: tuple[str, ...] = (
    "quiz_progress",
    "attempts",
    "wrong_questions",
    "weak_knowledge_points",
    "exam_sessions",
    "question_bank_state",
    "question_registry",
)



# ------------------------------------------------------------------- primitives


def _fire(failpoint: Callable[[str], None] | None, stage: str) -> None:
    if failpoint is not None:
        failpoint(stage)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(
        connection.execute(f'SELECT COUNT(*) AS total FROM "{table}"').fetchone()[
            "total"
        ]
    )


def _create_table(
    connection: sqlite3.Connection, name: str, *, table_name: str | None = None
) -> None:
    target = table_name or name
    connection.execute(f'CREATE TABLE "{target}" ({TABLE_BODIES[name]})')


def _create_current_schema(connection: sqlite3.Connection) -> None:
    """Create every table of the multi-course layout on a fresh database."""
    _create_table(connection, SCHEMA_META_TABLE)
    for name in TABLE_BODIES:
        if name == SCHEMA_META_TABLE:
            continue
        _create_table(connection, name)
    _create_indexes(connection)


def _create_indexes(connection: sqlite3.Connection) -> None:
    for statement in INDEX_STATEMENTS:
        connection.execute(statement)


def read_meta(connection: sqlite3.Connection, key: str) -> str | None:
    if SCHEMA_META_TABLE not in _table_names(connection):
        return None
    row = connection.execute(
        f'SELECT value FROM "{SCHEMA_META_TABLE}" WHERE key = ?', (key,)
    ).fetchone()
    return None if row is None else str(row["value"])


def read_legacy_course_id(database_path: Path) -> str | None:
    """Return the persisted legacy namespace without writing anything.

    The persisted value — not the module default — decides which namespace owns
    pre-multi-course data, so application assembly and the CLI must read it
    *before* they build the course loader or run the migration.  Returns ``None``
    for a missing/empty file, a database without ``schema_meta``, or a database
    that never recorded the key (a fresh one); the caller then uses
    :data:`~app.models.course.LEGACY_COURSE_ID`.

    Read-only by construction: the connection is opened with ``mode=ro`` so a
    probe can never create or modify the file it inspects.
    """
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return None
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if not _table_names(connection):
            return None
        return read_meta(connection, LEGACY_COURSE_KEY)
    finally:
        connection.close()


def _write_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        f'INSERT INTO "{SCHEMA_META_TABLE}" (key, value) VALUES (?, ?) '
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    """Return the stored schema version, or ``None`` when it was never stored."""
    if SCHEMA_META_TABLE not in _table_names(connection):
        return None
    raw = read_meta(connection, SCHEMA_VERSION_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise SchemaMigrationError(
            f'Stored "{SCHEMA_VERSION_KEY}" is not an integer: {raw!r}'
        ) from exc


def _assert_foreign_keys(connection: sqlite3.Connection) -> None:
    """Run the full parent/child check on the rebuilt layout, before COMMIT.

    ``PRAGMA foreign_key_check`` is an explicit check, so the connection-level
    ``PRAGMA foreign_keys = OFF`` does not affect it.  The caller invokes it
    inside the migration transaction, and that is what makes the message below
    true: a violation rolls the layout back, and the offending rows are still
    there to be repaired deliberately.
    """
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        summary = ", ".join(
            f"{row[0]}(rowid={row[1]}) -> {row[2]}" for row in violations[:10]
        )
        raise SchemaMigrationError(
            "Foreign key check failed after the namespace migration: "
            f"{summary}. No data was changed; fix the offending rows and retry."
        )


def _ensure_support_tables(
    connection: sqlite3.Connection, legacy_course_id: str
) -> None:
    """Bring a already-migrated database up to the full current layout."""
    tables = _table_names(connection)
    for name in TABLE_BODIES:
        if name not in tables:
            _create_table(connection, name)
            tables.add(name)
    connection.execute(
        f'INSERT INTO "{SCHEMA_META_TABLE}" (key, value) VALUES (?, ?) '
        "ON CONFLICT(key) DO NOTHING",
        (LEGACY_COURSE_KEY, legacy_course_id),
    )
    connection.execute(
        f'INSERT INTO "{SCHEMA_META_TABLE}" (key, value) VALUES (?, ?) '
        "ON CONFLICT(key) DO NOTHING",
        (SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
    )


def _ensure_course_row(
    connection: sqlite3.Connection,
    course_id: str,
    *,
    title: str = "Legacy course namespace",
    title_zh: str = "",
    enabled: bool = True,
    sort_order: int = 0,
) -> None:
    """Insert the permanent course identity row when it is missing."""
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO courses (
            course_id, title, title_zh, enabled, sort_order, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(course_id) DO NOTHING
        """,
        (course_id, title, title_zh, int(enabled), sort_order, now, now),
    )



# -------------------------------------------------------------------- migration


def _validate_legacy_relationships(
    connection: sqlite3.Connection, tables: set[str]
) -> None:
    """Refuse to migrate data that already violates the new parent/child rules.

    The pre-multi-course schema recorded exam slots without a foreign key, so a
    hand-edited or crashed database can contain slots whose exam no longer
    exists.  Those rows cannot satisfy ``exam_questions -> exam_sessions``, and
    the migration must report them instead of deleting them silently.
    """
    if "exam_questions" not in tables or "exam_sessions" not in tables:
        return
    orphans = [
        row["exam_id"]
        for row in connection.execute(
            """
            SELECT DISTINCT exam_id FROM exam_questions
            WHERE exam_id NOT IN (SELECT id FROM exam_sessions)
            ORDER BY exam_id
            """
        )
    ]
    if orphans:
        shown = ", ".join(orphans[:10])
        raise SchemaMigrationError(
            "Cannot migrate: exam_questions rows reference missing exam "
            f"sessions ({shown}"
            + (", ..." if len(orphans) > 10 else "")
            + "). No data was changed; repair or remove those rows deliberately, "
            "then retry."
        )


def _migrate_namespace(
    connection: sqlite3.Connection,
    legacy_course_id: str,
    failpoint: Callable[[str], None] | None,
) -> None:
    """Rebuild every table into the multi-course layout inside the caller's txn."""
    tables = _table_names(connection)
    if "courses" not in tables:
        _create_table(connection, "courses")
    if SCHEMA_META_TABLE not in tables:
        _create_table(connection, SCHEMA_META_TABLE)
    _ensure_course_row(connection, legacy_course_id)
    _validate_legacy_relationships(connection, tables)

    rebuild = [
        name
        for name in COURSE_SCOPED_TABLES
        if name in tables and "course_id" not in _columns(connection, name)
    ]
    # ``exam_questions`` carries no course_id (the namespace always comes from
    # its parent session) but gains the restrictive parent foreign key.
    if "exam_questions" in tables:
        rebuild.append("exam_questions")

    original_counts: dict[str, int] = {}
    for name in rebuild:
        original_counts[name] = _count(connection, name)
        temporary = f"{name}{MIGRATION_TABLE_SUFFIX}"
        connection.execute(f'DROP TABLE IF EXISTS "{temporary}"')
        _create_table(connection, name, table_name=temporary)

    _fire(failpoint, STAGE_AFTER_CREATE)

    for name in rebuild:
        _copy_table(connection, name, legacy_course_id)

    _fire(failpoint, STAGE_AFTER_COPY)

    for name in rebuild:
        _validate_migrated_table(connection, name, original_counts[name])

    _fire(failpoint, STAGE_BEFORE_REPLACE)

    for name in rebuild:
        temporary = f"{name}{MIGRATION_TABLE_SUFFIX}"
        connection.execute(f'DROP TABLE "{name}"')
        connection.execute(f'ALTER TABLE "{temporary}" RENAME TO "{name}"')

    # Tables that never existed in this database still need to exist now.
    present = _table_names(connection)
    for name in TABLE_BODIES:
        if name not in present:
            _create_table(connection, name)
    _fire(failpoint, STAGE_BEFORE_VERSION)



def _copy_table(
    connection: sqlite3.Connection, name: str, legacy_course_id: str
) -> None:
    """Copy one legacy table into its namespace-aware replacement."""
    existing = _columns(connection, name)
    projections = LEGACY_COPY_PROJECTIONS[name]
    scoped = name in COURSE_SCOPED_TABLES
    target_columns: list[str] = ["course_id"] if scoped else []
    select_expressions: list[str] = []
    parameters: list[object] = []
    if scoped:
        select_expressions.append("?")
        parameters.append(legacy_course_id)
    for target, _expression in projections:
        if target in existing:
            select_expressions.append(f'"{target}"')
            target_columns.append(target)
        elif target in LEGACY_COLUMN_DEFAULTS:
            select_expressions.append(LEGACY_COLUMN_DEFAULTS[target])
            target_columns.append(target)
        else:
            raise SchemaMigrationError(
                f'Column "{target}" is missing from the legacy "{name}" table and '
                "has no documented default; refusing to guess."
            )
    temporary = f"{name}{MIGRATION_TABLE_SUFFIX}"
    column_sql = ", ".join(f'"{column}"' for column in target_columns)
    connection.execute(
        f'INSERT INTO "{temporary}" ({column_sql}) '
        f'SELECT {", ".join(select_expressions)} FROM "{name}"',
        parameters,
    )


def _validate_migrated_table(
    connection: sqlite3.Connection, name: str, expected_rows: int
) -> None:
    """Field-level validation of one copied table before it replaces the old one."""
    temporary = f"{name}{MIGRATION_TABLE_SUFFIX}"
    copied = _count(connection, temporary)
    if copied != expected_rows:
        raise SchemaMigrationError(
            f'Namespace migration lost rows in "{name}": expected '
            f"{expected_rows}, copied {copied}. Nothing was written."
        )
    if name in COURSE_SCOPED_TABLES:
        missing = int(
            connection.execute(
                f'SELECT COUNT(*) AS total FROM "{temporary}" '
                "WHERE course_id IS NULL OR course_id = ''"
            ).fetchone()["total"]
        )
        if missing:
            raise SchemaMigrationError(
                f"Namespace migration produced {missing} rows without a course_id "
                f'in "{name}". Nothing was written.'
            )
    if name == "exam_questions":
        orphans = int(
            connection.execute(
                f'SELECT COUNT(*) AS total FROM "{temporary}" q '
                "WHERE q.exam_id NOT IN (SELECT id FROM exam_sessions)"
            ).fetchone()["total"]
        )
        if orphans:
            raise SchemaMigrationError(
                "Namespace migration found exam slots without a surviving exam "
                "session; aborting without writing anything."
            )

