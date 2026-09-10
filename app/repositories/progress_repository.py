"""Account-scoped resumable practice stored on the server."""

import json
from typing import Any

from app.models import QuizMode
from .database import Database


class ProgressRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, learner_id: str, mode: QuizMode) -> tuple[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT bank_version, state FROM quiz_progress WHERE learner_id = ? AND mode = ?",
                (learner_id, mode.value),
            ).fetchone()
        if row is None:
            return None
        return row["bank_version"], json.loads(row["state"]) if row["state"] else None

    def save(self, learner_id: str, mode: QuizMode, bank_version: str, state: Any) -> None:
        # A null state is a tombstone: an old device must not restore a reset round.
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO quiz_progress (learner_id, mode, bank_version, state)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(learner_id, mode) DO UPDATE SET
                       bank_version = excluded.bank_version, state = excluded.state""",
                (learner_id, mode.value, bank_version,
                 json.dumps(state, ensure_ascii=False) if state is not None else None),
            )
