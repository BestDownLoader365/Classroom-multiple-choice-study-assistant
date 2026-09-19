"""Namespace migration: field-level equivalence, idempotency, fault injection."""

import sqlite3

import pytest

from app.models import LEGACY_COURSE_ID
from app.repositories import Database
from app.repositories.schema_migrations import (
    LEGACY_COURSE_KEY,
    SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    STAGE_AFTER_COPY,
    STAGE_AFTER_CREATE,
    STAGE_BEFORE_COMMIT,
    STAGE_BEFORE_REPLACE,
    STAGE_BEFORE_VERSION,
    SchemaMigrationError,
    ensure_schema,
    read_meta,
)

LEGACY_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE quiz_progress (
    learner_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('normal', 'review')),
    bank_version TEXT NOT NULL,
    state TEXT,
    PRIMARY KEY (learner_id, mode)
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('normal', 'review', 'mock_exam')),
    selected_answers TEXT NOT NULL,
    is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    answered_at TEXT NOT NULL
);
CREATE TABLE wrong_questions (
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
CREATE TABLE weak_knowledge_points (
    learner_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    verified_question_ids TEXT NOT NULL DEFAULT '[]',
    last_wrong_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (learner_id, chapter_id)
);
CREATE TABLE auth_rate_limits (
    scope TEXT NOT NULL,
    identifier_hash TEXT NOT NULL,
    window_started_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    PRIMARY KEY (scope, identifier_hash)
);
CREATE TABLE exam_sessions (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'submitted', 'expired')),
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
CREATE TABLE exam_questions (
    exam_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    selected_answers TEXT,
    is_correct INTEGER CHECK (is_correct IN (0, 1)),
    answered_at TEXT,
    grading_fingerprint TEXT,
    PRIMARY KEY (exam_id, position),
    UNIQUE (exam_id, question_id)
);
CREATE TABLE question_bank_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bank_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    catalogue_fingerprint TEXT
);
CREATE TABLE question_registry (
    question_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    question_type TEXT NOT NULL,
    option_ids TEXT NOT NULL,
    correct_answers TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    placement_fingerprint TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    retired_at TEXT
);
"""

LEGACY_ROWS = """
INSERT INTO users VALUES ('u1', 'learner', 'hash', '2026-01-01T00:00:00+00:00');
INSERT INTO quiz_progress VALUES
    ('u1', 'normal', 'bank-v1', '{"mode":"normal","current_index":3}'),
    ('u1', 'review', 'bank-v1', NULL);
INSERT INTO attempts (learner_id, question_id, mode, selected_answers, is_correct, answered_at)
VALUES ('u1', 'q001', 'normal', '["a"]', 0, '2026-01-02T00:00:00+00:00'),
       ('u1', 'q001', 'mock_exam', '["b"]', 1, '2026-01-03T00:00:00+00:00'),
       ('u2', 'q002', 'review', '["y"]', 1, '2026-01-04T00:00:00+00:00');
INSERT INTO wrong_questions VALUES
    ('u1', 'q001', 4, 1, 1, 2, '2026-02-01T00:00:00+00:00',
     '2026-01-05T00:00:00+00:00', '2026-01-06T00:00:00+00:00');
INSERT INTO weak_knowledge_points VALUES
    ('u1', 'chapter_1', 0, '["q001","q002"]',
     '2026-01-05T00:00:00+00:00', '2026-01-06T00:00:00+00:00');
INSERT INTO auth_rate_limits VALUES ('login-ip', 'abc', 1, 2, 3);
INSERT INTO exam_sessions VALUES
    ('exam-1', 'u1', 'submitted', 2, 600, 'seed-1',
     '2026-01-07T00:00:00+00:00', '2026-01-07T00:00:00+00:00',
     '2026-01-07T00:10:00+00:00', '2026-01-07T00:09:00+00:00', 1, 1, 540);
INSERT INTO exam_questions VALUES
    ('exam-1', 0, 'q001', '["a"]', 0, '2026-01-07T00:01:00+00:00', 'fp-grading-1'),
    ('exam-1', 1, 'q002', '["y"]', 1, '2026-01-07T00:02:00+00:00', NULL);
INSERT INTO question_bank_state VALUES (1, 'bank-v1', 7, 'catalogue-v1');
INSERT INTO question_registry VALUES
    ('q001', 'active', 'single', '["a","b"]', '["a"]', 'content-1',
     'placement-1', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00', NULL),
    ('q002', 'retired', 'multiple', '["x","y"]', '["y"]', 'content-2',
     'placement-2', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00',
     '2026-01-03T00:00:00+00:00'),
    ('ghost', 'retired', '', '[]', '[]', '', NULL,
     '2026-01-04T00:00:00+00:00', '2026-01-04T00:00:00+00:00',
     '2026-01-04T00:00:00+00:00');
"""


def build_legacy_database(path, rows: str = LEGACY_ROWS):
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.executescript(rows)
    connection.commit()
    connection.close()
    return path


def rows_of(path, table: str, columns: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return [
            tuple(row)
            for row in connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY 1, 2"
            )
        ]
    finally:
        connection.close()



def test_migration_is_field_level_equivalent(tmp_path):
    path = build_legacy_database(tmp_path / "legacy.db")
    before = {
        table: rows_of(path, table, "*")
        for table in (
            "users",
            "auth_rate_limits",
            "attempts",
            "exam_questions",
        )
    }

    info = ensure_schema(path)

    assert info.schema_version == SCHEMA_VERSION
    assert info.migrated is True
    # users and auth_rate_limits stay global and unchanged.
    assert rows_of(path, "users", "*") == before["users"]
    assert rows_of(path, "auth_rate_limits", "*") == before["auth_rate_limits"]

    scoped_attempts = rows_of(
        path,
        "attempts",
        "id, learner_id, course_id, question_id, mode, selected_answers, "
        "is_correct, answered_at",
    )
    assert [row[2] for row in scoped_attempts] == [LEGACY_COURSE_ID] * 3
    assert [row[:2] + row[3:] for row in scoped_attempts] == before["attempts"]

    assert rows_of(
        path,
        "wrong_questions",
        "learner_id, course_id, question_id, wrong_count, review_streak, mastered, "
        "srs_level, next_review_at, last_wrong_at, last_reviewed_at",
    ) == [
        (
            "u1",
            LEGACY_COURSE_ID,
            "q001",
            4,
            1,
            1,
            2,
            "2026-02-01T00:00:00+00:00",
            "2026-01-05T00:00:00+00:00",
            "2026-01-06T00:00:00+00:00",
        )
    ]
    assert rows_of(
        path,
        "weak_knowledge_points",
        "learner_id, course_id, chapter_id, active, verified_question_ids, "
        "last_wrong_at, updated_at",
    ) == [
        (
            "u1",
            LEGACY_COURSE_ID,
            "chapter_1",
            0,
            '["q001","q002"]',
            "2026-01-05T00:00:00+00:00",
            "2026-01-06T00:00:00+00:00",
        )
    ]
    assert rows_of(
        path, "quiz_progress", "learner_id, course_id, mode, bank_version, state"
    ) == [
        (
            "u1",
            LEGACY_COURSE_ID,
            "normal",
            "bank-v1",
            '{"mode":"normal","current_index":3}',
        ),
        # The NULL tombstone survives verbatim.
        ("u1", LEGACY_COURSE_ID, "review", "bank-v1", None),
    ]
    assert rows_of(
        path,
        "exam_sessions",
        "id, course_id, learner_id, status, question_count, time_limit_seconds, "
        "option_seed, created_at, started_at, deadline_at, submitted_at, "
        "current_position, correct_count, duration_seconds",
    ) == [
        (
            "exam-1",
            LEGACY_COURSE_ID,
            "u1",
            "submitted",
            2,
            600,
            "seed-1",
            "2026-01-07T00:00:00+00:00",
            "2026-01-07T00:00:00+00:00",
            "2026-01-07T00:10:00+00:00",
            "2026-01-07T00:09:00+00:00",
            1,
            1,
            540,
        )
    ]
    # Slots keep their grading fingerprints and store no course_id of their own.
    assert (
        rows_of(
            path,
            "exam_questions",
            "exam_id, position, question_id, selected_answers, is_correct, "
            "answered_at, grading_fingerprint",
        )
        == before["exam_questions"]
    )

    assert rows_of(
        path,
        "question_bank_state",
        "course_id, bank_version, generation, catalogue_fingerprint",
    ) == [(LEGACY_COURSE_ID, "bank-v1", 7, "catalogue-v1")]
    assert rows_of(
        path,
        "question_registry",
        "course_id, question_id, status, question_type, option_ids, "
        "correct_answers, content_fingerprint, placement_fingerprint, "
        "first_seen_at, last_seen_at, retired_at",
    ) == [
        (
            LEGACY_COURSE_ID,
            "ghost",
            "retired",
            "",
            "[]",
            "[]",
            "",
            None,
            "2026-01-04T00:00:00+00:00",
            "2026-01-04T00:00:00+00:00",
            "2026-01-04T00:00:00+00:00",
        ),
        (
            LEGACY_COURSE_ID,
            "q001",
            "active",
            "single",
            '["a","b"]',
            '["a"]',
            "content-1",
            "placement-1",
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            None,
        ),
        (
            LEGACY_COURSE_ID,
            "q002",
            "retired",
            "multiple",
            '["x","y"]',
            '["y"]',
            "content-2",
            "placement-2",
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        ),
    ]
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        assert read_meta(connection, LEGACY_COURSE_KEY) == LEGACY_COURSE_ID
        assert read_meta(connection, SCHEMA_VERSION_KEY) == str(SCHEMA_VERSION)
    finally:
        connection.close()


def test_migration_is_idempotent_and_rerunnable(tmp_path):
    path = build_legacy_database(tmp_path / "legacy.db")
    ensure_schema(path)
    first = {
        table: rows_of(path, table, "*")
        for table in (
            "attempts",
            "wrong_questions",
            "exam_sessions",
            "exam_questions",
            "quiz_progress",
            "question_registry",
            "question_bank_state",
        )
    }

    second_info = ensure_schema(path)
    Database(path).initialize()

    assert second_info.migrated is False
    assert {
        table: rows_of(path, table, "*") for table in first
    } == first


def test_migration_does_not_recalculate_fingerprints(tmp_path):
    """Namespace migration must never re-derive grading/content fingerprints."""
    path = build_legacy_database(tmp_path / "legacy.db")
    before = rows_of(
        path,
        "question_registry",
        "question_id, content_fingerprint, placement_fingerprint, option_ids, "
        "correct_answers",
    )

    ensure_schema(path)

    after = rows_of(
        path,
        "question_registry",
        "question_id, content_fingerprint, placement_fingerprint, option_ids, "
        "correct_answers",
    )
    assert after == before


def test_orphan_exam_slots_abort_the_migration_without_writing(tmp_path):
    path = build_legacy_database(
        tmp_path / "orphan.db",
        LEGACY_ROWS
        + "INSERT INTO exam_questions VALUES "
        "('missing-exam', 0, 'q001', NULL, NULL, NULL, NULL);",
    )
    before_schema = sqlite3.connect(path).execute(
        "SELECT sql FROM sqlite_master WHERE name = 'attempts'"
    ).fetchone()[0]

    with pytest.raises(SchemaMigrationError, match="missing exam"):
        ensure_schema(path)

    connection = sqlite3.connect(path)
    try:
        # Nothing was replaced and no version was written.
        assert (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'attempts'"
            ).fetchone()[0]
            == before_schema
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '%__course_migration'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'schema_meta'"
            ).fetchall()
            == []
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "stage",
    [
        STAGE_AFTER_CREATE,
        STAGE_AFTER_COPY,
        STAGE_BEFORE_REPLACE,
        STAGE_BEFORE_VERSION,
        STAGE_BEFORE_COMMIT,
    ],
)
def test_fault_injection_rolls_back_and_reruns_consistently(tmp_path, stage):
    """Every injected failure must leave the database re-migratable."""
    path = build_legacy_database(tmp_path / "fault.db")

    def failpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"injected failure at {current}")

    with pytest.raises(RuntimeError, match="injected failure"):
        ensure_schema(path, failpoint=failpoint)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        # The failed attempt left no half-migrated table behind, and the only
        # extra table is the schema version bookkeeping that a rollback undoes.
        assert not {name for name in tables if name.endswith("__course_migration")}
        assert read_meta(connection, SCHEMA_VERSION_KEY) is None
    finally:
        connection.close()

    # Re-running reaches the same, fully migrated state.
    info = ensure_schema(path)
    assert info.schema_version == SCHEMA_VERSION
    assert info.migrated is True
    assert rows_of(path, "attempts", "learner_id, course_id, question_id") == [
        ("u1", LEGACY_COURSE_ID, "q001"),
        ("u1", LEGACY_COURSE_ID, "q001"),
        ("u2", LEGACY_COURSE_ID, "q002"),
    ]
    assert rows_of(path, "question_bank_state", "course_id, generation") == [
        (LEGACY_COURSE_ID, 7)
    ]

