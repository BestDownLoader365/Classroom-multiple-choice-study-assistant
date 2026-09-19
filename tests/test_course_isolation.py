"""Course isolation: the mandatory namespace-separation assertions.

Every assertion here is a *behavioural* guarantee of the multi-course refactor:
two courses that reuse the same local question/chapter/source IDs must never
observe, clear, or advance each other's state.
"""

import copy

import pytest

from app.models import Attempt, ExamSession, ExamStatus, QuizMode
from tests.conftest import two_course_app

A = "course_a"
B = "course_b"


@pytest.fixture
def app(tmp_path):
    return two_course_app(tmp_path)


def record(services, learner, question_id, correct, mode=QuizMode.NORMAL):
    return services.wrong_question_service.record_attempt(
        learner_id=learner,
        question_id=question_id,
        mode=mode,
        selected_answers=("a",) if correct else ("b",),
        is_correct=correct,
    )


def add_attempt(services, learner, question_id, day):
    services.attempt_repository.add(
        Attempt(
            learner_id=learner,
            question_id=question_id,
            mode=QuizMode.NORMAL,
            selected_answers=("a",),
            is_correct=True,
            answered_at=f"2026-01-{day:02d}T00:00:00+00:00",
        )
    )


def test_attempt_retention_window_is_per_course(app):
    """A/q001 attempt #11 does not delete B/q001 attempt history."""
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    for day in range(1, 12):
        add_attempt(a, "learner", "q001", day)
    record(b, "learner", "q001", True)

    assert a.attempt_repository.count() == 10
    assert b.attempt_repository.count() == 1
    assert len(b.attempt_repository.list_for_learner("learner")) == 1


def test_latest_incorrect_answers_never_cross_courses(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    record(b, "learner", "q001", False)

    assert b.attempt_repository.get_latest_incorrect_answers("learner") == {
        "q001": ("b",)
    }
    assert a.attempt_repository.get_latest_incorrect_answers("learner") == {}


def test_bulk_delete_stays_inside_one_course(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    for day in range(1, 13):
        add_attempt(a, "learner", "q001", day)
    record(b, "learner", "q001", True)

    removed = a.attempt_repository.delete_for_questions({"q001"})

    assert removed == 10
    assert a.attempt_repository.count() == 0
    assert b.attempt_repository.count() == 1


def test_resetting_mistakes_in_a_does_not_touch_b(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    record(a, "learner", "q001", False, mode=QuizMode.REVIEW)
    record(a, "learner", "q002", False)
    record(b, "learner", "q001", False, mode=QuizMode.REVIEW)
    a.progress_repository.save(
        "learner", QuizMode.NORMAL, a.bank_version, {"mode": "normal"}
    )
    b.progress_repository.save(
        "learner", QuizMode.NORMAL, b.bank_version, {"mode": "normal"}
    )

    deleted = a.wrong_question_service.reset("learner")

    assert deleted >= 1
    assert a.wrong_question_repository.get_all("learner") == []
    assert a.weak_knowledge_point_repository.get_by_id("learner", "chapter_1") is None
    # B keeps its wrong state, SRS state, weak chapter and progress round.
    record_b = b.wrong_question_repository.get_by_id("learner", "q001")
    assert record_b is not None and record_b.corrected is False
    assert (
        b.weak_knowledge_point_repository.get_by_id("learner", "chapter_1") is not None
    )
    assert b.progress_repository.get("learner", QuizMode.NORMAL) is not None


def test_completing_a_chapter_does_not_complete_b_chapter(app):
    """``chapter_1`` is course-local, so A's progress never completes B's."""
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    record(a, "learner", "q001", False)
    record(b, "learner", "q001", False)

    a.weak_knowledge_point_repository.save_verification(
        "learner", "chapter_1", ("q001", "q002"), False, "2026-03-01T00:00:00+00:00"
    )

    a_point = a.weak_knowledge_point_repository.get_by_id("learner", "chapter_1")
    b_point = b.weak_knowledge_point_repository.get_by_id("learner", "chapter_1")
    assert a_point.active is False and len(a_point.verified_question_ids) == 2
    assert b_point.active is True and b_point.verified_question_ids == ()


def test_progress_rounds_keep_independent_seed_token_queue_and_feedback(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    state_a = {
        "mode": "normal",
        "question_ids": ["q001", "q002"],
        "question_results": [None, None],
        "current_index": 1,
        "correct_count": 1,
        "incorrect_count": 0,
        "status": "answered",
        "answer_token": "token-a",
        "option_seed": "seed-a",
        "requested_size": "all",
        "initial_question_count": 2,
        "chapter_ids": [],
        "source_ids": [],
        "feedback": {"is_correct": True},
    }
    state_b = copy.deepcopy(state_a)
    state_b.update(
        {"current_index": 0, "answer_token": "token-b", "option_seed": "seed-b"}
    )
    state_b.pop("feedback")
    state_b["status"] = "pending"
    a.progress_repository.save("learner", QuizMode.NORMAL, "bank-a", state_a)
    b.progress_repository.save("learner", QuizMode.NORMAL, "bank-b", state_b)

    assert a.progress_repository.get("learner", QuizMode.NORMAL)[1] == state_a
    assert b.progress_repository.get("learner", QuizMode.NORMAL)[1] == state_b
    assert a.progress_repository.get("learner", QuizMode.REVIEW) is None
    assert b.progress_repository.get("learner", QuizMode.REVIEW) is None


def test_expired_a_exam_is_not_settled_by_using_b(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    # Build the session directly: the production create_exam path only allows
    # the configured exam sizes, which this two-question bank cannot serve.
    session = ExamSession(
        id="exam-a",
        learner_id="learner",
        status=ExamStatus.IN_PROGRESS,
        question_count=2,
        time_limit_seconds=None,
        option_seed="seed",
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:00+00:00",
        deadline_at="2020-01-01T00:00:00+00:00",
    )
    a.exam_repository.create(session, ("q001", "q002"))

    settled_in_b = b.exam_service.finalize_expired_for_learner("learner")

    assert settled_in_b == 0
    assert b.exam_repository.count() == 0
    assert (
        a.exam_repository.get_for_learner("learner", session.id).status.value
        == "in_progress"
    )


def test_dashboard_and_stats_exclude_the_other_course(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    record(a, "learner", "q001", True)
    record(a, "learner", "q002", False)
    record(b, "learner", "q001", True)

    dashboard_a = a.statistics_service.build_dashboard("learner")
    dashboard_b = b.statistics_service.build_dashboard("learner")
    overview_a = a.global_statistics_service.build_overview()

    assert dashboard_a.total_attempts == 2
    assert dashboard_b.total_attempts == 1
    assert overview_a.total_attempts == 2
    assert {attempt.question_id for attempt in a.attempt_repository.list_all()} == {
        "q001",
        "q002",
    }


def test_glossary_payloads_never_mix(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)

    ids_a = {term["id"] for term in a.glossary_repository.to_dict()["terms"]}
    ids_b = {term["id"] for term in b.glossary_repository.to_dict()["terms"]}

    assert ids_a == {"a-term-1", "a-term-2"}
    assert ids_b == {"b-term-1", "b-term-2"}
    assert ids_a.isdisjoint(ids_b)


def test_registry_bootstrap_does_not_collect_the_other_course_ids(app, tmp_path):
    """A registry bootstrap must only ever adopt A's own historical IDs."""
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    # B has history for an ID that A never had.
    record(b, "learner", "q002", False)
    b.question_registry_repository.retire("q002", retired_at="2026-01-01T00:00:00+00:00")
    a.attempt_repository.delete_for_questions({"q002"})

    # Force A's registry to bootstrap again.
    with a.question_registry_repository.database.connect() as connection:
        connection.execute(
            "DELETE FROM question_registry WHERE course_id = ?", (A,)
        )
    a.question_bank_sync_service.synchronize(a.bank_version)

    registry_a = a.question_registry_repository.get_all()
    registry_b = b.question_registry_repository.get_all()
    assert set(registry_a) == {"q001", "q002"}
    assert all(
        entry.status.value == "active" for entry in registry_a.values()
    ), "A must not adopt B's retired tombstones"
    assert registry_b["q002"].status.value == "retired"


def test_thirty_attempts_in_a_cannot_prune_b(app):
    a = app.extensions["mcq_services"].course(A)
    b = app.extensions["mcq_services"].course(B)
    for day in range(1, 29):
        add_attempt(a, "learner", "q001", min(day, 28))
        add_attempt(a, "learner", "q002", min(day, 28))
    record(b, "learner", "q002", False)

    assert a.attempt_repository.count() == 20
    assert b.attempt_repository.count() == 1
    assert b.wrong_question_repository.get_by_id("learner", "q002") is not None

