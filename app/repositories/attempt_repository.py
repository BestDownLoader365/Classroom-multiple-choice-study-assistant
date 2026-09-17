"""Persistence for answer attempts."""

import json

from app.models import Attempt, QuizMode

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

    def delete_for_questions(self, question_ids) -> int:
        """Delete every learner's attempts for the given questions.

        Used when a question's grading identity changes: its stored answers
        no longer have a reliable verdict.  Deleted questions keep their
        history instead, so this is never called for them.
        """
        ids = [str(question_id) for question_id in question_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM attempts WHERE question_id IN ({placeholders})",
                ids,
            )
        return cursor.rowcount

    def get_latest_attempt_for(
        self, learner_id: str, question_id: str, mode: QuizMode
    ) -> Attempt | None:
        """Return one learner's most recent attempt for a question in a mode."""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM attempts
                WHERE learner_id = ? AND question_id = ? AND mode = ?
                ORDER BY answered_at DESC, id DESC
                LIMIT 1
                """,
                (learner_id, question_id, mode.value),
            ).fetchone()
        return self._to_model(row) if row else None

    def distinct_question_ids(self) -> set[str]:
        """Return every question ID referenced by any stored attempt."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT question_id FROM attempts"
            ).fetchall()
        return {row["question_id"] for row in rows}

    def list_for_learner(self, learner_id: str) -> list[Attempt]:
        """Return one learner's retained attempts, oldest first.

        The attempts table keeps at most ``MAX_ATTEMPTS_PER_QUESTION`` rows
        per question, so this is a bounded, recent learning window rather
        than a lifetime history. Aggregation stays in the statistics
        service, keeping SQL minimal.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE learner_id = ?
                ORDER BY answered_at, id
                """,
                (learner_id,),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def list_all(self) -> list[Attempt]:
        """Return every account's retained attempts, oldest first.

        The per-learner retention bound also bounds the whole table
        (learners × questions × ``MAX_ATTEMPTS_PER_QUESTION``), so the
        cross-account statistics service can aggregate in plain Python.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                ORDER BY answered_at, id
                """,
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row) -> Attempt:
        return Attempt(
            id=int(row["id"]),
            learner_id=row["learner_id"],
            question_id=row["question_id"],
            mode=QuizMode(row["mode"]),
            selected_answers=tuple(json.loads(row["selected_answers"])),
            is_correct=bool(row["is_correct"]),
            answered_at=row["answered_at"],
        )

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
