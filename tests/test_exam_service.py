"""Service-level tests for the mock-exam lifecycle."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Chapter,
    ExamStatus,
    Option,
    Question,
    QuizMode,
    SourceDocument,
)
from app.repositories import (
    AttemptRepository,
    Database,
    ExamRepository,
    QuestionRepository,
    WrongQuestionRepository,
)
from app.services import (
    AnswerValidationError,
    ExamConfigError,
    ExamExpiredError,
    ExamNotFoundError,
    ExamService,
    ExamStateError,
    GradingService,
    WrongQuestionService,
)

LEARNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_LEARNER_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_questions(count: int) -> list[Question]:
    """Build ``count`` single-choice questions spread over two chapters."""
    questions = []
    for index in range(count):
        questions.append(
            Question(
                id=f"q{index}",
                text=f"Question {index}",
                question_type="single",
                options=(Option("a", "Alpha"), Option("b", "Beta")),
                correct_answers=("b",),
                explanation=f"Explanation {index}",
                source_id="source-a",
                chapter_ids=("chapter-a" if index % 2 == 0 else "chapter-b",),
            )
        )
    return questions


@pytest.fixture
def exam_context(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    question_repository = QuestionRepository(
        make_questions(12),
        sources=(
            SourceDocument(id="source-a", title="Source A", lecture="Lecture 1"),
        ),
        chapters=(
            Chapter(id="chapter-a", source_id="source-a", title="Chapter A", order=1),
            Chapter(id="chapter-b", source_id="source-a", title="Chapter B", order=2),
        ),
    )
    exam_repository = ExamRepository(database)
    attempts = AttemptRepository(database)
    wrong_questions = WrongQuestionRepository(database)
    wrong_question_service = WrongQuestionService(
        attempt_repository=attempts,
        wrong_question_repository=wrong_questions,
        question_repository=question_repository,
    )
    service = ExamService(
        exam_repository=exam_repository,
        question_repository=question_repository,
        grading_service=GradingService(),
        wrong_question_service=wrong_question_service,
        sampler=lambda ids, count: ids[:count],
    )
    return service, exam_repository, attempts, wrong_questions


def create(service, count=10, limit=None, now=NOW):
    return service.create_exam(
        LEARNER_ID, question_count=count, time_limit_seconds=limit, now=now
    )


def test_create_fixes_a_unique_question_set(exam_context):
    service, repository, _, _ = exam_context

    session = create(service, count=10)

    slots = service.get_questions(session)
    assert len(slots) == 10
    assert len({slot.question_id for slot in slots}) == 10
    assert [slot.position for slot in slots] == list(range(10))
    # Reading again (a refresh) returns the same fixed set in the same order.
    again = service.get_questions(service.get_session(LEARNER_ID, session.id))
    assert [slot.question_id for slot in again] == [
        slot.question_id for slot in slots
    ]
    assert repository.count() == 1


def test_create_rejects_unsupported_or_oversized_configuration(exam_context):
    service, _, _, _ = exam_context

    with pytest.raises(ExamConfigError):
        create(service, count=15)
    with pytest.raises(ExamConfigError):
        create(service, count=50)  # the test bank only has 12 questions
    with pytest.raises(ExamConfigError):
        create(service, count=10, limit=45)
    assert service.question_count_options(12) == (10,)
    assert service.question_count_options(225) == (10, 20, 30, 50)


def test_create_accepts_ninety_minutes_and_rejects_beyond(exam_context):
    service, _, _, _ = exam_context

    session = create(service, count=10, limit=5400)

    assert session.time_limit_seconds == 5400
    with pytest.raises(ExamConfigError):
        create(service, count=10, limit=6000)


def test_answers_persist_and_can_be_changed_or_cleared(exam_context):
    service, _, _, _ = exam_context
    session = create(service, count=10)

    service.save_answer(LEARNER_ID, session.id, 0, ["a"], now=NOW)
    service.save_answer(LEARNER_ID, session.id, 0, ["b"], now=NOW)
    slots = service.get_questions(service.get_session(LEARNER_ID, session.id))

    assert slots[0].selected_answers == ("b",)
    assert slots[0].answered_at is not None
    # A cleared selection removes the saved answer again.
    service.save_answer(LEARNER_ID, session.id, 0, [], now=NOW)
    cleared = service.get_questions(session)[0]
    assert cleared.selected_answers == ()
    assert cleared.answered_at is None
    # Saving tracks the last visited position for resume.
    assert service.get_session(LEARNER_ID, session.id).current_position == 0


def test_save_answer_validates_position_and_options(exam_context):
    service, _, _, _ = exam_context
    session = create(service, count=10)

    with pytest.raises(ExamConfigError):
        service.save_answer(LEARNER_ID, session.id, 10, ["a"], now=NOW)
    with pytest.raises(AnswerValidationError):
        service.save_answer(LEARNER_ID, session.id, 0, ["nope"], now=NOW)


def test_ownership_is_enforced(exam_context):
    service, _, _, _ = exam_context
    session = create(service, count=10)

    with pytest.raises(ExamNotFoundError):
        service.get_session(OTHER_LEARNER_ID, session.id)
    with pytest.raises(ExamNotFoundError):
        service.save_answer(OTHER_LEARNER_ID, session.id, 0, ["a"], now=NOW)
    with pytest.raises(ExamNotFoundError):
        service.submit(OTHER_LEARNER_ID, session.id, now=NOW)
    assert service.get_active_session(OTHER_LEARNER_ID) is None
    assert service.list_sessions(OTHER_LEARNER_ID) == []


def test_submit_grades_and_records_attempts_once(exam_context):
    service, _, attempts, _ = exam_context
    session = create(service, count=10)  # questions q0..q9 (sampler slices)
    # Answer q0..q3: even indexes are correct ("b"), odd are wrong ("a").
    for position in range(4):
        answer = ["b"] if position % 2 == 0 else ["a"]
        service.save_answer(LEARNER_ID, session.id, position, answer, now=NOW)

    submitted = service.submit(LEARNER_ID, session.id, now=NOW + timedelta(minutes=5))

    assert submitted.status is ExamStatus.SUBMITTED
    assert submitted.correct_count == 2
    assert submitted.duration_seconds == 300
    assert submitted.submitted_at == (NOW + timedelta(minutes=5)).isoformat()
    # Only the four answered questions became attempts, each exactly once.
    assert attempts.count() == 4
    # Re-submitting changes nothing.
    again = service.submit(
        LEARNER_ID, session.id, now=NOW + timedelta(minutes=6)
    )
    assert again.status is ExamStatus.SUBMITTED
    assert again.submitted_at == submitted.submitted_at
    assert attempts.count() == 4
    # Answers are locked after submission.
    with pytest.raises(ExamStateError):
        service.save_answer(LEARNER_ID, session.id, 5, ["b"], now=NOW)
    assert service.get_active_session(LEARNER_ID) is None


def test_submit_syncs_wrong_answers_into_existing_mistake_flow(exam_context):
    service, _, _, wrong_questions = exam_context
    session = create(service, count=10)
    service.save_answer(LEARNER_ID, session.id, 1, ["a"], now=NOW)  # q1 wrong

    service.submit(LEARNER_ID, session.id, now=NOW)

    record = wrong_questions.get_by_id(LEARNER_ID, "q1")
    assert record is not None
    assert (record.wrong_count, record.corrected) == (1, False)
    point = service.wrong_question_service.weak_knowledge_point_service
    assert point.repository.get_by_id(LEARNER_ID, "chapter-b") is not None

    # Answering the same question wrong again in another exam reopens the
    # existing record instead of duplicating it.
    second = create(service, count=10)
    service.save_answer(LEARNER_ID, second.id, 1, ["a"], now=NOW)
    service.submit(LEARNER_ID, second.id, now=NOW)
    record = wrong_questions.get_by_id(LEARNER_ID, "q1")
    assert record.wrong_count == 2
    assert len(wrong_questions.get_all(LEARNER_ID)) == 1


def test_exam_wrong_reopens_corrected_question_and_resets_srs(exam_context):
    service, _, _, wrong_questions = exam_context
    learning = service.wrong_question_service
    # Normal wrong -> review correct -> corrected with an SRS schedule.
    learning.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("a",), False, now=NOW)
    learning.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("b",), True, now=NOW)
    scheduled = wrong_questions.get_by_id(LEARNER_ID, "q1")
    assert scheduled.corrected and scheduled.next_review_at is not None

    session = create(service, count=10)
    service.save_answer(LEARNER_ID, session.id, 1, ["a"], now=NOW)
    service.submit(LEARNER_ID, session.id, now=NOW)

    reopened = wrong_questions.get_by_id(LEARNER_ID, "q1")
    assert reopened.corrected is False
    assert reopened.next_review_at is None
    assert reopened.srs_level == 0


def test_exam_correct_answer_does_not_correct_pending_mistake(exam_context):
    service, _, _, wrong_questions = exam_context
    learning = service.wrong_question_service
    learning.record_attempt(LEARNER_ID, "q0", QuizMode.NORMAL, ("a",), False, now=NOW)

    session = create(service, count=10)
    service.save_answer(LEARNER_ID, session.id, 0, ["b"], now=NOW)  # correct
    service.submit(LEARNER_ID, session.id, now=NOW)

    # Correction only happens through the review flow, never through exams.
    assert wrong_questions.get_by_id(LEARNER_ID, "q0").corrected is False


def test_deadline_rules_use_the_injected_server_clock(exam_context):
    service, _, attempts, _ = exam_context
    session = create(service, count=10, limit=600)
    deadline = NOW + timedelta(seconds=600)

    assert service.remaining_seconds(session, NOW) == 600
    assert not service.is_expired(session, NOW + timedelta(seconds=599))
    # The deadline itself already counts as expired (inclusive).
    assert service.is_expired(session, deadline)

    # Saving exactly at the deadline is rejected and auto-submits the exam.
    with pytest.raises(ExamExpiredError):
        service.save_answer(LEARNER_ID, session.id, 0, ["b"], now=deadline)
    expired = service.get_session(LEARNER_ID, session.id)
    assert expired.status is ExamStatus.EXPIRED
    assert expired.duration_seconds == 600
    assert attempts.count() == 0  # nothing was saved after the deadline


def test_finalize_if_expired_is_automatic_and_idempotent(exam_context):
    service, _, _, _ = exam_context
    session = create(service, count=10, limit=600)
    service.save_answer(LEARNER_ID, session.id, 0, ["b"], now=NOW)

    later = NOW + timedelta(hours=2)
    assert service.finalize_if_expired(session, later) is True
    assert service.get_session(LEARNER_ID, session.id).status is ExamStatus.EXPIRED
    # A second pass does not re-grade or re-flip the exam.
    assert service.finalize_if_expired(
        service.get_session(LEARNER_ID, session.id), later
    ) is False
    # Untimed exams never expire.
    untimed = create(service, count=10, limit=None)
    assert service.finalize_if_expired(untimed, NOW + timedelta(days=30)) is False


def test_report_breaks_down_score_per_chapter(exam_context):
    service, _, _, _ = exam_context
    session = create(service, count=10)  # q0..q9, alternating chapters a/b
    # Answer q0 (chapter-a, correct), q1 (chapter-b, wrong); leave the rest.
    service.save_answer(LEARNER_ID, session.id, 0, ["b"], now=NOW)
    service.save_answer(LEARNER_ID, session.id, 1, ["a"], now=NOW)
    service.submit(LEARNER_ID, session.id, now=NOW)

    report = service.get_report(LEARNER_ID, session.id)

    assert report.correct_count == 1
    assert report.wrong_count == 9
    assert report.unanswered_count == 8
    assert report.accuracy == pytest.approx(10.0)
    assert [item.is_correct for item in report.items[:3]] == [True, False, False]
    chapters = {row.chapter.id: row for row in report.chapters}
    assert chapters["chapter-a"].total == 5
    assert chapters["chapter-a"].correct == 1
    assert chapters["chapter-b"].total == 5
    assert chapters["chapter-b"].correct == 0
    assert chapters["chapter-a"].accuracy == pytest.approx(20.0)
    # Wrong items keep both answered-but-wrong and unanswered slots.
    assert len(report.wrong_items) == 9
    assert report.wrong_items[0].answered is True
    assert report.wrong_items[1].answered is False
    # An unfinished exam has no report yet.
    pending = create(service, count=10)
    with pytest.raises(ExamStateError):
        service.get_report(LEARNER_ID, pending.id)


def test_active_session_and_history(exam_context):
    service, _, _, _ = exam_context
    first = create(service, count=10, now=NOW)
    second = create(service, count=10, now=NOW + timedelta(minutes=1))

    active = service.get_active_session(LEARNER_ID)
    assert active is not None and active.id == second.id
    history = service.list_sessions(LEARNER_ID)
    assert [item.id for item in history] == [second.id, first.id]

    service.submit(LEARNER_ID, second.id, now=NOW + timedelta(minutes=2))
    assert service.get_active_session(LEARNER_ID).id == first.id


def test_expired_exam_is_never_returned_as_active(exam_context):
    service, _, _, _ = exam_context
    timed = create(service, count=10, limit=600)
    untimed = create(service, count=10, limit=None, now=NOW + timedelta(minutes=1))

    before_deadline = service.get_active_session(
        LEARNER_ID, now=NOW + timedelta(minutes=2)
    )
    assert before_deadline is not None and before_deadline.id == untimed.id

    past = NOW + timedelta(seconds=600)
    active = service.get_active_session(LEARNER_ID, now=past)
    assert active is not None and active.id == untimed.id
    assert service.get_active_session(OTHER_LEARNER_ID, now=past) is None


def test_finalize_expired_sweep_settles_and_stays_idempotent(exam_context):
    service, _, attempts, wrong_questions = exam_context
    timed = create(service, count=10, limit=600)
    untimed = create(service, count=10, limit=None, now=NOW + timedelta(minutes=1))
    service.save_answer(LEARNER_ID, timed.id, 1, ["a"], now=NOW)  # q1 wrong

    past = NOW + timedelta(hours=1)
    assert service.finalize_expired_for_learner(LEARNER_ID, now=past) == 1

    settled = service.get_session(LEARNER_ID, timed.id)
    assert settled.status is ExamStatus.EXPIRED
    assert settled.correct_count == 0
    assert attempts.count() == 1  # exactly the one saved answer
    record = wrong_questions.get_by_id(LEARNER_ID, "q1")
    assert record is not None and record.corrected is False
    # The untimed exam is untouched and still resumable.
    still_active = service.get_session(LEARNER_ID, untimed.id)
    assert still_active.status is ExamStatus.IN_PROGRESS
    # Repeating the sweep settles nothing more.
    assert service.finalize_expired_for_learner(LEARNER_ID, now=past) == 0
    assert attempts.count() == 1
    assert wrong_questions.get_by_id(LEARNER_ID, "q1").wrong_count == 1


def test_finalize_expired_sweep_settles_multiple_exams(exam_context):
    service, _, _, _ = exam_context
    create(service, count=10, limit=600)
    create(service, count=10, limit=600, now=NOW + timedelta(minutes=1))
    create(service, count=10, limit=None, now=NOW + timedelta(minutes=2))

    past = NOW + timedelta(hours=2)
    assert service.finalize_expired_for_learner(LEARNER_ID, now=past) == 2
    assert service.get_active_session(LEARNER_ID, now=past) is not None
    expired = [
        item
        for item in service.list_sessions(LEARNER_ID)
        if item.status is ExamStatus.EXPIRED
    ]
    assert len(expired) == 2
