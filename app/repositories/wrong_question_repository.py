"""Persistence operations for wrong-question learning state."""

import sqlite3

from app.models import WrongQuestion

from .database import Database


class WrongQuestionRepository:
    """Update question-level wrong and correction state."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_wrong(
        self,
        learner_id: str,
        question_id: str,
        timestamp: str,
        reviewed: bool,
    ) -> None:
        """Create or reopen a wrong question and clear correction state."""
        reviewed_at = timestamp if reviewed else None
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO wrong_questions (
                    learner_id, question_id, wrong_count, review_streak, mastered,
                    last_wrong_at, last_reviewed_at
                ) VALUES (?, ?, 1, 0, 0, ?, ?)
                ON CONFLICT(learner_id, question_id) DO UPDATE SET
                    wrong_count = wrong_count + 1,
                    review_streak = 0,
                    mastered = 0,
                    last_wrong_at = excluded.last_wrong_at,
                    last_reviewed_at = COALESCE(
                        excluded.last_reviewed_at,
                        wrong_questions.last_reviewed_at
                    )
                """,
                (learner_id, question_id, timestamp, reviewed_at),
            )

    def record_corrected(
        self,
        learner_id: str,
        question_id: str,
        timestamp: str,
    ) -> None:
        """Mark an existing wrong question corrected after one review answer."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE wrong_questions
                SET review_streak = 1,
                    mastered = 1,
                    last_reviewed_at = ?
                WHERE learner_id = ? AND question_id = ?
                """,
                (timestamp, learner_id, question_id),
            )

    def get_by_id(
        self, learner_id: str, question_id: str
    ) -> WrongQuestion | None:
        """Return one wrong-question record."""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM wrong_questions
                WHERE learner_id = ? AND question_id = ?
                """,
                (learner_id, question_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def get_all(self, learner_id: str | None = None) -> list[WrongQuestion]:
        """Return one learner's records, or all records for read-only viewing."""
        with self.database.connect() as connection:
            if learner_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM wrong_questions
                    ORDER BY last_wrong_at DESC, learner_id, question_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM wrong_questions
                    WHERE learner_id = ?
                    ORDER BY last_wrong_at DESC, question_id
                    """,
                    (learner_id,),
                ).fetchall()
        return [self._to_model(row) for row in rows]

    def delete_all_for_learner(self, learner_id: str) -> int:
        """Delete one learner's current mistake state and return its row count."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM wrong_questions WHERE learner_id = ?",
                (learner_id,),
            )
        return cursor.rowcount

    @staticmethod
    def _to_model(row: sqlite3.Row) -> WrongQuestion:
        return WrongQuestion(
            learner_id=row["learner_id"],
            question_id=row["question_id"],
            wrong_count=int(row["wrong_count"]),
            review_streak=int(row["review_streak"]),
            corrected=bool(row["mastered"]),
            last_wrong_at=row["last_wrong_at"],
            last_reviewed_at=row["last_reviewed_at"],
        )
