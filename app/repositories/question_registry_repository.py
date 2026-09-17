"""Persistence for the permanent per-question identity registry.

The registry is the long-lived memory of the question bank: every question
ID ever loaded is kept here with its grading identity.  Rows are never
deleted — a question that leaves ``questions.json`` is only marked
``retired`` — so a retired ID can never be silently recycled for a
human-different question later.
"""

import json
import sqlite3

from app.models import QuestionRegistryEntry, QuestionRegistryStatus

from .database import Database


class QuestionRegistryRepository:
    """Read and write ``question_registry`` rows."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get_all(self) -> dict[str, QuestionRegistryEntry]:
        """Return every registry row keyed by question ID."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM question_registry ORDER BY question_id"
            ).fetchall()
        return {row["question_id"]: self._to_model(row) for row in rows}

    def count(self) -> int:
        """Return the number of tracked question IDs (active and retired)."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM question_registry"
            ).fetchone()
        return int(row["total"])

    def upsert(self, entry: QuestionRegistryEntry) -> None:
        """Insert or replace one registry row."""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO question_registry (
                    question_id, status, question_type, option_ids,
                    correct_answers, content_fingerprint,
                    first_seen_at, last_seen_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(question_id) DO UPDATE SET
                    status = excluded.status,
                    question_type = excluded.question_type,
                    option_ids = excluded.option_ids,
                    correct_answers = excluded.correct_answers,
                    content_fingerprint = excluded.content_fingerprint,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    retired_at = excluded.retired_at
                """,
                (
                    entry.question_id,
                    entry.status.value,
                    entry.question_type,
                    json.dumps(list(entry.option_ids), ensure_ascii=False),
                    json.dumps(list(entry.correct_answers), ensure_ascii=False),
                    entry.content_fingerprint,
                    entry.first_seen_at,
                    entry.last_seen_at,
                    entry.retired_at,
                ),
            )

    def upsert_many(self, entries: list[QuestionRegistryEntry]) -> None:
        """Insert or replace several registry rows."""
        for entry in entries:
            self.upsert(entry)

    def retire(self, question_id: str, *, retired_at: str) -> None:
        """Mark one question as removed from the bank, keeping its tombstone."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE question_registry
                SET status = ?, retired_at = ?, last_seen_at = ?
                WHERE question_id = ?
                """,
                (
                    QuestionRegistryStatus.RETIRED.value,
                    retired_at,
                    retired_at,
                    question_id,
                ),
            )

    @staticmethod
    def tombstone_for(question_id: str, *, retired_at: str) -> QuestionRegistryEntry:
        """Build a retired row for an orphaned ID found only in history.

        The grading columns stay empty: no bank version of this ID was ever
        observed, so a later reappearance is adopted instead of rejected.
        """
        return QuestionRegistryEntry(
            question_id=question_id,
            status=QuestionRegistryStatus.RETIRED,
            question_type="",
            option_ids=(),
            correct_answers=(),
            content_fingerprint="",
            first_seen_at=retired_at,
            last_seen_at=retired_at,
            retired_at=retired_at,
        )

    @staticmethod
    def _to_model(row: sqlite3.Row) -> QuestionRegistryEntry:
        return QuestionRegistryEntry(
            question_id=row["question_id"],
            status=QuestionRegistryStatus(row["status"]),
            question_type=row["question_type"],
            option_ids=tuple(json.loads(row["option_ids"])),
            correct_answers=tuple(json.loads(row["correct_answers"])),
            content_fingerprint=row["content_fingerprint"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            retired_at=row["retired_at"],
        )