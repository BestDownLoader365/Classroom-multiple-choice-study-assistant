"""Aggregated learning statistics behind the dashboard page.

All numbers derive from the retained attempt window (a bounded number of
recent answers per question, see ``MAX_ATTEMPTS_PER_QUESTION``) plus the
wrong-question state machine, so the service never invents its own
definitions of "corrected" or "due". Aggregation runs in plain Python over
one learner's bounded attempt set, which keeps the SQL layer trivial and
every rule unit-testable with an injected ``now``.

Durations (7/30-day windows) are computed in UTC, while the trend's calendar
days follow the configured display timezone. The service defaults to UTC so
it stays deterministic without an application context; ``create_app`` injects
the resolved display zone.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from app.models import Attempt, Chapter, SourceDocument
from app.repositories import AttemptRepository, QuestionRepository

from . import srs_service as srs
from .local_time import display_day
from .wrong_question_service import WrongQuestionService

# Dashboard time windows, in days.
RECENT_WINDOW_DAYS = 7
MONTH_WINDOW_DAYS = 30
TREND_DAYS = 7

# Chapter mastery bands (accuracy percent). Below ``LOW_SAMPLE_ATTEMPTS``
# attempts a chapter keeps its band but is flagged as low-sample data.
MASTERY_WEAK_BELOW = 60.0
MASTERY_PROGRESSING_BELOW = 80.0
MASTERY_GOOD_BELOW = 90.0
LOW_SAMPLE_ATTEMPTS = 3

STATUS_NOT_STARTED = "not_started"
STATUS_WEAK = "weak"
STATUS_PROGRESSING = "progressing"
STATUS_GOOD = "good"
STATUS_MASTERED = "mastered"


@dataclass(frozen=True)
class ChapterMastery:
    """One chapter's aggregated performance for a single learner."""

    chapter: Chapter
    source: SourceDocument | None
    attempts: int
    correct: int
    covered_questions: int
    total_questions: int
    status: str
    low_sample: bool

    @property
    def wrong(self) -> int:
        return self.attempts - self.correct

    @property
    def accuracy(self) -> float | None:
        if not self.attempts:
            return None
        return self.correct / self.attempts * 100


@dataclass(frozen=True)
class DayActivity:
    """One display-timezone calendar day's attempts inside the trend window."""

    day: str
    attempts: int
    correct: int

    @property
    def accuracy(self) -> float | None:
        if not self.attempts:
            return None
        return self.correct / self.attempts * 100


@dataclass(frozen=True)
class DashboardData:
    """Everything the dashboard template needs, pre-computed."""

    total_attempts: int
    correct_attempts: int
    attempts_last_7_days: int
    attempts_last_30_days: int
    pending_wrong: int
    corrected_wrong: int
    due_srs: int
    chapters: tuple[ChapterMastery, ...]
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


def count_since(attempts: list[Attempt], cutoff: datetime) -> int:
    """Count attempts answered at or after ``cutoff``."""
    return sum(
        srs.parse_timestamp(attempt.answered_at) >= cutoff
        for attempt in attempts
    )


def daily_trend(
    attempts: list[Attempt], now: datetime, display_tz: tzinfo
) -> tuple[DayActivity, ...]:
    """Return one entry per display-timezone calendar day, filling zeros."""
    today = display_day(now, display_tz)
    by_day: dict[str, list[Attempt]] = {}
    window_start = today - timedelta(days=TREND_DAYS - 1)
    for attempt in attempts:
        day = display_day(
            srs.parse_timestamp(attempt.answered_at), display_tz
        )
        if day < window_start or day > today:
            continue
        by_day.setdefault(day.isoformat(), []).append(attempt)
    trend: list[DayActivity] = []
    for offset in range(TREND_DAYS):
        day = window_start + timedelta(days=offset)
        day_attempts = by_day.get(day.isoformat(), [])
        trend.append(
            DayActivity(
                day=day.isoformat(),
                attempts=len(day_attempts),
                correct=sum(item.is_correct for item in day_attempts),
            )
        )
    return tuple(trend)


class StatisticsService:
    """Compute per-learner learning statistics from existing stores."""

    def __init__(
        self,
        attempt_repository: AttemptRepository,
        question_repository: QuestionRepository,
        wrong_question_service: WrongQuestionService,
        display_tz: tzinfo | None = None,
    ) -> None:
        self.attempt_repository = attempt_repository
        self.question_repository = question_repository
        self.wrong_question_service = wrong_question_service
        self.display_tz = display_tz or timezone.utc

    def build_dashboard(
        self, learner_id: str, *, now: datetime | None = None
    ) -> DashboardData:
        """Collect every dashboard metric for one learner at ``now``."""
        moment = now or srs.utc_now()
        attempts = self.attempt_repository.list_for_learner(learner_id)
        total = len(attempts)
        correct = sum(attempt.is_correct for attempt in attempts)
        week_cutoff = moment - timedelta(days=RECENT_WINDOW_DAYS)
        month_cutoff = moment - timedelta(days=MONTH_WINDOW_DAYS)
        mistake_stats = self.wrong_question_service.get_stats(learner_id)
        return DashboardData(
            total_attempts=total,
            correct_attempts=correct,
            attempts_last_7_days=self._count_since(attempts, week_cutoff),
            attempts_last_30_days=self._count_since(attempts, month_cutoff),
            pending_wrong=mistake_stats["pending"],
            corrected_wrong=mistake_stats["corrected"],
            due_srs=self.wrong_question_service.get_due_srs_count(
                learner_id, now=moment
            ),
            chapters=self._chapter_mastery(attempts),
            trend=self._daily_trend(attempts, moment),
            trend_window_days=TREND_DAYS,
            recent_window_days=RECENT_WINDOW_DAYS,
            month_window_days=MONTH_WINDOW_DAYS,
        )

    @staticmethod
    def _count_since(attempts: list[Attempt], cutoff: datetime) -> int:
        return count_since(attempts, cutoff)

    def _chapter_mastery(
        self, attempts: list[Attempt]
    ) -> tuple[ChapterMastery, ...]:
        """Aggregate attempts per chapter in curriculum order.

        A question belonging to several chapters counts toward each of them,
        mirroring the weak-knowledge-point rules for shared questions.
        """
        questions_by_id = {
            question.id: question
            for question in self.question_repository.get_all()
        }
        attempts_by_chapter: dict[str, list[Attempt]] = {}
        covered_by_chapter: dict[str, set[str]] = {}
        for attempt in attempts:
            question = questions_by_id.get(attempt.question_id)
            if question is None:
                continue
            for chapter_id in question.chapter_ids:
                attempts_by_chapter.setdefault(chapter_id, []).append(attempt)
                covered_by_chapter.setdefault(chapter_id, set()).add(
                    attempt.question_id
                )
        mastery: list[ChapterMastery] = []
        for source in self.question_repository.get_sources():
            for chapter in self.question_repository.get_chapters(source.id):
                chapter_attempts = attempts_by_chapter.get(chapter.id, [])
                total = len(chapter_attempts)
                correct = sum(item.is_correct for item in chapter_attempts)
                accuracy = correct / total * 100 if total else None
                mastery.append(
                    ChapterMastery(
                        chapter=chapter,
                        source=source,
                        attempts=total,
                        correct=correct,
                        covered_questions=len(
                            covered_by_chapter.get(chapter.id, set())
                        ),
                        total_questions=(
                            self.question_repository.question_count_for_chapter(
                                chapter.id
                            )
                        ),
                        status=self._classify(accuracy),
                        low_sample=0 < total < LOW_SAMPLE_ATTEMPTS,
                    )
                )
        return tuple(mastery)

    @staticmethod
    def _classify(accuracy: float | None) -> str:
        """Map a chapter accuracy onto the shared mastery bands."""
        if accuracy is None:
            return STATUS_NOT_STARTED
        if accuracy < MASTERY_WEAK_BELOW:
            return STATUS_WEAK
        if accuracy < MASTERY_PROGRESSING_BELOW:
            return STATUS_PROGRESSING
        if accuracy < MASTERY_GOOD_BELOW:
            return STATUS_GOOD
        return STATUS_MASTERED

    def _daily_trend(
        self, attempts: list[Attempt], now: datetime
    ) -> tuple[DayActivity, ...]:
        return daily_trend(attempts, now, self.display_tz)
