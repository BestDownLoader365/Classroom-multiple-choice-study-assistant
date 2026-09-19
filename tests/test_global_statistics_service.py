"""Unit tests for the cross-account global statistics aggregation."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import LEGACY_COURSE_ID
from app.models import Attempt, Chapter, Option, Question, QuizMode, SourceDocument
from app.repositories import (
    AttemptRepository,
    Database,
    QuestionRepository,
    UserRepository,
)
from app.services import GlobalStatisticsService

NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_questions() -> list[Question]:
    """Four questions over three chapters, one question shared."""

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
        question("q5", ("chapter-c",)),
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
            Chapter(id="chapter-c", source_id="source-a", title="Chapter C", order=3),
        ),
    )
    attempts = AttemptRepository(database, course_id=LEGACY_COURSE_ID)
    users = UserRepository(database)
    service = GlobalStatisticsService(
        attempt_repository=attempts,
        user_repository=users,
        question_repository=question_repository,
    )
    return service, attempts, users


def register(context, username):
    return context[2].create(username, "secret1")


def record(context, learner_id, question_id, correct, *, when=NOW):
    context[1].add(
        Attempt(
            learner_id=learner_id,
            question_id=question_id,
            mode=QuizMode.NORMAL,
            selected_answers=("b",) if correct else ("a",),
            is_correct=correct,
            answered_at=when.isoformat(),
        )
    )


def build(context, now=NOW):
    return context[0].build_overview(now=now)


def test_empty_state_has_zero_counts_and_clean_chapters(stats_context):
    overview = build(stats_context)

    assert overview.account_count == 0
    assert overview.active_account_count == 0
    assert overview.total_attempts == 0
    assert overview.accuracy is None
    assert overview.attempts_last_7_days == 0
    assert overview.attempts_last_30_days == 0
    assert overview.has_attempts is False
    assert len(overview.trend) == 7
    assert all(day.attempts == 0 for day in overview.trend)
    assert [row.chapter.id for row in overview.chapters] == [
        "chapter-a",
        "chapter-b",
        "chapter-c",
    ]
    assert all(row.difficulty == "unattempted" for row in overview.chapters)
    assert all(row.learner_count == 0 for row in overview.chapters)


def test_counts_accounts_activity_and_accuracy_across_learners(stats_context):
    alice = register(stats_context, "alice")
    bob = register(stats_context, "bob")
    register(stats_context, "carol")  # never answers
    record(stats_context, alice.id, "q1", True)
    record(stats_context, alice.id, "q2", False)
    record(stats_context, bob.id, "q3", True)

    overview = build(stats_context)

    assert overview.account_count == 3
    assert overview.active_account_count == 2
    assert overview.total_attempts == 3
    assert overview.correct_attempts == 2
    assert overview.accuracy == pytest.approx(200 / 3)


def test_recent_windows_include_the_cutoff_day(stats_context):
    alice = register(stats_context, "alice")
    record(stats_context, alice.id, "q1", True, when=NOW - timedelta(days=7))
    record(stats_context, alice.id, "q2", True, when=NOW - timedelta(days=30))
    record(stats_context, alice.id, "q3", True, when=NOW - timedelta(days=31))

    overview = build(stats_context)

    assert overview.attempts_last_7_days == 1
    assert overview.attempts_last_30_days == 2


def test_trend_aggregates_every_account_and_fills_missing_days(stats_context):
    alice = register(stats_context, "alice")
    bob = register(stats_context, "bob")
    record(stats_context, alice.id, "q1", True)
    record(stats_context, bob.id, "q2", False)
    record(stats_context, bob.id, "q3", True, when=NOW - timedelta(days=2))
    record(stats_context, alice.id, "q1", True, when=NOW - timedelta(days=9))

    overview = build(stats_context)
    trend = {day.day: day for day in overview.trend}

    today = NOW.date().isoformat()
    two_days_ago = (NOW - timedelta(days=2)).date().isoformat()
    nine_days_ago = (NOW - timedelta(days=9)).date().isoformat()
    assert trend[today].attempts == 2
    assert trend[today].accuracy == pytest.approx(50.0)
    assert trend[two_days_ago].attempts == 1
    assert nine_days_ago not in trend
    assert overview.trend[0].day == (NOW - timedelta(days=6)).date().isoformat()
    assert overview.trend_max == 2


def test_chapter_difficulty_orders_hardest_first_then_unattempted(stats_context):
    alice = register(stats_context, "alice")
    # chapter-b: 0/2 -> 0% (hard); chapter-a: 3/4 -> 75% (medium).
    record(stats_context, alice.id, "q3", False)
    record(stats_context, alice.id, "q3", False)
    record(stats_context, alice.id, "q1", True)
    record(stats_context, alice.id, "q1", True)
    record(stats_context, alice.id, "q1", True)
    record(stats_context, alice.id, "q2", False)

    overview = build(stats_context)

    assert [row.chapter.id for row in overview.chapters] == [
        "chapter-b",
        "chapter-a",
        "chapter-c",
    ]
    chapter_b, chapter_a, chapter_c = overview.chapters
    assert chapter_b.accuracy == pytest.approx(0.0)
    assert chapter_b.difficulty == "hard"
    assert chapter_a.accuracy == pytest.approx(75.0)
    assert chapter_a.difficulty == "medium"
    assert chapter_c.difficulty == "unattempted"
    assert chapter_c.accuracy is None


def test_chapter_difficulty_counts_learners_and_shared_questions(stats_context):
    alice = register(stats_context, "alice")
    bob = register(stats_context, "bob")
    record(stats_context, alice.id, "q4", True)  # shared by both chapters
    record(stats_context, bob.id, "q4", False)
    record(stats_context, alice.id, "q1", True)

    overview = build(stats_context)
    rows = {row.chapter.id: row for row in overview.chapters}
    chapter_a, chapter_b = rows["chapter-a"], rows["chapter-b"]

    assert chapter_a.attempts == 3  # q1 + q4 twice
    assert chapter_a.correct == 2
    assert chapter_a.learner_count == 2
    assert chapter_b.attempts == 2  # q4 twice
    assert chapter_b.learner_count == 2
    assert chapter_a.total_questions == 3
    assert chapter_b.total_questions == 2


def test_difficulty_bands_and_low_sample_flag(stats_context):
    alice = register(stats_context, "alice")
    bob = register(stats_context, "bob")
    # chapter-a: 9/10 -> 90% (easy), chapter-b: one attempt (low sample).
    for _ in range(9):
        record(stats_context, alice.id, "q1", True)
    record(stats_context, alice.id, "q1", False)
    record(stats_context, bob.id, "q3", True)

    overview = build(stats_context)
    # Lower accuracy sorts first: chapter-a (90%) leads chapter-b (100%).
    chapter_a, chapter_b = overview.chapters[0], overview.chapters[1]

    assert chapter_a.difficulty == "easy"
    assert chapter_a.low_sample is False
    assert chapter_b.difficulty == "easy"
    assert chapter_b.low_sample is True


def test_accuracy_tiebreak_puts_more_attempts_first(stats_context):
    alice = register(stats_context, "alice")
    record(stats_context, alice.id, "q3", True)  # chapter-b: 1/1 = 100%
    for _ in range(4):
        record(stats_context, alice.id, "q1", True)  # chapter-a: 4/4 = 100%

    overview = build(stats_context)

    assert [row.chapter.id for row in overview.chapters[:2]] == [
        "chapter-a",
        "chapter-b",
    ]


def test_trend_buckets_follow_the_display_timezone(stats_context):
    """An attempt late at night UTC belongs to the local next day."""
    alice = register(stats_context, "alice")
    # UTC 2026-03-14 16:30 is 2026-03-15 00:30 at UTC+8.
    record(
        stats_context,
        alice.id,
        "q1",
        True,
        when=datetime(2026, 3, 14, 16, 30, tzinfo=timezone.utc),
    )

    utc_days = {day.day: day for day in build(stats_context).trend}
    assert utc_days["2026-03-14"].attempts == 1
    assert utc_days["2026-03-15"].attempts == 0

    plus8 = timezone(timedelta(hours=8))
    service, _, _ = stats_context
    local_service = GlobalStatisticsService(
        attempt_repository=service.attempt_repository,
        user_repository=service.user_repository,
        question_repository=service.question_repository,
        display_tz=plus8,
    )
    local_days = {
        day.day: day for day in local_service.build_overview(now=NOW).trend
    }
    assert local_days["2026-03-15"].attempts == 1
    assert local_days["2026-03-14"].attempts == 0
