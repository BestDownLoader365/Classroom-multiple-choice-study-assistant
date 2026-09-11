"""Persistence for mock-exam sessions and their fixed question slots."""

import json
import sqlite3

from app.models import ExamQuestion, ExamSession, ExamStatus

from .database import Database


class ExamRepository:
    """Store exam sessions and per-question answers, scoped per learner."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self, session: ExamSession, question_ids: tuple[str, ...]
    ) -> None:
        """Persist a new exam together with its fixed, deduplicated slots."""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO exam_sessions (
                    id, learner_id, status, question_count, time_limit_seconds,
                    option_seed, created_at, started_at, deadline_at,
                    submitted_at, current_position, correct_count,
                    duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.learner_id,
                    session.status.value,
                    session.question_count,
                    session.time_limit_seconds,
                    session.option_seed,
                    session.created_at,
                    session.started_at,
                    session.deadline_at,
                    session.submitted_at,
                    session.current_position,
                    session.correct_count,
                    session.duration_seconds,
                ),
            )
            for position, question_id in enumerate(question_ids):
                connection.execute(
                    """
                    INSERT INTO exam_questions (exam_id, position, question_id)
                    VALUES (?, ?, ?)
                    """,
                    (session.id, position, question_id),
                )

    def get_for_learner(
        self, learner_id: str, exam_id: str
    ) -> ExamSession | None:
        """Return one exam only when it belongs to ``learner_id``."""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE id = ? AND learner_id = ?
                """,
                (exam_id, learner_id),
            ).fetchone()
        return self._to_session(row) if row else None

    def get_active_for_learner(
        self, learner_id: str, now: str
    ) -> ExamSession | None:
        """Return the learner's most recent unfinished, unexpired exam.

        Exams whose deadline has passed are excluded even before they are
        finalized, so a stale "in progress" banner can never outlive its
        authoritative deadline.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE learner_id = ? AND status = 'in_progress'
                  AND (deadline_at IS NULL OR deadline_at > ?)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (learner_id, now),
            ).fetchone()
        return self._to_session(row) if row else None

    def list_expired_in_progress(
        self, learner_id: str, now: str
    ) -> list[ExamSession]:
        """Return one learner's in-progress exams whose deadline was reached."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE learner_id = ? AND status = 'in_progress'
                  AND deadline_at IS NOT NULL AND deadline_at <= ?
                ORDER BY created_at, id
                """,
                (learner_id, now),
            ).fetchall()
        return [self._to_session(row) for row in rows]

    def list_for_learner(
        self, learner_id: str, *, limit: int = 20
    ) -> list[ExamSession]:
        """Return the learner's exams, newest first, for the history list."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE learner_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (learner_id, limit),
            ).fetchall()
        return [self._to_session(row) for row in rows]

    def get_questions(self, exam_id: str) -> list[ExamQuestion]:
        """Return the fixed question slots of one exam in exam order."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_questions
                WHERE exam_id = ?
                ORDER BY position
                """,
                (exam_id,),
            ).fetchall()
        return [self._to_question(row) for row in rows]

    def save_answer(
        self,
        exam_id: str,
        position: int,
        selected_answers: tuple[str, ...],
        answered_at: str,
    ) -> None:
        """Replace the stored selection for one slot and remember the visit.

        An empty selection clears the slot again (``answered_at`` returns to
        ``NULL``), so the stored state always mirrors the visible options.
        """
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE exam_questions
                SET selected_answers = ?,
                    answered_at = ?
                WHERE exam_id = ? AND position = ?
                """,
                (
                    json.dumps(selected_answers, ensure_ascii=False),
                    answered_at if selected_answers else None,
                    exam_id,
                    position,
                ),
            )
            connection.execute(
                """
                UPDATE exam_sessions
                SET current_position = ?
                WHERE id = ?
                """,
                (position, exam_id),
            )

    def apply_grading(
        self, exam_id: str, results: list[tuple[int, bool]]
    ) -> None:
        """Store the graded outcome of every answered slot."""
        with self.database.connect() as connection:
            for position, is_correct in results:
                connection.execute(
                    """
                    UPDATE exam_questions
                    SET is_correct = ?
                    WHERE exam_id = ? AND position = ?
                    """,
                    (int(is_correct), exam_id, position),
                )

    def finalize(
        self,
        exam_id: str,
        *,
        status: ExamStatus,
        submitted_at: str,
        correct_count: int,
        duration_seconds: int,
    ) -> bool:
        """Flip an in-progress exam to a finished state exactly once.

        The ``WHERE status = 'in_progress'`` guard makes submission
        idempotent: a repeated or concurrent submit affects zero rows and
        returns ``False`` instead of writing results twice.
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE exam_sessions
                SET status = ?,
                    submitted_at = ?,
                    correct_count = ?,
                    duration_seconds = ?
                WHERE id = ? AND status = 'in_progress'
                """,
                (
                    status.value,
                    submitted_at,
                    correct_count,
                    duration_seconds,
                    exam_id,
                ),
            )
        return cursor.rowcount == 1

    def count(self) -> int:
        """Return the number of stored exams (primarily useful for tests)."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM exam_sessions"
            ).fetchone()
        return int(row["total"])

    @staticmethod
    def _to_session(row: sqlite3.Row) -> ExamSession:
        return ExamSession(
            id=row["id"],
            learner_id=row["learner_id"],
            status=ExamStatus(row["status"]),
            question_count=int(row["question_count"]),
            time_limit_seconds=(
                None
                if row["time_limit_seconds"] is None
                else int(row["time_limit_seconds"])
            ),
            option_seed=row["option_seed"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            deadline_at=row["deadline_at"],
            submitted_at=row["submitted_at"],
            current_position=int(row["current_position"]),
            correct_count=(
                None if row["correct_count"] is None else int(row["correct_count"])
            ),
            duration_seconds=(
                None
                if row["duration_seconds"] is None
                else int(row["duration_seconds"])
            ),
        )

    @staticmethod
    def _to_question(row: sqlite3.Row) -> ExamQuestion:
        raw_answers = row["selected_answers"]
        return ExamQuestion(
            exam_id=row["exam_id"],
            position=int(row["position"]),
            question_id=row["question_id"],
            selected_answers=(
                tuple(json.loads(raw_answers)) if raw_answers else ()
            ),
            is_correct=(
                None if row["is_correct"] is None else bool(row["is_correct"])
            ),
            answered_at=row["answered_at"],
        )
