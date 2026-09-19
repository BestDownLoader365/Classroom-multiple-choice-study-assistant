"""Permanent course identity and accepted metadata in SQLite.

The ``courses`` table is the durable record of *which courses exist* and of the
metadata an operator has accepted.  It is deliberately tiny: the content itself
lives in immutable files, and everything learner-facing is scoped by
``course_id`` in the other tables.

Two different IDs live here and must never be confused:

``legacy_course_id`` (``schema_meta``)
    Decided once by the namespace migration and persisted.  It records which
    course owns the rows written before the refactor; changing it later would
    silently re-own history, so the API only exposes reading it.

``default_course_id`` (``schema_meta``)
    A pure navigation preference for browsers that arrive without an explicit
    course URL.  Changing it never re-owns any historical data.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import LEGACY_COURSE_ID, Course

from .database import Database
from .schema_migrations import (
    DEFAULT_COURSE_KEY,
    LEGACY_COURSE_KEY,
    SCHEMA_VERSION_KEY,
    read_meta,
)


class CourseStateError(RuntimeError):
    """Raised when a course-state operation would be unsafe or meaningless."""


@dataclass(frozen=True)
class CourseRecord:
    """One accepted course identity as persisted in SQLite."""

    course: Course
    created_at: str
    updated_at: str


class CourseRepository:
    """Read and maintain the permanent course identity table."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ------------------------------------------------------------------ identity

    def accept(self, course: Course) -> CourseRecord:
        """Persist the metadata of one course as accepted by this publication.

        Only the metadata columns are refreshed; ``created_at`` is preserved so
        the row keeps recording when the course first appeared.
        """
        now = _utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO courses (
                    course_id, title, title_zh, enabled, sort_order,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_id) DO UPDATE SET
                    title = excluded.title,
                    title_zh = excluded.title_zh,
                    enabled = excluded.enabled,
                    sort_order = excluded.sort_order,
                    updated_at = excluded.updated_at
                """,
                (
                    course.course_id,
                    course.title,
                    course.title_zh,
                    int(course.enabled),
                    course.order,
                    now,
                    now,
                ),
            )
        record = self.get(course.course_id)
        if record is None:  # pragma: no cover - defensive
            raise CourseStateError(
                f'Course "{course.course_id}" vanished right after being accepted.'
            )
        return record

    def get(self, course_id: str) -> CourseRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM courses WHERE course_id = ?", (course_id,)
            ).fetchone()
        return _to_record(row) if row is not None else None

    def list_accepted(self) -> list[CourseRecord]:
        """Return every course identity this database knows, in display order."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM courses ORDER BY sort_order, course_id"
            ).fetchall()
        return [_to_record(row) for row in rows]

    def known_course_ids(self) -> set[str]:
        return {record.course.course_id for record in self.list_accepted()}

    def is_enabled(self, course_id: str) -> bool:
        """Return whether the course is currently servable, re-read from disk.

        Called from inside learner transactions so a course that was disabled
        after a page was rendered can never accept new learning writes.
        """
        record = self.get(course_id)
        return record is not None and record.course.enabled

    def set_enabled(self, course_id: str, enabled: bool) -> CourseRecord:
        """Enable or disable one course without touching its learner data."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE courses SET enabled = ?, updated_at = ? WHERE course_id = ?",
                (int(enabled), _utc_now(), course_id),
            )
        if cursor.rowcount == 0:
            raise CourseStateError(f'Course "{course_id}" is not registered.')
        record = self.get(course_id)
        if record is None:  # pragma: no cover - defensive
            raise CourseStateError(f'Course "{course_id}" disappeared.')
        return record

    # ------------------------------------------------------------------ meta keys

    def legacy_course_id(self) -> str:
        """Return the persisted legacy namespace (read-only by design)."""
        with self.database.connect() as connection:
            return read_meta(connection, LEGACY_COURSE_KEY) or LEGACY_COURSE_ID

    def schema_version(self) -> int:
        with self.database.connect() as connection:
            raw = read_meta(connection, SCHEMA_VERSION_KEY)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def default_course_id(self) -> str | None:
        """Return the persisted navigation preference, if any."""
        with self.database.connect() as connection:
            return read_meta(connection, DEFAULT_COURSE_KEY)

    def set_default_course_id(self, course_id: str | None) -> None:
        """Persist the navigation preference (never an ownership change)."""
        with self.database.connect() as connection:
            if course_id is None:
                connection.execute(
                    "DELETE FROM schema_meta WHERE key = ?", (DEFAULT_COURSE_KEY,)
                )
                return
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (DEFAULT_COURSE_KEY, course_id),
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_record(row: sqlite3.Row) -> CourseRecord:
    return CourseRecord(
        course=Course(
            course_id=row["course_id"],
            title=row["title"],
            title_zh=row["title_zh"] or "",
            enabled=bool(row["enabled"]),
            order=int(row["sort_order"]),
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

