"""Persistence for answer attempts, scoped to one course namespace."""

import json

from app.models import Attempt, QuizMode

from .course_scope import require_course_id
from .database import MAX_ATTEMPTS_PER_QUESTION, Database


class AttemptRepository:
    """Store the recent answers of one course, ten per learner/question at most.

    ``course_id`` is required at construction and immutable.  Every statement
    below restricts itself to that namespace, so ``list_all()`` returns "all
    accounts of this course" and the retention sweep can never delete another
    course's history even when two courses reuse the same local ``question_id``.
    """

    def __init__(self, database: Database, course_id: str) -> None:
        self.database = database
        self.course_id = require_course_id(course_id)

    def add(self, attempt: Attempt) -> int:
        """Persist an attempt and return its generated identifier."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO attempts (
                    learner_id, course_id, question_id, mode, selected_answers,
                    is_correct, answered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.learner_id,
                    self.course_id,
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
                  AND course_id = ?
                  AND question_id = ?
                  AND id NOT IN (
                      SELECT id
                      FROM attempts
                      WHERE learner_id = ? AND course_id = ? AND question_id = ?
                      ORDER BY answered_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (
                    attempt.learner_id,
                    self.course_id,
                    attempt.question_id,
                    attempt.learner_id,
                    self.course_id,
                    attempt.question_id,
                    MAX_ATTEMPTS_PER_QUESTION,
                ),
            )
            return int(cursor.lastrowid)

    def count(self) -> int:
        """Return the number of stored attempts in this course."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM attempts WHERE course_id = ?",
                (self.course_id,),
            ).fetchone()
        return int(row["total"])

    def delete_for_questions(self, question_ids) -> int:
        """Delete this course's attempts for the given local question IDs.

        Used when a question's grading identity changes inside this course: its
        stored answers no longer have a reliable verdict.  Deleted questions
        keep their history instead, so this is never called for them.  Another
        course's identically named question is untouched by construction.
        """
        ids = [str(question_id) for question_id in question_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM attempts WHERE course_id = ? "
                f"AND question_id IN ({placeholders})",
                [self.course_id, *ids],
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
                WHERE learner_id = ? AND course_id = ?
                  AND question_id = ? AND mode = ?
                ORDER BY answered_at DESC, id DESC
                LIMIT 1
                """,
                (learner_id, self.course_id, question_id, mode.value),
            ).fetchone()
        return self._to_model(row) if row else None

    def distinct_question_ids(self) -> set[str]:
        """Return every question ID this course's attempts reference."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT question_id FROM attempts WHERE course_id = ?",
                (self.course_id,),
            ).fetchall()
        return {row["question_id"] for row in rows}

    def list_for_learner(self, learner_id: str) -> list[Attempt]:
        """Return one learner's retained attempts in this course, oldest first.

        The attempts table keeps at most ``MAX_ATTEMPTS_PER_QUESTION`` rows per
        learner/question, so this is a bounded, recent learning window rather
        than a lifetime history. Aggregation stays in the statistics service,
        keeping SQL minimal.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE learner_id = ? AND course_id = ?
                ORDER BY answered_at, id
                """,
                (learner_id, self.course_id),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def list_all(self) -> list[Attempt]:
        """Return every account's retained attempts *in this course*.

        The per-learner retention bound also bounds the whole course slice
        (learners × questions × ``MAX_ATTEMPTS_PER_QUESTION``), so the
        cross-account statistics service can aggregate in plain Python.  This is
        not a whole-database scan: a bound repository never leaves its course.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE course_id = ?
                ORDER BY answered_at, id
                """,
                (self.course_id,),
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
        """Return each question's latest incorrect selection for one learner."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT question_id, selected_answers
                FROM attempts
                WHERE learner_id = ? AND course_id = ? AND is_correct = 0
                ORDER BY answered_at DESC, id DESC
                """,
                (learner_id, self.course_id),
            ).fetchall()
        latest: dict[str, tuple[str, ...]] = {}
        for row in rows:
            if row["question_id"] not in latest:
                latest[row["question_id"]] = tuple(json.loads(row["selected_answers"]))
        return latest

