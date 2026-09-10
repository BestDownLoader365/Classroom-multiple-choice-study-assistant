import json

import pytest

from app.models import Attempt, QuizMode
from app.repositories import (
    AttemptRepository,
    Database,
    UserRepository,
    UsernameAlreadyExistsError,
    WeakKnowledgePointRepository,
    WrongQuestionRepository,
)


def test_database_connection_rolls_back_failed_transaction(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()

    with pytest.raises(RuntimeError, match="stop transaction"):
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, username, password_hash, created_at)
                VALUES ('user-id', 'learner', 'hash', '2026-01-01T00:00:00+00:00')
                """
            )
            raise RuntimeError("stop transaction")

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


def test_users_are_hashed_and_case_insensitive(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    users = UserRepository(database)

    created = users.create("Learner", "secret1")

    assert created.password_hash != "secret1"
    assert users.authenticate("LEARNER", "secret1") == created
    assert users.authenticate("learner", "wrong-password") is None
    with pytest.raises(UsernameAlreadyExistsError):
        users.create("learner", "another-secret")


def test_attempt_repository_preserves_multiple_answer_order(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    attempts = AttemptRepository(database)
    attempt = Attempt(
        learner_id="learner-id",
        question_id="q2",
        mode=QuizMode.REVIEW,
        selected_answers=("c", "a"),
        is_correct=True,
        answered_at="2026-01-01T00:00:00+00:00",
    )

    attempt_id = attempts.add(attempt)

    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    assert row["learner_id"] == "learner-id"
    assert row["question_id"] == "q2"
    assert row["mode"] == "review"
    assert json.loads(row["selected_answers"]) == ["c", "a"]
    assert row["is_correct"] == 1


def test_attempt_repository_returns_latest_incorrect_answer_per_question(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    attempts = AttemptRepository(database)
    for selected, answered_at in [
        (("a",), "2026-01-01T00:00:00+00:00"),
        (("b", "c"), "2026-01-02T00:00:00+00:00"),
    ]:
        attempts.add(
            Attempt(
                learner_id="learner-id",
                question_id="q2",
                mode=QuizMode.NORMAL,
                selected_answers=selected,
                is_correct=False,
                answered_at=answered_at,
            )
        )

    assert attempts.get_latest_incorrect_answers("learner-id") == {
        "q2": ("b", "c")
    }


def test_attempt_repository_keeps_only_latest_three_per_learner_and_question(
    tmp_path,
):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    attempts = AttemptRepository(database)

    for index in range(5):
        attempts.add(
            Attempt(
                learner_id="learner-id",
                question_id="q1",
                mode=QuizMode.NORMAL,
                selected_answers=(str(index),),
                is_correct=False,
                answered_at=f"2026-01-0{index + 1}T00:00:00+00:00",
            )
        )
    for learner_id, question_id in [
        ("learner-id", "q2"),
        ("other-id", "q1"),
    ]:
        attempts.add(
            Attempt(
                learner_id=learner_id,
                question_id=question_id,
                mode=QuizMode.NORMAL,
                selected_answers=("separate",),
                is_correct=False,
                answered_at="2026-01-06T00:00:00+00:00",
            )
        )

    with database.connect() as connection:
        retained = connection.execute(
            """
            SELECT selected_answers
            FROM attempts
            WHERE learner_id = 'learner-id' AND question_id = 'q1'
            ORDER BY answered_at, id
            """
        ).fetchall()

    assert [json.loads(row["selected_answers"]) for row in retained] == [
        ["2"],
        ["3"],
        ["4"],
    ]
    assert attempts.count() == 5


def test_database_startup_prunes_existing_attempts_to_three(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    with database.connect() as connection:
        for index in range(5):
            connection.execute(
                """
                INSERT INTO attempts (
                    learner_id, question_id, mode, selected_answers,
                    is_correct, answered_at
                ) VALUES ('learner-id', 'q1', 'normal', '[]', 0, ?)
                """,
                (f"2026-01-0{index + 1}T00:00:00+00:00",),
            )

    database.initialize()

    assert AttemptRepository(database).count() == 3


def test_wrong_question_reset_deletes_only_requested_learners_rows(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    wrong_questions = WrongQuestionRepository(database)
    wrong_questions.record_wrong("learner-id", "q1", "2026-01-01", False)
    wrong_questions.record_wrong("learner-id", "q2", "2026-01-02", False)
    wrong_questions.record_wrong("other-id", "q1", "2026-01-03", False)

    deleted_count = wrong_questions.delete_all_for_learner("learner-id")

    assert deleted_count == 2
    assert wrong_questions.get_all("learner-id") == []
    assert len(wrong_questions.get_all("other-id")) == 1


def test_weak_knowledge_points_store_distinct_json_state_per_learner(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    weak_points = WeakKnowledgePointRepository(database)
    weak_points.activate_and_reset(
        "learner-id", "chapter-a", "2026-01-01T00:00:00+00:00"
    )
    weak_points.save_verification(
        "learner-id",
        "chapter-a",
        ("q1", "q2"),
        False,
        "2026-01-02T00:00:00+00:00",
    )
    weak_points.activate_and_reset(
        "other-id", "chapter-a", "2026-01-03T00:00:00+00:00"
    )

    learner = weak_points.get_by_id("learner-id", "chapter-a")
    other = weak_points.get_by_id("other-id", "chapter-a")

    assert learner.active is False
    assert learner.verified_question_ids == ("q1", "q2")
    assert other.active is True
    assert other.verified_question_ids == ()


def test_weak_point_malformed_json_is_read_as_empty_progress(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO weak_knowledge_points (
                learner_id, chapter_id, active, verified_question_ids,
                last_wrong_at, updated_at
            ) VALUES ('learner-id', 'chapter-a', 1, '{bad', 'now', 'now')
            """
        )

    point = WeakKnowledgePointRepository(database).get_by_id(
        "learner-id", "chapter-a"
    )

    assert point.verified_question_ids == ()
