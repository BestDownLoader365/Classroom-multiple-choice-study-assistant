"""Persistence for the permanent per-question identity registry.

The registry is the long-lived memory of *one course's* question bank: every
question ID that course has ever loaded is kept here with its grading identity.
Rows are never deleted — a question that leaves the bank is only marked
``retired`` — so a retired ID can never be silently recycled for a
human-different question later.

The primary key is ``(course_id, question_id)``.  Course A and course B may both
own a ``q001`` with completely different content, and neither course's registry
bootstrap, diff or retirement can observe the other's IDs.
"""

import json
import sqlite3

from app.models import QuestionRegistryEntry, QuestionRegistryStatus

from .course_scope import require_course_id
from .database import Database


class QuestionRegistryRepository:
    """Read and write ``question_registry`` rows of one course."""

    def __init__(self, database: Database, course_id: str) -> None:
        self.database = database
        self.course_id = require_course_id(course_id)

    def get_all(self) -> dict[str, QuestionRegistryEntry]:
        """Return this course's registry rows keyed by local question ID."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM question_registry WHERE course_id = ? "
                "ORDER BY question_id",
                (self.course_id,),
            ).fetchall()
        return {row["question_id"]: self._to_model(row) for row in rows}

    def count(self) -> int:
        """Return the number of tracked question IDs of this course."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM question_registry WHERE course_id = ?",
                (self.course_id,),
            ).fetchone()
        return int(row["total"])

    def upsert(self, entry: QuestionRegistryEntry) -> None:
        """Insert or replace one registry row of this course."""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO question_registry (
                    course_id, question_id, status, question_type, option_ids,
                    correct_answers, content_fingerprint,
                    placement_fingerprint, first_seen_at, last_seen_at,
                    retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_id, question_id) DO UPDATE SET
                    status = excluded.status,
                    question_type = excluded.question_type,
                    option_ids = excluded.option_ids,
                    correct_answers = excluded.correct_answers,
                    content_fingerprint = excluded.content_fingerprint,
                    placement_fingerprint = excluded.placement_fingerprint,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    retired_at = excluded.retired_at
                """,
                (
                    self.course_id,
                    entry.question_id,
                    entry.status.value,
                    entry.question_type,
                    json.dumps(list(entry.option_ids), ensure_ascii=False),
                    json.dumps(list(entry.correct_answers), ensure_ascii=False),
                    entry.content_fingerprint,
                    entry.placement_fingerprint or None,
                    entry.first_seen_at,
                    entry.last_seen_at,
                    entry.retired_at,
                ),
            )

    def upsert_many(self, entries: list[QuestionRegistryEntry]) -> None:
        """Insert or replace several registry rows of this course."""
        for entry in entries:
            self.upsert(entry)

    def retire(self, question_id: str, *, retired_at: str) -> None:
        """Mark one question as removed, keeping its course-local tombstone."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE question_registry
                SET status = ?, retired_at = ?, last_seen_at = ?
                WHERE course_id = ? AND question_id = ?
                """,
                (
                    QuestionRegistryStatus.RETIRED.value,
                    retired_at,
                    retired_at,
                    self.course_id,
                    question_id,
                ),
            )


    @staticmethod
    def tombstone_for(question_id: str, *, retired_at: str) -> QuestionRegistryEntry:
        """Build a retired row for an orphaned ID found only in history.

        The grading columns stay empty: no bank version of this ID was ever
        observed, so a later reappearance is adopted instead of rejected.

        This is a one-time migration-era tolerance for pre-registry databases
        (the ID only ever existed in learner history, e.g. stored progress or
        exam slots).  It is *not* a general licence to reuse a retired ID: a
        tombstone that does record a grading identity rejects a different
        question.  The trade-off is documented in ``docs/ARCHITECTURE.md``:
        such a legacy tombstone cannot prove the reappearing question is the
        same one, so the reappearance inherits the old history.
        """
        return QuestionRegistryEntry(
            question_id=question_id,
            status=QuestionRegistryStatus.RETIRED,
            question_type="",
            option_ids=(),
            correct_answers=(),
            content_fingerprint="",
            first_seen_at=retired_at,
            last_seen_at=retired_at,
            retired_at=retired_at,
        )

    @staticmethod
    def _to_model(row: sqlite3.Row) -> QuestionRegistryEntry:
        return QuestionRegistryEntry(
            question_id=row["question_id"],
            status=QuestionRegistryStatus(row["status"]),
            question_type=row["question_type"],
            option_ids=tuple(json.loads(row["option_ids"])),
            correct_answers=tuple(json.loads(row["correct_answers"])),
            content_fingerprint=row["content_fingerprint"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            retired_at=row["retired_at"],
            placement_fingerprint=row["placement_fingerprint"] or "",
        )
