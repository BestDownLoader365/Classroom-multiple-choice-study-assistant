"""Question-bank fingerprint tracking and per-bank data synchronization.

Detecting a question-bank change and resetting the affected learning data is
a business rule, not a connection-management concern, so it lives in its own
repository rather than on the Database connection manager.
"""

from .database import Database


class QuestionBankStateRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def synchronize(self, bank_version: str) -> int:
        """Atomically reset course data once per bank change across workers.

        Older databases infer their bank from saved progress. If they contain
        learning data but no fingerprint, reset it rather than misattribute it.
        """
        with self.database.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS question_bank_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bank_version TEXT NOT NULL,
                generation INTEGER NOT NULL
            )""")
            row = connection.execute(
                "SELECT bank_version, generation FROM question_bank_state WHERE id = 1"
            ).fetchone()
            generation = row["generation"] if row else 0
            if row:
                changed = row["bank_version"] != bank_version
            else:
                versions = {item[0] for item in connection.execute(
                    "SELECT DISTINCT bank_version FROM quiz_progress"
                )}
                has_history = any(connection.execute(
                    f"SELECT 1 FROM {table} LIMIT 1"
                ).fetchone() for table in ("attempts", "wrong_questions"))
                changed = bool(versions - {bank_version}) or (not versions and has_history)
            if changed:
                for table in (
                    "attempts",
                    "wrong_questions",
                    "weak_knowledge_points",
                    "quiz_progress",
                    "exam_questions",
                    "exam_sessions",
                ):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute("DELETE FROM sqlite_sequence WHERE name = 'attempts'")
                generation += 1
            connection.execute(
                """INSERT INTO question_bank_state VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    bank_version = excluded.bank_version,
                    generation = excluded.generation""",
                (bank_version, generation),
            )
            return generation