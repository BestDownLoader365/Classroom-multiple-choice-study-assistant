"""Mock-exam lifecycle: creation, answer keeping, timing, and grading.

The exam has its own persisted state (``exam_sessions`` / ``exam_questions``)
and never touches the normal/review progress cycle. The server-side clock is
the single authority for time limits; every entry point accepts an injectable
``now`` so tests do not depend on real waiting. Graded final answers are
forwarded to :class:`WrongQuestionService` exactly once per question at
submission, so exam mistakes flow into the existing correction and SRS
machinery through the same code path as the other modes.
"""

import logging
import random
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import (
    Chapter,
    ExamQuestion,
    ExamSession,
    ExamStatus,
    Question,
    QuizMode,
    SourceDocument,
)
from app.repositories import ExamRepository, QuestionRepository

from . import srs_service as srs
from .grading_service import GradingService
from .wrong_question_service import WrongQuestionService

LOGGER = logging.getLogger(__name__)

# Configurable exam shapes. Question counts are validated against the live
# bank size; time limits are stored in seconds (``None`` means untimed).
EXAM_QUESTION_COUNT_OPTIONS: tuple[int, ...] = (10, 20, 30, 50)
EXAM_TIME_LIMIT_OPTIONS: tuple[int | None, ...] = (
    600, 1200, 1800, 2400, 3000, 3600, 4200, 4800, 5400, None,
)
EXAM_TIME_LIMIT_LABELS: dict[int | None, str] = {
    600: "10 分钟",
    1200: "20 分钟",
    1800: "30 分钟",
    2400: "40 分钟",
    3000: "50 分钟",
    3600: "60 分钟",
    4200: "70 分钟",
    4800: "80 分钟",
    5400: "90 分钟",
    None: "不限时",
}
EXAM_HISTORY_LIMIT = 20


class ExamConfigError(ValueError):
    """Raised when an exam configuration is not allowed."""


class ExamNotFoundError(LookupError):
    """Raised when the exam does not exist or belongs to another learner."""


class ExamStateError(RuntimeError):
    """Raised when an action conflicts with the exam's current state."""


class ExamExpiredError(ExamStateError):
    """Raised when an action arrives after the exam's deadline."""


@dataclass(frozen=True)
class ExamQuestionReport:
    """One graded exam question paired with its live bank content."""

    position: int
    question: Question
    selected_answers: tuple[str, ...]
    is_correct: bool
    answered: bool


@dataclass(frozen=True)
class ExamChapterReport:
    """Chapter-level score breakdown for one exam."""

    chapter: Chapter
    source: SourceDocument | None
    total: int
    correct: int

    @property
    def wrong(self) -> int:
        return self.total - self.correct

    @property
    def accuracy(self) -> float | None:
        if not self.total:
            return None
        return self.correct / self.total * 100


@dataclass(frozen=True)
class ExamReport:
    """The finalized result of one exam, ready for rendering."""

    session: ExamSession
    items: tuple[ExamQuestionReport, ...]
    chapters: tuple[ExamChapterReport, ...]
    correct_count: int
    unanswered_count: int
    elapsed_seconds: int

    @property
    def question_count(self) -> int:
        return len(self.items)

    @property
    def wrong_count(self) -> int:
        return self.question_count - self.correct_count

    @property
    def accuracy(self) -> float | None:
        if not self.question_count:
            return None
        return self.correct_count / self.question_count * 100

    @property
    def wrong_items(self) -> tuple[ExamQuestionReport, ...]:
        return tuple(item for item in self.items if not item.is_correct)


class ExamService:
    """Coordinate exam persistence, timing, grading, and mistake sync."""

    def __init__(
        self,
        exam_repository: ExamRepository,
        question_repository: QuestionRepository,
        grading_service: GradingService,
        wrong_question_service: WrongQuestionService,
        sampler: Callable[[list[str], int], list[str]] = random.sample,
    ) -> None:
        self.exam_repository = exam_repository
        self.question_repository = question_repository
        self.grading_service = grading_service
        self.wrong_question_service = wrong_question_service
        self.sampler = sampler

    @staticmethod
    def question_count_options(available: int) -> tuple[int, ...]:
        """Return the allowed exam sizes that the bank can actually serve."""
        return tuple(
            count for count in EXAM_QUESTION_COUNT_OPTIONS if count <= available
        )

    @staticmethod
    def time_limit_options() -> tuple[tuple[int | None, str], ...]:
        """Return ``(seconds, label)`` pairs for the setup form."""
        return tuple(
            (seconds, EXAM_TIME_LIMIT_LABELS[seconds])
            for seconds in EXAM_TIME_LIMIT_OPTIONS
        )

    def create_exam(
        self,
        learner_id: str,
        *,
        question_count: int,
        time_limit_seconds: int | None,
        now: datetime | None = None,
    ) -> ExamSession:
        """Fix one random, deduplicated question set for a new exam."""
        moment = now or srs.utc_now()
        available_ids = [
            question.id for question in self.question_repository.get_all()
        ]
        if question_count not in EXAM_QUESTION_COUNT_OPTIONS:
            raise ExamConfigError("不支持的题目数量。")
        if question_count > len(available_ids):
            raise ExamConfigError("题库题目不足，无法创建该规模的考试。")
        if time_limit_seconds not in EXAM_TIME_LIMIT_OPTIONS:
            raise ExamConfigError("不支持的时间限制。")
        started_at = moment.isoformat()
        deadline_at = (
            (moment + timedelta(seconds=time_limit_seconds)).isoformat()
            if time_limit_seconds is not None
            else None
        )
        question_ids = tuple(self.sampler(list(available_ids), question_count))
        session = ExamSession(
            id=uuid.uuid4().hex,
            learner_id=learner_id,
            status=ExamStatus.IN_PROGRESS,
            question_count=question_count,
            time_limit_seconds=time_limit_seconds,
            option_seed=secrets.token_hex(16),
            created_at=started_at,
            started_at=started_at,
            deadline_at=deadline_at,
        )
        self.exam_repository.create(session, question_ids)
        return session

    def get_session(
        self, learner_id: str, exam_id: str
    ) -> ExamSession:
        """Return one exam owned by the learner, or raise ``ExamNotFoundError``."""
        session = self.exam_repository.get_for_learner(learner_id, exam_id)
        if session is None:
            raise ExamNotFoundError(exam_id)
        return session

    def get_active_session(
        self, learner_id: str, *, now: datetime | None = None
    ) -> ExamSession | None:
        """Return the learner's most recent unfinished, unexpired exam."""
        moment = now or srs.utc_now()
        return self.exam_repository.get_active_for_learner(
            learner_id, moment.isoformat()
        )

    def finalize_expired_for_learner(
        self, learner_id: str, *, now: datetime | None = None
    ) -> int:
        """Auto-submit every expired in-progress exam of one learner.

        Each settlement goes through the regular idempotent ``submit()``,
        so repeated sweeps are harmless; the return value counts the exams
        settled by this call and is used by tests and flash-free callers.
        """
        moment = now or srs.utc_now()
        settled = 0
        for session in self.exam_repository.list_expired_in_progress(
            learner_id, moment.isoformat()
        ):
            self.submit(learner_id, session.id, now=moment)
            settled += 1
        return settled

    def list_sessions(self, learner_id: str) -> list[ExamSession]:
        """Return the learner's exam history, newest first."""
        return self.exam_repository.list_for_learner(
            learner_id, limit=EXAM_HISTORY_LIMIT
        )

    def get_questions(self, session: ExamSession) -> list[ExamQuestion]:
        """Return the exam's fixed question slots in exam order."""
        return self.exam_repository.get_questions(session.id)

    @staticmethod
    def is_expired(session: ExamSession, now: datetime) -> bool:
        """Return whether the server-side deadline has been reached."""
        if session.deadline_at is None or session.status.finished:
            return False
        return srs.parse_timestamp(session.deadline_at) <= now

    @staticmethod
    def remaining_seconds(session: ExamSession, now: datetime) -> int | None:
        """Return the authoritative remaining time for the countdown."""
        if session.deadline_at is None or session.status.finished:
            return None
        remaining = (srs.parse_timestamp(session.deadline_at) - now).total_seconds()
        return max(0, int(remaining))

    def finalize_if_expired(
        self, session: ExamSession, now: datetime | None = None
    ) -> bool:
        """Auto-submit an exam whose deadline passed; return whether it did."""
        moment = now or srs.utc_now()
        if not self.is_expired(session, moment):
            return False
        self.submit(session.learner_id, session.id, now=moment)
        return True

    def save_answer(
        self,
        learner_id: str,
        exam_id: str,
        position: int,
        selected_answers: list[str],
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist the current selection of one slot of an unfinished exam.

        The selection is stored verbatim (an empty selection clears the
        slot); grading happens only at submission, so learners can revisit
        and change answers freely without creating attempt history.
        """
        moment = now or srs.utc_now()
        session = self.get_session(learner_id, exam_id)
        if self.is_expired(session, moment):
            self.submit(learner_id, exam_id, now=moment)
            raise ExamExpiredError("考试时间已结束，系统已自动交卷。")
        if session.status is not ExamStatus.IN_PROGRESS:
            raise ExamStateError("本场考试已经交卷，不能继续修改答案。")
        if not 0 <= position < session.question_count:
            raise ExamConfigError("题目序号超出本场考试范围。")
        slot = self.exam_repository.get_questions(exam_id)[position]
        question = self.question_repository.get_by_id(slot.question_id)
        if question is None:
            raise ExamNotFoundError(slot.question_id)
        normalized = self.grading_service.normalize(question, selected_answers)
        self.exam_repository.save_answer(
            exam_id, position, normalized, moment.isoformat()
        )

    def submit(
        self,
        learner_id: str,
        exam_id: str,
        *,
        now: datetime | None = None,
    ) -> ExamSession:
        """Grade and finalize an exam exactly once, then sync learning state.

        Re-submitting a finished exam is a no-op that returns the stored
        result, so repeated clicks or refreshes never duplicate attempts or
        wrong-question records.
        """
        moment = now or srs.utc_now()
        session = self.get_session(learner_id, exam_id)
        if session.status.finished:
            return session
        final_status = (
            ExamStatus.EXPIRED
            if self.is_expired(session, moment)
            else ExamStatus.SUBMITTED
        )
        slots = self.exam_repository.get_questions(exam_id)
        graded: list[tuple[ExamQuestion, Question, bool]] = []
        for slot in slots:
            question = self.question_repository.get_by_id(slot.question_id)
            if question is None:
                LOGGER.warning(
                    'Exam question "%s" no longer exists during grading.',
                    slot.question_id,
                )
                continue
            is_correct = bool(slot.selected_answers) and self.grading_service.grade(
                question, list(slot.selected_answers)
            )
            graded.append((slot, question, is_correct))
        correct_count = sum(1 for _, _, is_correct in graded if is_correct)
        elapsed = int(
            (moment - srs.parse_timestamp(session.started_at)).total_seconds()
        )
        if session.time_limit_seconds is not None:
            elapsed = min(elapsed, session.time_limit_seconds)
        if not self.exam_repository.finalize(
            exam_id,
            status=final_status,
            submitted_at=moment.isoformat(),
            correct_count=correct_count,
            duration_seconds=max(0, elapsed),
        ):
            # A concurrent request finalized first; its results win.
            return self.get_session(learner_id, exam_id)
        self.exam_repository.apply_grading(
            exam_id,
            [(slot.position, is_correct) for slot, _, is_correct in graded],
        )
        for slot, question, is_correct in graded:
            if not slot.selected_answers:
                # Unanswered slots count as wrong in the score but are not
                # real answer attempts, so they skip attempt history and
                # the wrong-question flow.
                continue
            self.wrong_question_service.record_attempt(
                learner_id=learner_id,
                question_id=question.id,
                mode=QuizMode.MOCK_EXAM,
                selected_answers=slot.selected_answers,
                is_correct=is_correct,
                now=moment,
            )
        return self.get_session(learner_id, exam_id)

    def get_report(self, learner_id: str, exam_id: str) -> ExamReport:
        """Build the immutable score report of a finished exam."""
        session = self.get_session(learner_id, exam_id)
        if not session.status.finished:
            raise ExamStateError("本场考试尚未交卷，还没有成绩报告。")
        slots = self.exam_repository.get_questions(exam_id)
        items: list[ExamQuestionReport] = []
        for slot in slots:
            question = self.question_repository.get_by_id(slot.question_id)
            if question is None:
                LOGGER.warning(
                    'Exam question "%s" no longer exists in questions.json.',
                    slot.question_id,
                )
                continue
            items.append(
                ExamQuestionReport(
                    position=slot.position,
                    question=question,
                    selected_answers=slot.selected_answers,
                    is_correct=bool(slot.is_correct),
                    answered=bool(slot.selected_answers),
                )
            )
        chapters = self._chapter_breakdown(items)
        correct_count = (
            session.correct_count
            if session.correct_count is not None
            else sum(item.is_correct for item in items)
        )
        return ExamReport(
            session=session,
            items=tuple(items),
            chapters=chapters,
            correct_count=correct_count,
            unanswered_count=sum(not item.answered for item in items),
            elapsed_seconds=int(session.duration_seconds or 0),
        )

    def _chapter_breakdown(
        self, items: list[ExamQuestionReport]
    ) -> tuple[ExamChapterReport, ...]:
        """Aggregate exam items per chapter in curriculum order.

        A question belonging to several chapters counts toward each of them,
        mirroring how weak-knowledge-point tracking treats shared questions.
        """
        totals: dict[str, int] = {}
        corrects: dict[str, int] = {}
        for item in items:
            for chapter_id in item.question.chapter_ids:
                totals[chapter_id] = totals.get(chapter_id, 0) + 1
                if item.is_correct:
                    corrects[chapter_id] = corrects.get(chapter_id, 0) + 1
        breakdown: list[ExamChapterReport] = []
        for source in self.question_repository.get_sources():
            for chapter in self.question_repository.get_chapters(source.id):
                if chapter.id not in totals:
                    continue
                breakdown.append(
                    ExamChapterReport(
                        chapter=chapter,
                        source=source,
                        total=totals[chapter.id],
                        correct=corrects.get(chapter.id, 0),
                    )
                )
        return tuple(breakdown)
