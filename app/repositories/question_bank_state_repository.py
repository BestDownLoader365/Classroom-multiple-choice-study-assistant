"""Question-bank state row: active fingerprint and structural generation.

The single ``question_bank_state`` row records which raw bank fingerprint
was loaded last and the structural bank generation.  The generation is only
bumped when the question set itself changes (new, deleted, grading-changed
or resurrected questions) and drives the stale-worker guard; cosmetic
edits leave it untouched so unchanged workers keep serving.  Per-question
reconciliation lives in ``QuestionBankSyncService`` — this repository only
owns the state row, never learner data.
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

    def save_state(self, bank_version: str, generation: int) -> None:
        """Persist the active fingerprint and generation atomically."""
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO question_bank_state VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    bank_version = excluded.bank_version,
                    generation = excluded.generation""",
                (bank_version, generation),
            )

    @staticmethod
    def _read_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT bank_version, generation FROM question_bank_state WHERE id = 1"
        ).fetchone()