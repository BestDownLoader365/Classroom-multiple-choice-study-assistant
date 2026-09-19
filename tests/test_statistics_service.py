"""Unit tests for the dashboard statistics aggregation."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import LEGACY_COURSE_ID
from app.models import Chapter, Option, Question, QuizMode, SourceDocument
from app.repositories import (
    AttemptRepository,
    Database,
    QuestionRepository,
    WrongQuestionRepository,
)
from app.services import StatisticsService, WrongQuestionService

LEARNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_LEARNER_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_questions() -> list[Question]:
    """Four questions: two in chapter-a, one in chapter-b, one shared."""

    def question(question_id, chapter_ids):
        return Question(
            id=question_id,
            text=f"Question {question_id}",
            question_type="single",
            options=(Option("a", "Alpha"), Option("b", "Beta")),
            correct_answers=("b",),
            explanation="",
            source_id="source-a",
            chapter_ids=chapter_ids,
        )

    return [
        question("q1", ("chapter-a",)),
        question("q2", ("chapter-a",)),
        question("q3", ("chapter-b",)),
        question("q4", ("chapter-a", "chapter-b")),
    ]


@pytest.fixture
def stats_context(tmp_path):
    database = Database(tmp_path / "mcq.db")
    database.initialize()
    question_repository = QuestionRepository(
        make_questions(),
        sources=(
            SourceDocument(id="source-a", title="Source A", lecture="Lecture 1"),
        ),
        chapters=(
            Chapter(id="chapter-a", source_id="source-a", title="Chapter A", order=1),
            Chapter(id="chapter-b", source_id="source-a", title="Chapter B", order=2),
        ),
    )
    attempts = AttemptRepository(database, course_id=LEGACY_COURSE_ID)
    wrong_questions = WrongQuestionRepository(database, course_id=LEGACY_COURSE_ID)
    wrong_question_service = WrongQuestionService(
        attempt_repository=attempts,
        wrong_question_repository=wrong_questions,
        question_repository=question_repository,
    )
    service = StatisticsService(
        attempt_repository=attempts,
        question_repository=question_repository,
        wrong_question_service=wrong_question_service,
    )
    return service, wrong_question_service


def record(
    context, question_id, correct, *, when=NOW, learner=LEARNER_ID,
    mode=QuizMode.NORMAL,
):
    service, wrong_question_service = context
    wrong_question_service.record_attempt(
        learner_id=learner,
        question_id=question_id,
        mode=mode,
        selected_answers=("b",) if correct else ("a",),
        is_correct=correct,
        now=when,
    )


def build(context, learner=LEARNER_ID, now=NOW):
    return context[0].build_dashboard(learner, now=now)


def test_empty_learner_has_a_clean_zero_state(stats_context):
    dashboard = build(stats_context)

    assert dashboard.total_attempts == 0
    assert dashboard.accuracy is None
    assert dashboard.attempts_last_7_days == 0
    assert dashboard.attempts_last_30_days == 0
    assert (dashboard.pending_wrong, dashboard.corrected_wrong) == (0, 0)
    assert dashboard.has_attempts is False
    assert all(day.attempts == 0 for day in dashboard.trend)
    assert len(dashboard.trend) == 7
    assert all(row.status == "not_started" for row in dashboard.chapters)
    assert all(row.attempts == 0 for row in dashboard.chapters)
    assert [row.total_questions for row in dashboard.chapters] == [3, 2]


def test_overall_counts_accuracy_and_windows(stats_context):
    record(stats_context, "q1", True)
    record(stats_context, "q2", False)
    record(stats_context, "q3", True, when=NOW - timedelta(days=3))
    record(stats_context, "q1", True, when=NOW - timedelta(days=10))
    record(stats_context, "q2", True, when=NOW - timedelta(days=20))

    dashboard = build(stats_context)

    assert dashboard.total_attempts == 5
    assert dashboard.correct_attempts == 4
    assert dashboard.accuracy == pytest.approx(80.0)
    assert dashboard.attempts_last_7_days == 3
    assert dashboard.attempts_last_30_days == 5


def test_recent_windows_include_the_cutoff_day(stats_context):
    record(stats_context, "q1", True, when=NOW - timedelta(days=7))
    record(stats_context, "q2", True, when=NOW - timedelta(days=30))
    record(stats_context, "q3", True, when=NOW - timedelta(days=31))

    dashboard = build(stats_context)

    assert dashboard.attempts_last_7_days == 1
    assert dashboard.attempts_last_30_days == 2


def test_chapter_mastery_tracks_accuracy_coverage_and_shared_questions(
    stats_context,
):
    record(stats_context, "q1", True)
    record(stats_context, "q1", True)
    record(stats_context, "q2", False)
    record(stats_context, "q4", True)  # belongs to both chapters

    dashboard = build(stats_context)
    chapter_a, chapter_b = dashboard.chapters

    assert chapter_a.attempts == 4  # q1 twice + q2 + q4
    assert chapter_a.correct == 3
    assert chapter_a.wrong == 1
    assert chapter_a.accuracy == pytest.approx(75.0)
    assert chapter_a.status == "progressing"
    assert chapter_a.covered_questions == 3
    assert chapter_b.attempts == 1
    assert chapter_b.accuracy == pytest.approx(100.0)
    assert chapter_b.status == "mastered"
    assert chapter_b.low_sample is True  # only one attempt so far
    assert chapter_a.low_sample is False


def test_status_bands_and_low_sample_flag(stats_context):
    record(stats_context, "q1", True)
    record(stats_context, "q1", False)
    record(stats_context, "q1", False)
    record(stats_context, "q1", True)
    record(stats_context, "q2", False)  # chapter-a: 2/5 = 40% -> weak

    chapter_a = build(stats_context).chapters[0]
    assert chapter_a.status == "weak"
    assert chapter_a.low_sample is False


def test_trend_fills_missing_days_and_counts_per_utc_day(stats_context):
    record(stats_context, "q1", True)
    record(stats_context, "q2", False)
    record(stats_context, "q3", True, when=NOW - timedelta(days=2))
    record(stats_context, "q1", True, when=NOW - timedelta(days=9))  # outside

    dashboard = build(stats_context)
    trend = {day.day: day for day in dashboard.trend}

    today = NOW.date().isoformat()
    two_days_ago = (NOW - timedelta(days=2)).date().isoformat()
    nine_days_ago = (NOW - timedelta(days=9)).date().isoformat()
    assert trend[today].attempts == 2
    assert trend[today].accuracy == pytest.approx(50.0)
    assert trend[two_days_ago].attempts == 1
    assert nine_days_ago not in trend
    assert dashboard.trend[0].day == (NOW - timedelta(days=6)).date().isoformat()
    assert dashboard.trend_max == 2


def test_mistake_counts_come_from_the_existing_state_machine(stats_context):
    record(stats_context, "q1", False)
    record(stats_context, "q2", False)
    record(stats_context, "q1", True, mode=QuizMode.REVIEW)  # corrects q1

    dashboard = build(stats_context)

    assert dashboard.pending_wrong == 1
    assert dashboard.corrected_wrong == 1
    assert dashboard.due_srs == 0  # q1 is scheduled one day out


def test_statistics_are_isolated_per_learner(stats_context):
    record(stats_context, "q1", True)
    record(stats_context, "q2", False, learner=OTHER_LEARNER_ID)
    record(stats_context, "q3", True, learner=OTHER_LEARNER_ID)

    mine = build(stats_context)
    theirs = build(stats_context, learner=OTHER_LEARNER_ID)

    assert mine.total_attempts == 1
    assert mine.accuracy == pytest.approx(100.0)
    assert theirs.total_attempts == 2
    assert theirs.pending_wrong == 1
    assert mine.pending_wrong == 0


def test_trend_buckets_follow_the_display_timezone(stats_context):
    """An attempt late at night UTC belongs to the local next day."""
    service, _ = stats_context
    # UTC 2026-03-14 16:30 is 2026-03-15 00:30 at UTC+8.
    record(
        stats_context,
        "q1",
        True,
        when=datetime(2026, 3, 14, 16, 30, tzinfo=timezone.utc),
    )

    utc_days = {day.day: day for day in build(stats_context).trend}
    assert utc_days["2026-03-14"].attempts == 1
    assert utc_days["2026-03-15"].attempts == 0

    plus8 = timezone(timedelta(hours=8))
    local_service = StatisticsService(
        attempt_repository=service.attempt_repository,
        question_repository=service.question_repository,
        wrong_question_service=service.wrong_question_service,
        display_tz=plus8,
    )
    local_days = {
        day.day: day
        for day in local_service.build_dashboard(LEARNER_ID, now=NOW).trend
    }
    assert local_days["2026-03-15"].attempts == 1
    assert local_days["2026-03-14"].attempts == 0
