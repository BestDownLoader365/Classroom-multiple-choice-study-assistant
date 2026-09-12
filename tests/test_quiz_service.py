from dataclasses import replace
import logging

from app.models import Option, Question, QuizMode
from app.repositories import (
    AttemptRepository,
    Database,
    QuestionRepository,
    WrongQuestionRepository,
)
from app.services import GradingService, QuizService, WrongQuestionService

LEARNER_ID = "11111111-1111-4111-8111-111111111111"


def review_question_ids(quiz, learner_id, **filters):
    """Return the IDs a review round would serve, in order."""
    return [
        item.question_id
        for item in quiz.start_review(learner_id, **filters).items
    ]


def fairness_questions(count=6):
    return [
        Question(
            id=f"fair-{index}",
            text=f"Question {index}",
            question_type="single",
            options=(Option("yes", "Yes"), Option("no", "No")),
            correct_answers=("yes",),
            explanation="Yes.",
            source_id="source-a",
            chapter_ids=("chapter-a" if index < count - 1 else "chapter-b",),
        )
        for index in range(count)
    ]


def build_services(tmp_path, sample_questions, shuffler=lambda values: None):
    question_repository = QuestionRepository(sample_questions)
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    attempt_repository = AttemptRepository(database)
    wrong_repository = WrongQuestionRepository(database)
    wrong_service = WrongQuestionService(
        attempt_repository,
        wrong_repository,
        question_repository,
    )
    quiz_service = QuizService(
        question_repository,
        GradingService(),
        wrong_service,
        shuffler=shuffler,
    )
    return quiz_service, wrong_service, wrong_repository


def test_normal_mode_contains_all_questions(tmp_path, sample_questions):
    quiz, _, _ = build_services(tmp_path, sample_questions)

    assert list(quiz.start_normal().question_ids) == ["q1", "q2", "q3"]


def test_normal_mode_shuffles_question_ids(tmp_path, sample_questions):
    calls = []

    def reverse(values):
        calls.append(list(values))
        values.reverse()

    quiz, _, _ = build_services(tmp_path, sample_questions, reverse)

    assert list(quiz.start_normal().question_ids) == ["q3", "q2", "q1"]
    assert calls == [["q1", "q2", "q3"]]


def test_normal_mode_honors_question_limit(tmp_path, sample_questions):
    quiz, _, _ = build_services(tmp_path, sample_questions)

    assert list(quiz.start_normal(2).question_ids) == ["q1", "q2"]


def test_normal_fairness_covers_cycle_before_repeating(tmp_path):
    quiz, _, _ = build_services(tmp_path, fairness_questions())
    scope = None
    remaining = None
    rounds = []

    for _ in range(3):
        result = quiz.start_normal(
            2,
            fairness_scope=scope,
            fairness_remaining_ids=remaining,
        )
        rounds.append(set(result.question_ids))
        scope = result.fairness_scope
        remaining = list(result.fairness_remaining_ids)

    assert set.union(*rounds) == {f"fair-{index}" for index in range(6)}
    assert sum(len(round_ids) for round_ids in rounds) == 6


def test_normal_fairness_cycle_boundary_has_no_round_duplicate(tmp_path):
    quiz, _, _ = build_services(
        tmp_path, fairness_questions(5), shuffler=lambda values: None
    )
    first = quiz.start_normal(3)
    second = quiz.start_normal(
        3,
        fairness_scope=first.fairness_scope,
        fairness_remaining_ids=list(first.fairness_remaining_ids),
    )

    assert len(second.question_ids) == 3
    assert len(set(second.question_ids)) == 3
    assert set(first.question_ids).intersection(second.question_ids) == {
        second.question_ids[-1]
    }


def test_normal_fairness_scope_change_rebuilds_bag(tmp_path):
    quiz, _, _ = build_services(tmp_path, fairness_questions())
    first = quiz.start_normal(2, chapter_ids={"chapter-a"})
    changed = quiz.start_normal(
        2,
        chapter_ids={"chapter-b"},
        fairness_scope=first.fairness_scope,
        fairness_remaining_ids=list(first.fairness_remaining_ids),
    )

    assert changed.question_ids == ("fair-5",)
    assert changed.fairness_scope != first.fairness_scope


def test_normal_all_returns_every_eligible_question_once(tmp_path):
    quiz, _, _ = build_services(tmp_path, fairness_questions())

    result = quiz.start_normal(None, chapter_ids={"chapter-a"})

    assert len(result.question_ids) == 5
    assert len(set(result.question_ids)) == 5
    assert result.fairness_remaining_ids == ()


def test_normal_fairness_ignores_stale_and_duplicate_remaining_ids(tmp_path):
    quiz, _, _ = build_services(
        tmp_path, fairness_questions(5), shuffler=lambda values: None
    )
    first = quiz.start_normal(2)

    result = quiz.start_normal(
        3,
        fairness_scope=first.fairness_scope,
        fairness_remaining_ids=["missing", "fair-2", "fair-2", "fair-3"],
    )

    assert result.question_ids[:2] == ("fair-2", "fair-3")
    assert len(result.question_ids) == len(set(result.question_ids)) == 3


def test_normal_mode_filters_by_chapter_before_applying_limit(
    tmp_path, sample_questions
):
    quiz, _, _ = build_services(tmp_path, sample_questions)

    assert list(
        quiz.start_normal(10, chapter_ids={"chapter-b"}).question_ids
    ) == ["q2"]


def test_question_is_included_when_any_of_its_chapters_is_selected(
    tmp_path, sample_questions
):
    multi_chapter_question = replace(
        sample_questions[0], chapter_ids=("chapter-a", "chapter-b")
    )
    quiz, _, _ = build_services(
        tmp_path, [multi_chapter_question, *sample_questions[1:]]
    )

    assert list(
        quiz.start_normal(chapter_ids={"chapter-b"}).question_ids
    ) == ["q1", "q2"]


def test_all_chapters_remains_the_unfiltered_question_bank(
    tmp_path, sample_questions
):
    quiz, _, _ = build_services(tmp_path, sample_questions)

    assert list(quiz.start_normal().question_ids) == ["q1", "q2", "q3"]


def test_option_order_is_stable_for_one_occurrence_and_changes_across_appearances(
    tmp_path, sample_questions
):
    quiz, _, _ = build_services(tmp_path, sample_questions)
    question = sample_questions[1]

    first = [option.id for option in quiz.order_options(question, "round-one", 0)]
    same_occurrence = [
        option.id for option in quiz.order_options(question, "round-one", 0)
    ]
    observed = {
        tuple(option.id for option in quiz.order_options(question, "round-one", index))
        for index in range(10)
    }

    assert first == same_occurrence
    assert len(observed) > 1


def test_review_contains_only_uncorrected_wrong_questions(tmp_path, sample_questions):
    quiz, wrong_service, repository = build_services(tmp_path, sample_questions)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False)
    wrong_service.record_attempt(LEARNER_ID, "q2", QuizMode.NORMAL, ("b",), False)
    wrong_service.record_attempt(
        LEARNER_ID, "q2", QuizMode.REVIEW, ("a", "c"), True
    )
    wrong_service.record_attempt(
        LEARNER_ID, "q2", QuizMode.REVIEW, ("a", "c"), True
    )
    assert repository.get_by_id(LEARNER_ID, "q2").corrected is True

    assert review_question_ids(quiz, LEARNER_ID) == ["q1"]


def test_review_mode_shuffles_question_ids(tmp_path, sample_questions):
    def reverse(values):
        values.reverse()

    quiz, wrong_service, _ = build_services(tmp_path, sample_questions, reverse)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False)
    wrong_service.record_attempt(
        LEARNER_ID, "q3", QuizMode.NORMAL, ("no",), False
    )

    assert review_question_ids(quiz, LEARNER_ID) == ["q1", "q3"]


def test_review_mode_can_continue_within_one_chapter(tmp_path, sample_questions):
    quiz, wrong_service, _ = build_services(tmp_path, sample_questions)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False)
    wrong_service.record_attempt(LEARNER_ID, "q2", QuizMode.NORMAL, ("b",), False)

    assert review_question_ids(quiz, LEARNER_ID, chapter_ids={"chapter-b"}) == [
        "q2"
    ]


def test_stale_question_id_is_skipped_with_warning(
    tmp_path, sample_questions, caplog
):
    quiz, _, repository = build_services(tmp_path, sample_questions)
    repository.record_wrong(
        LEARNER_ID, "missing", "2026-01-01T00:00:00+00:00", False
    )
    repository.record_wrong(
        LEARNER_ID, "q1", "2026-01-02T00:00:00+00:00", False
    )

    with caplog.at_level(logging.WARNING):
        ids = review_question_ids(quiz, LEARNER_ID)

    assert ids == ["q1"]
    assert 'Wrong question "missing" no longer exists' in caplog.text
