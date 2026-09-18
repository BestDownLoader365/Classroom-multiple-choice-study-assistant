"""Question-bank state row: active fingerprint, structural generation, catalogue.

The single ``question_bank_state`` row records which raw bank fingerprint was
loaded last, the structural bank generation, and the shape of the loaded course
catalogue.  The generation is bumped when the bank's structure changes (new,
deleted, grading-changed, chapter/source-moved or resurrected questions, or a
changed catalogue shape) and drives the stale-worker guard; cosmetic edits,
including source/chapter *titles*, leave it untouched so unchanged workers keep
serving.  Per-question reconciliation lives in ``QuestionBankSyncService`` —
this repository only owns the state row, never learner data.
"""

import sqlite3

from .database import Database


class QuestionBankStateRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_state(self) -> tuple[str, int] | None:
        """Return ``(bank_version, generation)`` or ``None`` when unset."""
        with self.database.connect() as connection:
            row = self._read_row(connection)
        if row is None:
            return None
        return row["bank_version"], int(row["generation"])

    def get_generation(self) -> int:
        """Return the active structural generation (0 when never stored)."""
        with self.database.connect() as connection:
            row = self._read_row(connection)
        return int(row["generation"]) if row is not None else 0

    def get_catalogue_fingerprint(self) -> str | None:
        """Return the stored catalogue shape; ``None`` before it was tracked."""
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
        """Persist the active fingerprint, generation and catalogue shape.

        ``catalogue_fingerprint=None`` leaves any stored value untouched, so a
        caller that only knows the bank version can never erase the baseline by
        accident.
        """
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO question_bank_state
                    (id, bank_version, generation, catalogue_fingerprint)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    bank_version = excluded.bank_version,
                    generation = excluded.generation,
                    catalogue_fingerprint = COALESCE(
                        excluded.catalogue_fingerprint,
                        question_bank_state.catalogue_fingerprint
                    )""",
                (bank_version, generation, catalogue_fingerprint),
            )

    @staticmethod
    def _read_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT bank_version, generation, catalogue_fingerprint
            FROM question_bank_state WHERE id = 1"""
        ).fetchone()