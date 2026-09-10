"""Persistence for answer attempts."""

import json

from app.models import Attempt

from .database import Database, MAX_ATTEMPTS_PER_QUESTION


class AttemptRepository:
    """Store up to three recent answers per learner and question."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, attempt: Attempt) -> int:
        """Persist an attempt and return its generated identifier."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO attempts (
                    learner_id, question_id, mode, selected_answers,
                    is_correct, answered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.learner_id,
                    attempt.question_id,
                    attempt.mode.value,
                    json.dumps(attempt.selected_answers, ensure_ascii=False),
                    int(attempt.is_correct),
                    attempt.answered_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM attempts
                WHERE learner_id = ?
                  AND question_id = ?
                  AND id NOT IN (
                      SELECT id
                      FROM attempts
                      WHERE learner_id = ? AND question_id = ?
                      ORDER BY answered_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (
                    attempt.learner_id,
                    attempt.question_id,
                    attempt.learner_id,
                    attempt.question_id,
                    MAX_ATTEMPTS_PER_QUESTION,
                ),
            )
            return int(cursor.lastrowid)

    def count(self) -> int:
        """Return the number of stored attempts (primarily useful for tests)."""
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM attempts").fetchone()
        return int(row["total"])

    def get_latest_incorrect_answers(
        self, learner_id: str
    ) -> dict[str, tuple[str, ...]]:
        """Return each question's most recent incorrect selection for one learner."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT question_id, selected_answers
                FROM attempts
                WHERE learner_id = ? AND is_correct = 0
                ORDER BY answered_at DESC, id DESC
                """,
                (learner_id,),
            ).fetchall()
        latest: dict[str, tuple[str, ...]] = {}
        for row in rows:
            if row["question_id"] not in latest:
                latest[row["question_id"]] = tuple(json.loads(row["selected_answers"]))
        return latest
