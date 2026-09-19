"""SQLite connection and schema management.

The database is one shared SQLite file for the whole deployment — courses are a
*namespace inside it*, not one file per course.  This module owns three things
and nothing else:

* opening short-lived, correctly configured connections;
* running the transactional namespace migration exactly once
  (``schema_migrations.ensure_schema``);
* the per-``(learner_id, course_id, question_id)`` attempt-retention sweep,
  which is a *separate* step from the migration on purpose.

Course-scoped repositories bind a ``course_id`` at construction and never see
this module's cross-course seams.
"""

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from app.models import Course, LEGACY_COURSE_ID

from . import schema_migrations

#: Retained answer window per learner and question, shared across every mode.
MAX_ATTEMPTS_PER_QUESTION = 10


class Database:
    """Create short-lived SQLite connections for repository operations."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._transaction_connection = ContextVar("mcq_connection", default=None)
        self.schema_info: schema_migrations.SchemaInfo | None = None

    # -------------------------------------------------------------- initialization

    def initialize(
        self,
        *,
        courses: Iterable[Course] = (),
        legacy_course_id: str = LEGACY_COURSE_ID,
        failpoint=None,
    ) -> schema_migrations.SchemaInfo:
        """Create or migrate the database, then register accepted courses.

        Ordering matters: the namespace migration runs first and touches no
        learner semantics, then the retention sweep runs as its own step, then
        the accepted course metadata is recorded.

        The legacy namespace row always exists because ``legacy_course_id`` is
        part of the schema this build writes: learner tables declare a
        restrictive foreign key onto ``courses``, and rows written under the
        legacy namespace must always have an owner.
        """
        parent_existed = self.database_path.parent.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(self.database_path.parent, 0o700)
        self.schema_info = schema_migrations.ensure_schema(
            self.database_path,
            legacy_course_id=legacy_course_id,
            failpoint=failpoint,
        )
        self.enforce_attempt_retention()
        self.register_courses(courses)
        return self.schema_info

    def register_courses(self, courses: Iterable[Course]) -> None:
        """Record accepted course metadata without re-owning any history.

        The *persisted* ``legacy_course_id`` is used, not the module default: an
        operator may have renamed that namespace (``scripts/rename_course.py``),
        and re-inserting the default id would recreate a phantom, history-less
        course row on every startup.  An existing row keeps its metadata, so the
        recorded title is never overwritten from here.
        """
        from .course_repository import CourseRepository

        repository = CourseRepository(self)
        legacy_course_id = repository.legacy_course_id()
        existing = repository.get(legacy_course_id)
        if existing is None:
            repository.accept(
                Course(
                    course_id=legacy_course_id,
                    title="Legacy course namespace",
                    title_zh="旧版课程命名空间",
                    enabled=True,
                    order=-1_000_001,
                )
            )
        for course in courses:
            repository.accept(course)

    def enforce_attempt_retention(self) -> int:
        """Trim each learner's per-course, per-question answer window.

        The window is ``(learner_id, course_id, question_id)`` and stays shared
        across ``normal``/``review``/``mock_exam``, exactly as before the
        refactor; only the partition gained the course namespace.
        """
        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM attempts
                WHERE id IN (
                    SELECT id
                    FROM (
                        SELECT
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY learner_id, course_id, question_id
                                ORDER BY answered_at DESC, id DESC
                            ) AS retention_rank
                        FROM attempts
                    ) AS ranked_attempts
                    WHERE retention_rank > ?
                )
                """,
                (MAX_ATTEMPTS_PER_QUESTION,),
            )
        return int(cursor.rowcount or 0)


    # ------------------------------------------------------------------ connections

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and commit or roll back automatically."""
        active = self._transaction_connection.get()
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize learner writes across workers inside one ``BEGIN IMMEDIATE``.

        Repositories called inside this block transparently share the single
        connection, so a services call that touches several tables is one
        atomic unit and SQLite's writer lock coordinates sibling Gunicorn
        workers.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = self._transaction_connection.set(connection)
            try:
                yield connection
            finally:
                self._transaction_connection.reset(token)


class CrossCourseQueries:
    """Explicit cross-course access for administration, migration, and tooling.

    Course-bound repositories deliberately cannot escape their namespace.  The
    few operations that genuinely have to look across courses (resolving which
    course owns a bare ``exam_id``, counting rows per course for a migration
    report) live here so an accidental cross-course read is always a visible,
    reviewed choice rather than an implicit default of a repository method.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def find_course_for_exam(self, exam_id: str) -> str | None:
        """Return the course that owns ``exam_id``, or ``None`` when unknown."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT course_id FROM exam_sessions WHERE id = ?", (exam_id,)
            ).fetchone()
        return None if row is None else str(row["course_id"])

    def find_course_for_exam_of_learner(
        self, exam_id: str, learner_id: str
    ) -> str | None:
        """Return the owning course only when the exam belongs to ``learner_id``.

        This backs the legacy ``/exam/<id>/report`` compatibility redirect: the
        redirect target must be derived from the exam's real namespace, never
        from whichever course the browser last looked at.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT course_id FROM exam_sessions WHERE id = ? AND learner_id = ?",
                (exam_id, learner_id),
            ).fetchone()
        return None if row is None else str(row["course_id"])

    def row_counts(self) -> dict[str, int]:
        """Return the total row count per table (diagnostics and tests)."""
        counts: dict[str, int] = {}
        with self.database.connect() as connection:
            tables = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in tables:
                counts[table] = int(
                    connection.execute(
                        f'SELECT COUNT(*) AS total FROM "{table}"'
                    ).fetchone()["total"]
                )
        return counts

    def question_ids_for_course(self, table: str, course_id: str) -> set[str]:
        """Return the question IDs one learner table holds for one course."""
        if table not in {"attempts", "wrong_questions"}:
            raise ValueError(f'Unsupported table for this lookup: "{table}"')
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT question_id FROM {table} WHERE course_id = ?",
                (course_id,),
            ).fetchall()
        return {row["question_id"] for row in rows}
