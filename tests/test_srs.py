"""SRS scheduling rules, persistence, migration, and review selection."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.models import LEGACY_COURSE_ID
from app.models import Chapter, Option, Question, QuizMode
from app.repositories import (
    AttemptRepository,
    Database,
    QuestionRepository,
    WrongQuestionRepository,
)
from app.services import (
    ORIGINAL_CORRECTION,
    SRS_REVIEW,
    TRANSFER_VERIFICATION,
    GradingService,
    QuizService,
    WrongQuestionService,
)
from app.services import srs_service as srs

LEARNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_LEARNER_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

LEGACY_WRONG_QUESTIONS_SCHEMA = """
CREATE TABLE wrong_questions (
    learner_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    wrong_count INTEGER NOT NULL DEFAULT 1,
    review_streak INTEGER NOT NULL DEFAULT 0,
    mastered INTEGER NOT NULL DEFAULT 0 CHECK (mastered IN (0, 1)),
    last_wrong_at TEXT NOT NULL,
    last_reviewed_at TEXT,
    PRIMARY KEY (learner_id, question_id)
);
"""


# ---------------------------------------------------------------------------
# Pure scheduling rules
# ---------------------------------------------------------------------------


def test_interval_progression_matches_fixed_table():
    assert [srs.interval_days_for_level(level) for level in range(6)] == [
        1,
        3,
        7,
        15,
        30,
        30,
    ]


def test_interval_is_capped_and_clamped():
    assert srs.interval_days_for_level(99) == 30
    assert srs.interval_days_for_level(-3) == 1


def test_initial_schedule_is_level_zero_one_day_later():
    level, next_review_at = srs.initial_schedule(NOW)

    assert level == 0
    assert srs.parse_timestamp(next_review_at) == NOW + timedelta(days=1)


def test_advanced_schedule_walks_the_whole_ladder():
    level, moment = 0, NOW
    for expected_level, expected_days in [(1, 3), (2, 7), (3, 15), (4, 30), (5, 30)]:
        level, next_review_at = srs.advanced_schedule(level, moment)
        assert level == expected_level
        assert srs.parse_timestamp(next_review_at) == moment + timedelta(
            days=expected_days
        )
        moment += timedelta(days=expected_days)


def test_is_due_is_inclusive_and_rejects_unscheduled():
    assert srs.is_due(None, NOW) is False
    assert srs.is_due(NOW.isoformat(), NOW) is True
    assert srs.is_due((NOW - timedelta(seconds=1)).isoformat(), NOW) is True
    assert srs.is_due((NOW + timedelta(seconds=1)).isoformat(), NOW) is False


def test_naive_timestamps_are_interpreted_as_utc():
    assert srs.parse_timestamp("2026-01-01T12:00:00") == NOW
    assert srs.is_due("2026-01-01T12:00:00", NOW) is True


# ---------------------------------------------------------------------------
# Repository persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    return WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID)


def test_new_wrong_record_has_no_srs_schedule(repository):
    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), False)

    record = repository.get_by_id(LEARNER_ID, "q1")

    assert record.srs_level == 0
    assert record.next_review_at is None


def test_record_corrected_persists_srs_schedule(repository):
    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), False)
    next_review_at = (NOW + timedelta(days=1)).isoformat()

    repository.record_corrected(
        LEARNER_ID, "q1", NOW.isoformat(), srs_level=0, next_review_at=next_review_at
    )

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.corrected is True
    assert record.srs_level == 0
    assert record.next_review_at == next_review_at


def test_record_corrected_without_srs_arguments_keeps_existing_schedule(repository):
    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), False)
    next_review_at = (NOW + timedelta(days=15)).isoformat()
    repository.record_corrected(
        LEARNER_ID, "q1", NOW.isoformat(), srs_level=3, next_review_at=next_review_at
    )

    repository.record_corrected(LEARNER_ID, "q1", NOW.isoformat())

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.srs_level == 3
    assert record.next_review_at == next_review_at


def test_record_srs_reviewed_advances_only_corrected_records(repository):
    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), False)
    repository.record_corrected(
        LEARNER_ID, "q1", NOW.isoformat(), srs_level=0, next_review_at=NOW.isoformat()
    )
    repository.record_wrong(LEARNER_ID, "q2", NOW.isoformat(), False)
    advanced_at = (NOW + timedelta(days=3)).isoformat()

    repository.record_srs_reviewed(
        LEARNER_ID, "q1", NOW.isoformat(), srs_level=1, next_review_at=advanced_at
    )
    repository.record_srs_reviewed(
        LEARNER_ID, "q2", NOW.isoformat(), srs_level=1, next_review_at=advanced_at
    )

    assert repository.get_by_id(LEARNER_ID, "q1").srs_level == 1
    assert repository.get_by_id(LEARNER_ID, "q1").next_review_at == advanced_at
    # q2 is uncorrected: the guarded update must not touch it.
    pending = repository.get_by_id(LEARNER_ID, "q2")
    assert pending.corrected is False
    assert pending.srs_level == 0
    assert pending.next_review_at is None


def test_record_wrong_resets_srs_schedule(repository):
    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), False)
    repository.record_corrected(
        LEARNER_ID,
        "q1",
        NOW.isoformat(),
        srs_level=4,
        next_review_at=(NOW + timedelta(days=30)).isoformat(),
    )

    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), True)

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.corrected is False
    assert record.srs_level == 0
    assert record.next_review_at is None
    assert record.wrong_count == 2


def test_get_due_applies_boundary_correction_and_learner_filters(repository):
    for question_id in ("q-equal", "q-future", "q-legacy"):
        repository.record_wrong(LEARNER_ID, question_id, NOW.isoformat(), False)
        repository.record_corrected(LEARNER_ID, question_id, NOW.isoformat())
    repository.record_wrong(OTHER_LEARNER_ID, "q-other", NOW.isoformat(), False)
    repository.record_corrected(OTHER_LEARNER_ID, "q-other", NOW.isoformat())
    with repository.database.connect() as connection:
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ?, srs_level = 1 "
            "WHERE learner_id = ? AND question_id = 'q-equal'",
            (NOW.isoformat(), LEARNER_ID),
        )
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ? "
            "WHERE learner_id = ? AND question_id = 'q-future'",
            ((NOW + timedelta(seconds=1)).isoformat(), LEARNER_ID),
        )
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ? "
            "WHERE learner_id = ? AND question_id = 'q-other'",
            ((NOW - timedelta(days=2)).isoformat(), OTHER_LEARNER_ID),
        )

    due = repository.get_due(LEARNER_ID, NOW.isoformat())

    # 恰好等于当前时间视为到期；未来题、未排期历史题、他人题目都被排除。
    assert [record.question_id for record in due] == ["q-equal"]


def test_get_due_never_returns_uncorrected_rows(repository):
    repository.record_wrong(LEARNER_ID, "q1", NOW.isoformat(), False)
    with repository.database.connect() as connection:
        # Simulate an inconsistent legacy row: pending but with a past due date.
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ? "
            "WHERE learner_id = ? AND question_id = 'q1'",
            ((NOW - timedelta(days=1)).isoformat(), LEARNER_ID),
        )

    assert repository.get_due(LEARNER_ID, NOW.isoformat()) == []


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_fresh_database_contains_srs_columns(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(wrong_questions)")
        }

    assert {"srs_level", "next_review_at"} <= columns


def test_legacy_database_is_upgraded_in_place_without_forcing_due(tmp_path):
    path = tmp_path / "mcq.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_WRONG_QUESTIONS_SCHEMA)
        connection.execute(
            """
            INSERT INTO wrong_questions (
                learner_id, question_id, wrong_count, review_streak, mastered,
                last_wrong_at, last_reviewed_at
            ) VALUES (
                'learner-id', 'q1', 2, 1, 1,
                '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO wrong_questions (
                learner_id, question_id, wrong_count, review_streak, mastered,
                last_wrong_at, last_reviewed_at
            ) VALUES ('learner-id', 'q2', 1, 0, 0, '2026-01-03T00:00:00+00:00', NULL)
            """
        )

    database = Database(path)
    database.initialize()
    database.initialize()  # the migration must be idempotent

    repository = WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID)
    corrected = repository.get_by_id("learner-id", "q1")
    pending = repository.get_by_id("learner-id", "q2")
    assert (corrected.corrected, corrected.wrong_count, corrected.review_streak) == (
        True,
        2,
        1,
    )
    assert corrected.srs_level == 0
    assert corrected.next_review_at is None
    assert pending.corrected is False
    assert pending.next_review_at is None
    # 历史记录在升级后保持原行为，不会被强制变成“今天到期”。
    assert repository.get_due("learner-id", "2999-01-01T00:00:00+00:00") == []


# ---------------------------------------------------------------------------
# Service-level SRS state machine
# ---------------------------------------------------------------------------


@pytest.fixture
def srs_context(tmp_path, sample_questions):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    wrong_repository = WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID)
    service = WrongQuestionService(
        attempt_repository=AttemptRepository(database, course_id=LEGACY_COURSE_ID),
        wrong_question_repository=wrong_repository,
        question_repository=QuestionRepository(sample_questions),
    )
    return service, wrong_repository


def test_first_wrong_answer_creates_no_srs_schedule(srs_context):
    service, repository = srs_context

    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.corrected is False
    assert record.next_review_at is None
    assert service.get_due_srs_count(LEARNER_ID, now=NOW) == 0


def test_correction_schedules_first_review_one_day_later(srs_context):
    service, repository = srs_context
    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)

    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.corrected is True
    assert record.srs_level == 0
    assert srs.parse_timestamp(record.next_review_at) == NOW + timedelta(days=1)
    # 刚安排的复习不算到期，也不会在“恰好到期”边界前出现。
    assert service.get_due_srs_count(LEARNER_ID, now=NOW) == 0
    assert service.get_due_srs_count(LEARNER_ID, now=NOW + timedelta(days=1)) == 1


def test_due_review_correct_walks_the_full_interval_ladder(srs_context):
    service, repository = srs_context
    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)
    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)

    moment = NOW + timedelta(days=1)
    for expected_level, expected_days in [(1, 3), (2, 7), (3, 15), (4, 30), (5, 30)]:
        assert service.get_due_srs_count(LEARNER_ID, now=moment) == 1
        service.record_attempt(
            LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=moment
        )
        record = repository.get_by_id(LEARNER_ID, "q1")
        assert record.srs_level == expected_level
        assert srs.parse_timestamp(record.next_review_at) == moment + timedelta(
            days=expected_days
        )
        moment += timedelta(days=expected_days)


def test_due_review_wrong_returns_to_correction_flow(srs_context):
    service, repository = srs_context
    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)
    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)
    due_at = NOW + timedelta(days=1)

    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("1",), False, now=due_at)

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.corrected is False
    assert record.srs_level == 0
    assert record.next_review_at is None
    # 同一道题只处于“待纠正”一种状态，不会同时作为到期 SRS 重复出现。
    assert service.get_filtered_uncorrected_question_ids(LEARNER_ID) == ["q1"]
    assert service.get_filtered_due_srs_question_ids(LEARNER_ID, now=due_at) == []

    # 重新纠正成功后从 1 天周期重新开始。
    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=due_at)
    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.corrected is True
    assert record.srs_level == 0
    assert srs.parse_timestamp(record.next_review_at) == due_at + timedelta(days=1)


def test_not_due_review_correct_keeps_existing_schedule(srs_context):
    service, repository = srs_context
    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)
    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)
    scheduled_at = repository.get_by_id(LEARNER_ID, "q1").next_review_at

    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.srs_level == 0
    assert record.next_review_at == scheduled_at


def test_normal_mode_correct_does_not_touch_the_schedule(srs_context):
    service, repository = srs_context
    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)
    service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)
    scheduled_at = repository.get_by_id(LEARNER_ID, "q1").next_review_at
    due_at = NOW + timedelta(days=2)

    service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("2",), True, now=due_at)

    record = repository.get_by_id(LEARNER_ID, "q1")
    assert record.srs_level == 0
    assert record.next_review_at == scheduled_at


def test_due_srs_items_are_isolated_per_learner(srs_context):
    service, repository = srs_context
    for learner in (LEARNER_ID, OTHER_LEARNER_ID):
        service.record_attempt(learner, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)
        service.record_attempt(learner, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)
    # 两人的排期都是 NOW+1 天；只把 OTHER 的排期改到过去。
    with repository.database.connect() as connection:
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ? WHERE learner_id = ?",
            ((NOW - timedelta(days=1)).isoformat(), OTHER_LEARNER_ID),
        )
    moment = NOW + timedelta(hours=12)

    assert service.get_due_srs_count(LEARNER_ID, now=moment) == 0
    assert service.get_due_srs_count(OTHER_LEARNER_ID, now=moment) == 1



# ---------------------------------------------------------------------------
# Review selection priority
# ---------------------------------------------------------------------------


def build_quiz(tmp_path, questions, shuffler=lambda values: None, chapters=()):
    question_repository = QuestionRepository(questions, chapters=chapters)
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    wrong_repository = WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID)
    wrong_service = WrongQuestionService(
        AttemptRepository(database, course_id=LEGACY_COURSE_ID),
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


def make_due(repository, question_id, learner_id=LEARNER_ID, when=None):
    due_at = (when or (NOW - timedelta(days=1))).isoformat()
    with repository.database.connect() as connection:
        connection.execute(
            "UPDATE wrong_questions SET next_review_at = ? "
            "WHERE learner_id = ? AND question_id = ?",
            (due_at, learner_id, question_id),
        )


def chapter_pair():
    return [
        Question(
            id="qa",
            text="First",
            question_type="single",
            options=(Option("y", "Yes"), Option("n", "No")),
            correct_answers=("y",),
            explanation="Yes.",
            source_id="source-a",
            chapter_ids=("chapter-a",),
        ),
        Question(
            id="qb",
            text="Second",
            question_type="single",
            options=(Option("y", "Yes"), Option("n", "No")),
            correct_answers=("y",),
            explanation="Yes.",
            source_id="source-a",
            chapter_ids=("chapter-a",),
        ),
    ]


def test_start_review_lists_pending_corrections_before_due_srs(
    tmp_path, sample_questions
):
    quiz, wrong_service, repository = build_quiz(tmp_path, sample_questions)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False)
    wrong_service.record_attempt(LEARNER_ID, "q2", QuizMode.NORMAL, ("b",), False)
    wrong_service.record_attempt(LEARNER_ID, "q2", QuizMode.REVIEW, ("a", "c"), True)
    make_due(repository, "q2")

    started = quiz.start_review(LEARNER_ID)
    assert [(item.question_id, item.role) for item in started.items] == [
        ("q1", ORIGINAL_CORRECTION)
    ]

    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True)
    follow_up = quiz.next_review_item(LEARNER_ID)
    assert [(item.question_id, item.role) for item in follow_up.items] == [
        ("q2", SRS_REVIEW)
    ]


def test_start_review_queues_all_due_questions(tmp_path, sample_questions):
    quiz, wrong_service, repository = build_quiz(tmp_path, sample_questions)
    for question_id, wrong_answer, right_answer in (
        ("q1", ("1",), ("2",)),
        ("q2", ("b",), ("a", "c")),
    ):
        wrong_service.record_attempt(
            LEARNER_ID, question_id, QuizMode.NORMAL, wrong_answer, False, now=NOW
        )
        wrong_service.record_attempt(
            LEARNER_ID, question_id, QuizMode.REVIEW, right_answer, True, now=NOW
        )
        make_due(repository, question_id)

    started = quiz.start_review(LEARNER_ID)

    assert {item.question_id for item in started.items} == {"q1", "q2"}
    assert {item.role for item in started.items} == {SRS_REVIEW}


def test_not_due_corrected_question_is_never_selected(tmp_path, sample_questions):
    quiz, wrong_service, _ = build_quiz(tmp_path, sample_questions)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True)

    started = quiz.start_review(LEARNER_ID)

    assert started.items == ()
    assert all(
        item.role != SRS_REVIEW for item in quiz.next_review_item(LEARNER_ID).items
    )


def test_due_srs_review_is_preferred_over_transfer_verification(tmp_path):
    chapters = (Chapter(id="chapter-a", source_id="source-a", title="A"),)
    quiz, wrong_service, repository = build_quiz(
        tmp_path, chapter_pair(), chapters=chapters
    )
    wrong_service.record_attempt(LEARNER_ID, "qa", QuizMode.NORMAL, ("n",), False, now=NOW)
    wrong_service.record_attempt(LEARNER_ID, "qa", QuizMode.REVIEW, ("y",), True, now=NOW)
    make_due(repository, "qa")

    selection = quiz.next_review_item(LEARNER_ID)

    # chapter-a 的强化尚未完成（qb 可作强化题），但到期 SRS 优先。
    assert [(item.question_id, item.role) for item in selection.items] == [
        ("qa", SRS_REVIEW)
    ]


def test_transfer_verification_is_used_when_nothing_is_due(tmp_path):
    chapters = (Chapter(id="chapter-a", source_id="source-a", title="A"),)
    quiz, wrong_service, _ = build_quiz(tmp_path, chapter_pair(), chapters=chapters)
    wrong_service.record_attempt(LEARNER_ID, "qa", QuizMode.NORMAL, ("n",), False)
    wrong_service.record_attempt(LEARNER_ID, "qa", QuizMode.REVIEW, ("y",), True)

    selection = quiz.next_review_item(LEARNER_ID)

    assert [(item.question_id, item.role) for item in selection.items] == [
        ("qb", TRANSFER_VERIFICATION)
    ]


def test_due_selection_respects_curriculum_filters_and_avoid(tmp_path, sample_questions):
    quiz, wrong_service, repository = build_quiz(tmp_path, sample_questions)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.NORMAL, ("1",), False, now=NOW)
    wrong_service.record_attempt(LEARNER_ID, "q1", QuizMode.REVIEW, ("2",), True, now=NOW)
    make_due(repository, "q1")

    assert quiz.start_review(LEARNER_ID, chapter_ids={"chapter-b"}).items == ()
    assert quiz.next_review_item(LEARNER_ID, avoid_question_ids={"q1"}).items == ()

