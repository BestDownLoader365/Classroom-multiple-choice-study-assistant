"""Per-course question-bank state: fingerprint, generation, catalogue shape.

Each course owns exactly one ``question_bank_state`` row, keyed by
``course_id``.  The row records which raw bank fingerprint was loaded last, that
course's structural generation, and the shape of its catalogue.

There is deliberately **no** global generation: a structural update of course A
bumps only A's row, so A's stale workers are fenced while B and C keep serving.
The generation drives the stale-worker guard; cosmetic edits — including
source/chapter *titles* — leave it untouched so unchanged workers keep serving.
Per-question reconciliation lives in ``QuestionBankSyncService``; this
repository only owns the state row, never learner data.

A missing row is *not* the same as "generation 0": :meth:`get_state` returns
``None`` so callers can tell "this course was never synced here" from "this
course was synced and its generation is zero".
"""

import sqlite3

from .course_scope import require_course_id
from .database import Database


class QuestionBankStateRepository:
    def __init__(self, database: Database, course_id: str) -> None:
        self.database = database
        self.course_id = require_course_id(course_id)

    def get_state(self) -> tuple[str, int] | None:
        """Return this course's ``(bank_version, generation)`` or ``None``.

        ``None`` means this course has no recorded bank state at all — distinct
        from a recorded generation of ``0``.
        """
        with self.database.connect() as connection:
            row = self._read_row(connection)
        if row is None:
            return None
        return row["bank_version"], int(row["generation"])

    def has_state(self) -> bool:
        """Return whether this course was ever synced in this database."""
        return self.get_state() is not None

    def get_generation(self) -> int:
        """Return this course's structural generation (0 when never stored)."""
        with self.database.connect() as connection:
            row = self._read_row(connection)
        return int(row["generation"]) if row is not None else 0

    def get_catalogue_fingerprint(self) -> str | None:
        """Return this course's stored catalogue shape; ``None`` before tracking."""
        with self.database.connect() as connection:
            row = self._read_row(connection)
        if row is None:
            return None
        return row["catalogue_fingerprint"] or None

    def save_state(
        self,
        bank_version: str,
        generation: int,
        catalogue_fingerprint: str | None = None,
    ) -> None:
        """Persist this course's fingerprint, generation and catalogue shape.

        ``catalogue_fingerprint=None`` leaves any stored value untouched, so a
        caller that only knows the bank version can never erase the baseline by
        accident.
        """
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO question_bank_state
                    (course_id, bank_version, generation, catalogue_fingerprint)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(course_id) DO UPDATE SET
                    bank_version = excluded.bank_version,
                    generation = excluded.generation,
                    catalogue_fingerprint = COALESCE(
                        excluded.catalogue_fingerprint,
                        question_bank_state.catalogue_fingerprint
                    )""",
                (self.course_id, bank_version, generation, catalogue_fingerprint),
            )

    def _read_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT bank_version, generation, catalogue_fingerprint
            FROM question_bank_state WHERE course_id = ?""",
            (self.course_id,),
        ).fetchone()
