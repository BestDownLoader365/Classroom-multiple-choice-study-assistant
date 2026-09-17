"""Cross-account learning statistics behind the global overview page.

Where ``StatisticsService`` isolates one learner's records, this service
aggregates the retained attempt window across every registered account.
It deliberately answers different questions than the personal dashboard:
how active the whole group is and which chapters are hard for everyone,
never how any single account performed. Per-learner concepts such as
pending mistakes or SRS due counts have no meaning here and are excluded
by design.

Aggregation runs in plain Python over the bounded attempt set (learners ×
questions × ``MAX_ATTEMPTS_PER_QUESTION``), mirroring the personal
service so every rule stays unit-testable with an injected ``now``.
Window and trend rules are shared with ``statistics_service``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from app.models import Attempt, Chapter, SourceDocument
from app.repositories import (
    AttemptRepository,
    QuestionRepository,
    UserRepository,
)

from . import srs_service as srs
from .statistics_service import (
    LOW_SAMPLE_ATTEMPTS,
    MASTERY_PROGRESSING_BELOW,
    MASTERY_WEAK_BELOW,
    MONTH_WINDOW_DAYS,
    RECENT_WINDOW_DAYS,
    TREND_DAYS,
    DayActivity,
    count_since,
    daily_trend,
)

# Cross-account difficulty bands, aligned with the personal mastery
# thresholds (below 60% hard, 60-79% medium, 80% and above easy).
DIFFICULTY_UNATTEMPTED = "unattempted"
DIFFICULTY_HARD = "hard"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_EASY = "easy"


@dataclass(frozen=True)
class ChapterDifficulty:
    """One chapter's aggregated performance across every learner."""

    chapter: Chapter
    source: SourceDocument | None
    attempts: int
    correct: int
    learner_count: int
    total_questions: int
    low_sample: bool

    @property
    def wrong(self) -> int:
        return self.attempts - self.correct

    @property
    def accuracy(self) -> float | None:
        if not self.attempts:
            return None
        return self.correct / self.attempts * 100

    @property
    def difficulty(self) -> str:
        """Map the cross-account accuracy onto the difficulty bands."""
        accuracy = self.accuracy
        if accuracy is None:
            return DIFFICULTY_UNATTEMPTED
        if accuracy < MASTERY_WEAK_BELOW:
            return DIFFICULTY_HARD
        if accuracy < MASTERY_PROGRESSING_BELOW:
            return DIFFICULTY_MEDIUM
        return DIFFICULTY_EASY


@dataclass(frozen=True)
class GlobalOverviewData:
    """Everything the global statistics template needs, pre-computed."""

    account_count: int
    active_account_count: int
    total_attempts: int
    correct_attempts: int
    attempts_last_7_days: int
    attempts_last_30_days: int
    chapters: tuple[ChapterDifficulty, ...]
    trend: tuple[DayActivity, ...]
    trend_window_days: int
    recent_window_days: int
    month_window_days: int

    @property
    def has_attempts(self) -> bool:
        return self.total_attempts > 0

    @property
    def accuracy(self) -> float | None:
        if not self.total_attempts:
            return None
        return self.correct_attempts / self.total_attempts * 100

    @property
    def trend_max(self) -> int:
        return max((day.attempts for day in self.trend), default=0)


class GlobalStatisticsService:
    """Compute learning statistics aggregated over every account."""

    def __init__(
        self,
        attempt_repository: AttemptRepository,
        user_repository: UserRepository,
        question_repository: QuestionRepository,
        display_tz: tzinfo | None = None,
    ) -> None:
        self.attempt_repository = attempt_repository
        self.user_repository = user_repository
        self.question_repository = question_repository
        self.display_tz = display_tz or timezone.utc

    def build_overview(self, *, now: datetime | None = None) -> GlobalOverviewData:
        """Collect every global metric at ``now``.

        As on the personal dashboard, attempts whose question has left the
        bank stay stored but no longer count toward any number.
        """
        moment = now or srs.utc_now()
        live_ids = self.question_repository.ids()
        attempts = [
            attempt
            for attempt in self.attempt_repository.list_all()
            if attempt.question_id in live_ids
        ]
        total = len(attempts)
        correct = sum(attempt.is_correct for attempt in attempts)
        week_cutoff = moment - timedelta(days=RECENT_WINDOW_DAYS)
        month_cutoff = moment - timedelta(days=MONTH_WINDOW_DAYS)
        return GlobalOverviewData(
            account_count=len(self.user_repository.list_all()),
            active_account_count=len(
                {attempt.learner_id for attempt in attempts}
            ),
            total_attempts=total,
            correct_attempts=correct,
            attempts_last_7_days=count_since(attempts, week_cutoff),
            attempts_last_30_days=count_since(attempts, month_cutoff),
            chapters=self._chapter_difficulty(attempts),
            trend=daily_trend(attempts, moment, self.display_tz),
            trend_window_days=TREND_DAYS,
            recent_window_days=RECENT_WINDOW_DAYS,
            month_window_days=MONTH_WINDOW_DAYS,
        )

    def _chapter_difficulty(
        self, attempts: list[Attempt]
    ) -> tuple[ChapterDifficulty, ...]:
        """Aggregate attempts per chapter, hardest first.

        A question belonging to several chapters counts toward each of
        them, mirroring the per-learner dashboard rules. Chapters nobody
        attempted keep curriculum order after the attempted ones.
        """
        questions_by_id = {
            question.id: question
            for question in self.question_repository.get_all()
        }
        attempts_by_chapter: dict[str, list[Attempt]] = {}
        learners_by_chapter: dict[str, set[str]] = {}
        for attempt in attempts:
            question = questions_by_id.get(attempt.question_id)
            if question is None:
                continue
            for chapter_id in question.chapter_ids:
                attempts_by_chapter.setdefault(chapter_id, []).append(attempt)
                learners_by_chapter.setdefault(chapter_id, set()).add(
                    attempt.learner_id
                )
        attempted: list[ChapterDifficulty] = []
        unattempted: list[ChapterDifficulty] = []
        for source in self.question_repository.get_sources():
            for chapter in self.question_repository.get_chapters(source.id):
                chapter_attempts = attempts_by_chapter.get(chapter.id, [])
                total = len(chapter_attempts)
                row = ChapterDifficulty(
                    chapter=chapter,
                    source=source,
                    attempts=total,
                    correct=sum(item.is_correct for item in chapter_attempts),
                    learner_count=len(
                        learners_by_chapter.get(chapter.id, set())
                    ),
                    total_questions=(
                        self.question_repository.question_count_for_chapter(
                            chapter.id
                        )
                    ),
                    low_sample=0 < total < LOW_SAMPLE_ATTEMPTS,
                )
                if total:
                    attempted.append(row)
                else:
                    unattempted.append(row)
        attempted.sort(
            key=lambda row: (row.accuracy, -row.attempts, row.chapter.order)
        )
        return tuple(attempted + unattempted)
