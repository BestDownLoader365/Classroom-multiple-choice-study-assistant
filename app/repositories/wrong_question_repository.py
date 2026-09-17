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
        """Create or reopen a wrong question and clear correction state.

        Reopening also resets the SRS schedule: the question must pass the
        regular correction flow again before it is rescheduled.
        """
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
                    srs_level = 0,
                    next_review_at = NULL,
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
        *,
        srs_level: int | None = None,
        next_review_at: str | None = None,
    ) -> None:
        """Mark an existing wrong question corrected after one review answer.

        Passing ``srs_level``/``next_review_at`` (re)starts the SRS schedule;
        omitting them keeps any existing schedule untouched.
        """
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE wrong_questions
                SET review_streak = 1,
                    mastered = 1,
                    last_reviewed_at = ?,
                    srs_level = COALESCE(?, srs_level),
                    next_review_at = COALESCE(?, next_review_at)
                WHERE learner_id = ? AND question_id = ?
                """,
                (timestamp, srs_level, next_review_at, learner_id, question_id),
            )

    def record_srs_reviewed(
        self,
        learner_id: str,
        question_id: str,
        timestamp: str,
        *,
        srs_level: int,
        next_review_at: str,
    ) -> None:
        """Advance the SRS schedule of a corrected question whose review was due."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE wrong_questions
                SET srs_level = ?,
                    next_review_at = ?,
                    last_reviewed_at = ?
                WHERE learner_id = ? AND question_id = ? AND mastered = 1
                """,
                (srs_level, next_review_at, timestamp, learner_id, question_id),
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

    def get_due(self, learner_id: str, now: str) -> list[WrongQuestion]:
        """Return one learner's corrected records whose SRS review is due.

        ``now`` is an ISO timestamp; a record is due when its scheduled time
        has been reached (inclusive). Unscheduled legacy rows are excluded.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM wrong_questions
                WHERE learner_id = ?
                  AND mastered = 1
                  AND next_review_at IS NOT NULL
                  AND next_review_at <= ?
                ORDER BY next_review_at, question_id
                """,
                (learner_id, now),
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

    def delete_for_questions(self, question_ids) -> int:
        """Delete every learner's correction/SRS state for the given questions.

        Used when a question leaves the bank or its grading identity changes:
        the record cannot be answered meaningfully anymore, so it is dropped
        silently for every account at once.
        """
        ids = [str(question_id) for question_id in question_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM wrong_questions WHERE question_id IN ({placeholders})",
                ids,
            )
        return cursor.rowcount

    def distinct_question_ids(self) -> set[str]:
        """Return every question ID referenced by any wrong-question row."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT question_id FROM wrong_questions"
            ).fetchall()
        return {row["question_id"] for row in rows}

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
            srs_level=int(row["srs_level"] or 0),
            next_review_at=row["next_review_at"],
        )
