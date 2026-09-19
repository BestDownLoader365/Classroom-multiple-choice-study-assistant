"""Persistence for learner-scoped chapter reinforcement state, per course."""

import json
import sqlite3

from app.models import WeakKnowledgePoint

from .course_scope import require_course_id
from .database import Database


class WeakKnowledgePointRepository:
    """Store active weak chapters and review verification IDs for one course.

    Chapter IDs are unique *inside* a course, so the key is
    ``(learner_id, course_id, chapter_id)``: completing ``chapter_1`` in course
    A can never complete ``chapter_1`` in course B.
    """

    def __init__(self, database: Database, course_id: str) -> None:
        self.database = database
        self.course_id = require_course_id(course_id)

    def activate_and_reset(
        self,
        learner_id: str,
        chapter_id: str,
        timestamp: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO weak_knowledge_points (
                    learner_id, course_id, chapter_id, active,
                    verified_question_ids, last_wrong_at, updated_at
                ) VALUES (?, ?, ?, 1, '[]', ?, ?)
                ON CONFLICT(learner_id, course_id, chapter_id) DO UPDATE SET
                    active = 1,
                    verified_question_ids = '[]',
                    last_wrong_at = excluded.last_wrong_at,
                    updated_at = excluded.updated_at
                """,
                (learner_id, self.course_id, chapter_id, timestamp, timestamp),
            )

    def ensure_active(
        self,
        learner_id: str,
        chapter_id: str,
        timestamp: str,
    ) -> None:
        """Backfill a weak chapter without reopening an existing completed row."""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO weak_knowledge_points (
                    learner_id, course_id, chapter_id, active,
                    verified_question_ids, last_wrong_at, updated_at
                ) VALUES (?, ?, ?, 1, '[]', ?, ?)
                """,
                (learner_id, self.course_id, chapter_id, timestamp, timestamp),
            )

    def save_verification(
        self,
        learner_id: str,
        chapter_id: str,
        verified_question_ids: tuple[str, ...],
        active: bool,
        timestamp: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE weak_knowledge_points
                SET active = ?,
                    verified_question_ids = ?,
                    updated_at = ?
                WHERE learner_id = ? AND course_id = ? AND chapter_id = ?
                """,
                (
                    int(active),
                    json.dumps(verified_question_ids, ensure_ascii=False),
                    timestamp,
                    learner_id,
                    self.course_id,
                    chapter_id,
                ),
            )

    def get_by_id(
        self, learner_id: str, chapter_id: str
    ) -> WeakKnowledgePoint | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM weak_knowledge_points
                WHERE learner_id = ? AND course_id = ? AND chapter_id = ?
                """,
                (learner_id, self.course_id, chapter_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def get_all(self, learner_id: str | None = None) -> list[WeakKnowledgePoint]:
        """Return this course's weak points; ``None`` means every account here."""
        with self.database.connect() as connection:
            if learner_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM weak_knowledge_points
                    WHERE course_id = ?
                    ORDER BY learner_id, updated_at DESC, chapter_id
                    """,
                    (self.course_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM weak_knowledge_points
                    WHERE learner_id = ? AND course_id = ?
                    ORDER BY updated_at DESC, chapter_id
                    """,
                    (learner_id, self.course_id),
                ).fetchall()
        return [self._to_model(row) for row in rows]

    def delete_all_for_learner(self, learner_id: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM weak_knowledge_points "
                "WHERE learner_id = ? AND course_id = ?",
                (learner_id, self.course_id),
            )
        return cursor.rowcount

    @staticmethod
    def _to_model(row: sqlite3.Row) -> WeakKnowledgePoint:
        return WeakKnowledgePoint(
            learner_id=row["learner_id"],
            chapter_id=row["chapter_id"],
            active=bool(row["active"]),
            verified_question_ids=_validated_ids(row["verified_question_ids"]),
            last_wrong_at=row["last_wrong_at"],
            updated_at=row["updated_at"],
        )


def _validated_ids(raw_value: str) -> tuple[str, ...]:
    """Treat malformed legacy/manual JSON as empty reinforcement progress."""
    try:
        values = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str)))
