"""Account-scoped resumable practice stored on the server, per course."""

import json
from typing import Any

from app.models import QuizMode

from .course_scope import require_course_id
from .database import Database


class ProgressRepository:
    """Store one learner's resumable round per mode, inside one course.

    The primary key is ``(learner_id, course_id, mode)``: switching courses is a
    plain namespace change, and two courses keep fully independent seeds,
    answer tokens, queues and feedback.  ``get_all()`` iterates the bound
    course only.
    """

    def __init__(self, database: Database, course_id: str) -> None:
        self.database = database
        self.course_id = require_course_id(course_id)

    def get(self, learner_id: str, mode: QuizMode) -> tuple[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT bank_version, state FROM quiz_progress "
                "WHERE learner_id = ? AND course_id = ? AND mode = ?",
                (learner_id, self.course_id, mode.value),
            ).fetchone()
        if row is None:
            return None
        return row["bank_version"], json.loads(row["state"]) if row["state"] else None

    def get_all(self) -> list[tuple[str, QuizMode, str, Any]]:
        """Return every stored progress row *of this course* for reconciliation."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT learner_id, mode, bank_version, state FROM quiz_progress "
                "WHERE course_id = ?",
                (self.course_id,),
            ).fetchall()
        return [
            (
                row["learner_id"],
                QuizMode(row["mode"]),
                row["bank_version"],
                json.loads(row["state"]) if row["state"] else None,
            )
            for row in rows
        ]

    def save(
        self, learner_id: str, mode: QuizMode, bank_version: str, state: Any
    ) -> None:
        # A null state is a tombstone: an old device must not restore a reset round.
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO quiz_progress
                       (learner_id, course_id, mode, bank_version, state)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(learner_id, course_id, mode) DO UPDATE SET
                       bank_version = excluded.bank_version,
                       state = excluded.state""",
                (
                    learner_id,
                    self.course_id,
                    mode.value,
                    bank_version,
                    json.dumps(state, ensure_ascii=False) if state is not None else None,
                ),
            )
