"""Persistence for mock-exam sessions and their fixed question slots.

Everything here is scoped to one course.  ``exam_id`` stays globally unique, but
a session always has an explicit ``course_id``, so knowing a bare ``exam_id`` is
never enough to read or change another course's exam.

The slots (``exam_questions``) intentionally store **no** ``course_id``: their
namespace is the parent session, and every slot statement proves that parent
membership with an ``EXISTS`` predicate instead of duplicating the column.  That
keeps the namespace impossible to desynchronise.
"""

import json
import sqlite3

from app.models import ExamQuestion, ExamSession, ExamStatus

from .course_scope import require_course_id
from .database import Database

#: Predicate proving that ``exam_questions.exam_id`` belongs to this course.
_SLOT_SCOPE = (
    "EXISTS (SELECT 1 FROM exam_sessions s "
    "WHERE s.id = exam_questions.exam_id AND s.course_id = ?)"
)


class ExamRepository:
    """Store exam sessions and per-question answers for one course."""

    def __init__(self, database: Database, course_id: str) -> None:
        self.database = database
        self.course_id = require_course_id(course_id)

    def create(
        self,
        session: ExamSession,
        question_ids: tuple[str, ...],
        grading_fingerprints: tuple[str | None, ...] | None = None,
    ) -> None:
        """Persist a new exam together with its fixed, deduplicated slots.

        ``grading_fingerprints`` records each question's grading identity at
        creation time so later reports can detect slots whose grading rule
        drifted; omitted fingerprints stay ``NULL`` (legacy behavior).

        The session's ``course_id`` always comes from this repository's binding,
        which is the authoritative namespace for the write.
        """
        if grading_fingerprints is None:
            grading_fingerprints = (None,) * len(question_ids)
        if len(grading_fingerprints) != len(question_ids):
            raise ValueError("grading_fingerprints must align with question_ids")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO exam_sessions (
                    id, course_id, learner_id, status, question_count,
                    time_limit_seconds, option_seed, created_at, started_at,
                    deadline_at, submitted_at, current_position, correct_count,
                    duration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    self.course_id,
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
            for position, (question_id, fingerprint) in enumerate(
                zip(question_ids, grading_fingerprints)
            ):
                connection.execute(
                    """
                    INSERT INTO exam_questions (
                        exam_id, position, question_id, grading_fingerprint
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (session.id, position, question_id, fingerprint),
                )

    def get_for_learner(
        self, learner_id: str, exam_id: str
    ) -> ExamSession | None:
        """Return one exam only when it belongs to ``learner_id`` and this course."""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE id = ? AND learner_id = ? AND course_id = ?
                """,
                (exam_id, learner_id, self.course_id),
            ).fetchone()
        return self._to_session(row) if row else None

    def get_by_id(self, exam_id: str) -> ExamSession | None:
        """Return one exam of this course, regardless of learner ownership.

        Used for course-scope validation; learner-facing reads go through
        :meth:`get_for_learner` so ownership is always checked too.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM exam_sessions WHERE id = ? AND course_id = ?",
                (exam_id, self.course_id),
            ).fetchone()
        return self._to_session(row) if row else None

    def owns(self, exam_id: str) -> bool:
        """Return whether this course owns ``exam_id``."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 AS found FROM exam_sessions WHERE id = ? AND course_id = ?",
                (exam_id, self.course_id),
            ).fetchone()
        return row is not None

    def get_active_for_learner(
        self, learner_id: str, now: str
    ) -> ExamSession | None:
        """Return the learner's most recent unfinished, unexpired exam here.

        Exams whose deadline has passed are excluded even before they are
        finalized, so a stale "in progress" banner can never outlive its
        authoritative deadline.  Only this course's exams are considered: using
        course B never settles course A's expired session.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE learner_id = ? AND course_id = ? AND status = 'in_progress'
                  AND (deadline_at IS NULL OR deadline_at > ?)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (learner_id, self.course_id, now),
            ).fetchone()
        return self._to_session(row) if row else None

    def list_expired_in_progress(
        self, learner_id: str, now: str
    ) -> list[ExamSession]:
        """Return this course's in-progress exams whose deadline was reached."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE learner_id = ? AND course_id = ? AND status = 'in_progress'
                  AND deadline_at IS NOT NULL AND deadline_at <= ?
                ORDER BY created_at, id
                """,
                (learner_id, self.course_id, now),
            ).fetchall()
        return [self._to_session(row) for row in rows]

    def list_for_learner(
        self, learner_id: str, *, limit: int = 20
    ) -> list[ExamSession]:
        """Return this course's exams of one learner, newest first."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE learner_id = ? AND course_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (learner_id, self.course_id, limit),
            ).fetchall()
        return [self._to_session(row) for row in rows]

    def list_in_progress(self) -> list[ExamSession]:
        """Return every learner's unfinished exams *of this course*."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_sessions
                WHERE course_id = ? AND status = 'in_progress'
                ORDER BY created_at, id
                """,
                (self.course_id,),
            ).fetchall()
        return [self._to_session(row) for row in rows]

    def distinct_question_ids(self) -> set[str]:
        """Return every question ID referenced by this course's exam slots."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT question_id FROM exam_questions "
                f"WHERE {_SLOT_SCOPE}",
                (self.course_id,),
            ).fetchall()
        return {row["question_id"] for row in rows}

    def get_questions(self, exam_id: str) -> list[ExamQuestion]:
        """Return the fixed slots of one exam of this course, in exam order."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exam_questions
                WHERE exam_id = ?
                  AND EXISTS (
                      SELECT 1 FROM exam_sessions s
                      WHERE s.id = exam_questions.exam_id AND s.course_id = ?
                  )
                ORDER BY position
                """,
                (exam_id, self.course_id),
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
        ``NULL``), so the stored state always mirrors the visible options.  Both
        statements are guarded by the parent session's course, so a bare
        ``exam_id`` from another course changes nothing.
        """
        with self.database.connect() as connection:
            connection.execute(
                f"""
                UPDATE exam_questions
                SET selected_answers = ?,
                    answered_at = ?
                WHERE exam_id = ? AND position = ?
                  AND {_SLOT_SCOPE}
                """,
                (
                    json.dumps(selected_answers, ensure_ascii=False),
                    answered_at if selected_answers else None,
                    exam_id,
                    position,
                    self.course_id,
                ),
            )
            connection.execute(
                """
                UPDATE exam_sessions
                SET current_position = ?
                WHERE id = ? AND course_id = ?
                """,
                (position, exam_id, self.course_id),
            )

    def apply_grading(
        self, exam_id: str, results: list[tuple[int, bool]]
    ) -> None:
        """Store the graded outcome of every answered slot."""
        with self.database.connect() as connection:
            for position, is_correct in results:
                connection.execute(
                    f"""
                    UPDATE exam_questions
                    SET is_correct = ?
                    WHERE exam_id = ? AND position = ?
                      AND {_SLOT_SCOPE}
                    """,
                    (int(is_correct), exam_id, position, self.course_id),
                )

    def replace_slots(self, exam_id: str, slots: list[ExamQuestion]) -> None:
        """Rewrite one exam's slots in order, resequencing positions.

        Used to drop slots whose question left the bank or changed grading.
        All rows are deleted before reinsertion so the
        ``UNIQUE (exam_id, question_id)`` constraint cannot collide with a
        shifted position, and every kept slot's saved answer state survives.
        """
        with self.database.connect() as connection:
            if not self.owns(exam_id):
                return
            connection.execute(
                """
                DELETE FROM exam_questions
                WHERE exam_id = ?
                  AND EXISTS (
                      SELECT 1 FROM exam_sessions s
                      WHERE s.id = exam_questions.exam_id AND s.course_id = ?
                  )
                """,
                (exam_id, self.course_id),
            )
            for position, slot in enumerate(slots):
                connection.execute(
                    """
                    INSERT INTO exam_questions (
                        exam_id, position, question_id, selected_answers,
                        is_correct, answered_at, grading_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exam_id,
                        position,
                        slot.question_id,
                        (
                            json.dumps(slot.selected_answers, ensure_ascii=False)
                            if slot.selected_answers
                            else None
                        ),
                        (
                            None
                            if slot.is_correct is None
                            else int(slot.is_correct)
                        ),
                        slot.answered_at,
                        slot.grading_fingerprint,
                    ),
                )

    def update_layout(
        self, exam_id: str, *, question_count: int, current_position: int
    ) -> None:
        """Store the exam's layout after its slot set changed."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE exam_sessions
                SET question_count = ?, current_position = ?
                WHERE id = ? AND course_id = ?
                """,
                (question_count, current_position, exam_id, self.course_id),
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
        """Flip an in-progress exam of this course to a finished state once.

        The ``status = 'in_progress'`` guard makes submission idempotent: a
        repeated or concurrent submit affects zero rows and returns ``False``
        instead of writing results twice.
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE exam_sessions
                SET status = ?,
                    submitted_at = ?,
                    correct_count = ?,
                    duration_seconds = ?
                WHERE id = ? AND course_id = ? AND status = 'in_progress'
                """,
                (
                    status.value,
                    submitted_at,
                    correct_count,
                    duration_seconds,
                    exam_id,
                    self.course_id,
                ),
            )
        return cursor.rowcount == 1

    def delete_for_learner(self, learner_id: str) -> int:
        """Delete one learner's exams of this course (administration only)."""
        with self.database.connect() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM exam_sessions "
                    "WHERE learner_id = ? AND course_id = ?",
                    (learner_id, self.course_id),
                )
            ]
            for exam_id in ids:
                connection.execute(
                    "DELETE FROM exam_questions WHERE exam_id = ?", (exam_id,)
                )
            cursor = connection.execute(
                "DELETE FROM exam_sessions WHERE learner_id = ? AND course_id = ?",
                (learner_id, self.course_id),
            )
        return cursor.rowcount

    def count(self) -> int:
        """Return the number of stored exams of this course."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM exam_sessions WHERE course_id = ?",
                (self.course_id,),
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
            course_id=row["course_id"],
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
            grading_fingerprint=row["grading_fingerprint"],
        )

