from dataclasses import replace

import pytest

from app.models import LEGACY_COURSE_ID
from app.models import QuizMode
from app.repositories import (
    AttemptRepository,
    Database,
    QuestionRepository,
    WrongQuestionRepository,
)
from app.services import WrongQuestionService

LEARNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_LEARNER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def wrong_context(tmp_path, sample_questions):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    attempts = AttemptRepository(database, course_id=LEGACY_COURSE_ID)
    wrong_questions = WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID)
    service = WrongQuestionService(
        attempt_repository=attempts,
        wrong_question_repository=wrong_questions,
        question_repository=QuestionRepository(sample_questions),
    )
    return service, wrong_questions, attempts


def record(service, *, correct, mode=QuizMode.NORMAL, question_id="q1"):
    service.record_attempt(
        learner_id=LEARNER_ID,
        question_id=question_id,
        mode=mode,
        selected_answers=("2",) if correct else ("1",),
        is_correct=correct,
    )


def test_first_wrong_answer_creates_record(wrong_context):
    service, repository, attempts = wrong_context

    record(service, correct=False)

    item = repository.get_by_id(LEARNER_ID, "q1")
    assert item is not None
    assert (item.wrong_count, item.review_streak, item.corrected) == (1, 0, False)
    point = service.weak_knowledge_point_service.repository.get_by_id(
        LEARNER_ID, "chapter-a"
    )
    assert point.active is True
    assert point.verified_question_ids == ()
    assert attempts.count() == 1


def test_repeated_wrong_answer_increments_count(wrong_context):
    service, repository, _ = wrong_context
    record(service, correct=False)
    record(service, correct=False)

    assert repository.get_by_id(LEARNER_ID, "q1").wrong_count == 2


def test_one_review_correct_marks_original_corrected(wrong_context):
    service, repository, _ = wrong_context
    record(service, correct=False)
    record(service, correct=True, mode=QuizMode.REVIEW)

    item = repository.get_by_id(LEARNER_ID, "q1")
    assert (item.review_streak, item.corrected) == (1, True)
    point = service.weak_knowledge_point_service.repository.get_by_id(
        LEARNER_ID, "chapter-a"
    )
    assert point.verified_question_ids == ("q1",)
    assert point.active is True


def test_normal_correct_does_not_advance_knowledge_verification(wrong_context):
    service, repository, attempts = wrong_context
    record(service, correct=False)
    record(service, correct=True, mode=QuizMode.REVIEW)

    record(service, correct=True, mode=QuizMode.NORMAL)

    item = repository.get_by_id(LEARNER_ID, "q1")
    assert (item.review_streak, item.corrected) == (1, True)
    point = service.weak_knowledge_point_service.repository.get_by_id(
        LEARNER_ID, "chapter-a"
    )
    assert point.verified_question_ids == ("q1",)
    assert attempts.count() == 3


def test_repeated_review_correct_does_not_duplicate_verification(wrong_context):
    service, repository, _ = wrong_context
    record(service, correct=False)
    record(service, correct=True, mode=QuizMode.REVIEW)
    record(service, correct=True, mode=QuizMode.REVIEW)

    item = repository.get_by_id(LEARNER_ID, "q1")
    assert (item.review_streak, item.corrected) == (1, True)
    point = service.weak_knowledge_point_service.repository.get_by_id(
        LEARNER_ID, "chapter-a"
    )
    assert point.verified_question_ids == ("q1",)
    assert point.active is True


def test_review_wrong_reopens_question_and_resets_knowledge(wrong_context):
    service, repository, _ = wrong_context
    record(service, correct=False)
    record(service, correct=True, mode=QuizMode.REVIEW)
    record(service, correct=False, mode=QuizMode.REVIEW)

    item = repository.get_by_id(LEARNER_ID, "q1")
    assert (item.wrong_count, item.review_streak, item.corrected) == (2, 0, False)
    point = service.weak_knowledge_point_service.repository.get_by_id(
        LEARNER_ID, "chapter-a"
    )
    assert point.verified_question_ids == ()
    assert point.active is True


def test_normal_wrong_reopens_corrected_question(wrong_context):
    service, repository, _ = wrong_context
    record(service, correct=False)
    record(service, correct=True, mode=QuizMode.REVIEW)
    assert repository.get_by_id(LEARNER_ID, "q1").corrected is True

    record(service, correct=False, mode=QuizMode.NORMAL)

    item = repository.get_by_id(LEARNER_ID, "q1")
    assert (item.wrong_count, item.review_streak, item.corrected) == (2, 0, False)


def test_same_question_has_independent_state_per_learner(wrong_context):
    service, repository, _ = wrong_context
    record(service, correct=False)
    record(service, correct=False)
    service.record_attempt(
        learner_id=OTHER_LEARNER_ID,
        question_id="q1",
        mode=QuizMode.NORMAL,
        selected_answers=("1",),
        is_correct=False,
    )

    first = repository.get_by_id(LEARNER_ID, "q1")
    second = repository.get_by_id(OTHER_LEARNER_ID, "q1")
    assert first.wrong_count == 2
    assert second.wrong_count == 1


def test_mistake_items_preserve_latest_wrong_answer_and_chapter_filter(
    wrong_context,
):
    service, _, _ = wrong_context
    record(service, correct=False, question_id="q1")
    service.record_attempt(
        learner_id=LEARNER_ID,
        question_id="q2",
        mode=QuizMode.NORMAL,
        selected_answers=("b",),
        is_correct=False,
    )

    items = service.get_items(LEARNER_ID, chapter_ids={"chapter-b"})

    assert [item.question.id for item in items] == ["q2"]
    assert items[0].question.source_id == "source-a"
    assert items[0].question.chapter_ids == ("chapter-b",)
    assert items[0].selected_answers == ("b",)


def test_mistake_filter_matches_any_question_chapter(tmp_path, sample_questions):
    sample_questions[0] = replace(
        sample_questions[0], chapter_ids=("chapter-a", "chapter-b")
    )
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    service = WrongQuestionService(
        AttemptRepository(database, course_id=LEGACY_COURSE_ID),
        WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID),
        QuestionRepository(sample_questions),
    )
    record(service, correct=False, question_id="q1")

    items = service.get_items(LEARNER_ID, chapter_ids={"chapter-b"})

    assert [item.question.id for item in items] == ["q1"]


def test_reset_clears_mistake_state_but_preserves_attempt_history(wrong_context):
    service, repository, attempts = wrong_context
    record(service, correct=False, question_id="q1")
    record(service, correct=False, question_id="q2")

    deleted_count = service.reset(LEARNER_ID)

    assert deleted_count == 2
    assert repository.get_all(LEARNER_ID) == []
    assert service.weak_knowledge_point_service.repository.get_all(LEARNER_ID) == []
    assert attempts.count() == 2
