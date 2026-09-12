"""SQLite connection and schema management."""

import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

MAX_ATTEMPTS_PER_QUESTION = 10


class Database:
    """Create short-lived SQLite connections for repository operations."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._transaction_connection = ContextVar("mcq_connection", default=None)

    def initialize(self) -> None:
        """Create the local database and tables on first startup."""
        parent_existed = self.database_path.parent.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(self.database_path.parent, 0o700)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quiz_progress (
                    learner_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('normal', 'review')),
                    bank_version TEXT NOT NULL,
                    state TEXT,
                    PRIMARY KEY (learner_id, mode)
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    learner_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('normal', 'review', 'mock_exam')),
                    selected_answers TEXT NOT NULL,
                    is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                    answered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wrong_questions (
                    learner_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    wrong_count INTEGER NOT NULL DEFAULT 1,
                    review_streak INTEGER NOT NULL DEFAULT 0,
                    mastered INTEGER NOT NULL DEFAULT 0 CHECK (mastered IN (0, 1)),
                    srs_level INTEGER NOT NULL DEFAULT 0,
                    next_review_at TEXT,
                    last_wrong_at TEXT NOT NULL,
                    last_reviewed_at TEXT,
                    PRIMARY KEY (learner_id, question_id)
                );

                CREATE TABLE IF NOT EXISTS weak_knowledge_points (
                    learner_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    verified_question_ids TEXT NOT NULL DEFAULT '[]',
                    last_wrong_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (learner_id, chapter_id)
                );

                CREATE TABLE IF NOT EXISTS auth_rate_limits (
                    scope TEXT NOT NULL,
                    identifier_hash TEXT NOT NULL,
                    window_started_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    PRIMARY KEY (scope, identifier_hash)
                );

                CREATE TABLE IF NOT EXISTS exam_sessions (
                    id TEXT PRIMARY KEY,
                    learner_id TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('in_progress', 'submitted', 'expired')),
                    question_count INTEGER NOT NULL,
                    time_limit_seconds INTEGER,
                    option_seed TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    deadline_at TEXT,
                    submitted_at TEXT,
                    current_position INTEGER NOT NULL DEFAULT 0,
                    correct_count INTEGER,
                    duration_seconds INTEGER
                );

                CREATE TABLE IF NOT EXISTS exam_questions (
                    exam_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    question_id TEXT NOT NULL,
                    selected_answers TEXT,
                    is_correct INTEGER CHECK (is_correct IN (0, 1)),
                    answered_at TEXT,
                    PRIMARY KEY (exam_id, position),
                    UNIQUE (exam_id, question_id)
                );
                """
            )
            self._migrate_wrong_question_srs_columns(connection)
            self._migrate_attempt_mode_constraint(connection)
            connection.execute(
                """
                DELETE FROM attempts
                WHERE id IN (
                    SELECT id
                    FROM (
                        SELECT
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY learner_id, question_id
                                ORDER BY answered_at DESC, id DESC
                            ) AS retention_rank
                        FROM attempts
                    ) AS ranked_attempts
                    WHERE retention_rank > ?
                )
                """,
                (MAX_ATTEMPTS_PER_QUESTION,),
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_attempts_learner_question
                    ON attempts(learner_id, question_id);
                CREATE INDEX IF NOT EXISTS idx_wrong_questions_learner_mastered
                    ON wrong_questions(learner_id, mastered);
                CREATE INDEX IF NOT EXISTS idx_weak_points_learner_active
                    ON weak_knowledge_points(learner_id, active);
                CREATE INDEX IF NOT EXISTS idx_exam_sessions_learner
                    ON exam_sessions(learner_id, created_at);
                """
            )
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def _migrate_wrong_question_srs_columns(connection: sqlite3.Connection) -> None:
        """Add SRS scheduling columns to databases created before this feature.

        The ``CREATE TABLE`` above already covers fresh databases; these
        guarded ``ALTER TABLE`` statements upgrade existing ones in place.
        Legacy rows keep ``srs_level = 0`` and ``next_review_at = NULL``, so
        they are never scheduled until they are corrected again.
        """
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(wrong_questions)")
        }
        if "srs_level" not in columns:
            connection.execute(
                "ALTER TABLE wrong_questions "
                "ADD COLUMN srs_level INTEGER NOT NULL DEFAULT 0"
            )
        if "next_review_at" not in columns:
            connection.execute(
                "ALTER TABLE wrong_questions ADD COLUMN next_review_at TEXT"
            )

    @staticmethod
    def _migrate_attempt_mode_constraint(connection: sqlite3.Connection) -> None:
        """Allow the ``mock_exam`` attempt mode in pre-mock-exam databases.

        SQLite cannot alter a ``CHECK`` constraint, so databases created
        before mock exams are rebuilt in place. Fresh databases already carry
        the widened constraint and skip the rebuild. All rows and their ids
        are preserved, and the learner/question index is recreated below.
        """
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'attempts'"
        ).fetchone()
        if row is None or "mock_exam" in row["sql"]:
            return
        connection.execute(
            """
            CREATE TABLE attempts_mode_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learner_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('normal', 'review', 'mock_exam')),
                selected_answers TEXT NOT NULL,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                answered_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO attempts_mode_migration (
                id, learner_id, question_id, mode, selected_answers,
                is_correct, answered_at
            )
            SELECT id, learner_id, question_id, mode, selected_answers,
                   is_correct, answered_at
            FROM attempts
            """
        )
        connection.execute("DROP TABLE attempts")
        connection.execute(
            "ALTER TABLE attempts_mode_migration RENAME TO attempts"
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and commit or roll back automatically."""
        active = self._transaction_connection.get()
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize progress changes across workers and share all learner writes."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = self._transaction_connection.set(connection)
            try:
                yield connection
            finally:
                self._transaction_connection.reset(token)
