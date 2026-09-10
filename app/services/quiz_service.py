"""Shared quiz orchestration for normal and review modes."""

import hashlib
import json
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass

from app.models import Option, Question, QuizMode
from app.repositories import QuestionRepository

from .grading_service import GradingService
from .weak_knowledge_point_service import WeakKnowledgePointService
from .wrong_question_service import LearningUpdate, WrongQuestionService

LOGGER = logging.getLogger(__name__)
ORIGINAL_CORRECTION = "original_correction"
TRANSFER_VERIFICATION = "transfer_verification"


@dataclass(frozen=True)
class NormalSelectionResult:
    question_ids: tuple[str, ...]
    fairness_scope: str
    fairness_remaining_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReviewItem:
    question_id: str
    role: str
    chapter_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "question_id": self.question_id,
            "role": self.role,
            "chapter_id": self.chapter_id,
        }


@dataclass(frozen=True)
class ReviewSelectionResult:
    items: tuple[ReviewItem, ...] = ()
    shortages: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class AnswerResult:
    """The server-side result rendered after an answer is submitted."""

    question: Question
    selected_answers: tuple[str, ...]
    is_correct: bool
    learning_update: LearningUpdate


class QuizService:
    """Build randomized quizzes and grade answers in either supported mode."""

    def __init__(
        self,
        question_repository: QuestionRepository,
        grading_service: GradingService,
        wrong_question_service: WrongQuestionService,
        weak_knowledge_point_service: WeakKnowledgePointService | None = None,
        shuffler: Callable[[list[str]], None] = random.shuffle,
    ) -> None:
        self.question_repository = question_repository
        self.grading_service = grading_service
        self.wrong_question_service = wrong_question_service
        self.weak_knowledge_point_service = (
            weak_knowledge_point_service
            or wrong_question_service.weak_knowledge_point_service
        )
        self.shuffler = shuffler

    def start_normal(
        self,
        limit: int | None = None,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
        fairness_scope: object = None,
        fairness_remaining_ids: object = None,
    ) -> NormalSelectionResult:
        """Select a random normal round while consuming a scope-bound bag."""
        eligible_ids = [
            question.id
            for question in self.question_repository.get_filtered(
                chapter_ids=chapter_ids, source_ids=source_ids
            )
        ]
        scope = _scope_signature(eligible_ids)
        if limit is None:
            selected = list(eligible_ids)
            self.shuffler(selected)
            return NormalSelectionResult(tuple(selected), scope, ())

        requested_count = min(limit, len(eligible_ids))
        eligible_set = set(eligible_ids)
        remaining = _validated_remaining_ids(
            fairness_remaining_ids
            if fairness_scope == scope
            else None,
            eligible_set,
        )
        if not remaining and requested_count:
            remaining = list(eligible_ids)
            self.shuffler(remaining)

        selected: list[str] = []
        while len(selected) < requested_count:
            while remaining and len(selected) < requested_count:
                question_id = remaining.pop(0)
                if question_id not in selected:
                    selected.append(question_id)
            if len(selected) >= requested_count:
                break

            next_cycle = list(eligible_ids)
            self.shuffler(next_cycle)
            skipped: list[str] = []
            while next_cycle and len(selected) < requested_count:
                question_id = next_cycle.pop(0)
                if question_id in selected:
                    skipped.append(question_id)
                else:
                    selected.append(question_id)
            remaining = skipped + next_cycle

        return NormalSelectionResult(tuple(selected), scope, tuple(remaining))

    def start_review(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> ReviewSelectionResult:
        """Start with all pending original corrections before transfer work."""
        pending = self.wrong_question_service.get_filtered_uncorrected_question_ids(
            learner_id,
            chapter_ids=chapter_ids,
            source_ids=source_ids,
        )
        self.shuffler(pending)
        if pending:
            return ReviewSelectionResult(
                items=tuple(
                    ReviewItem(question_id, ORIGINAL_CORRECTION)
                    for question_id in pending
                )
            )
        return self.next_review_item(
            learner_id,
            chapter_ids=chapter_ids,
            source_ids=source_ids,
        )

    def next_review_item(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
        avoid_question_ids: set[str] | None = None,
    ) -> ReviewSelectionResult:
        """Choose one pending correction or same-chapter transfer question."""
        avoid = avoid_question_ids or set()
        pending = self.wrong_question_service.get_filtered_uncorrected_question_ids(
            learner_id,
            chapter_ids=chapter_ids,
            source_ids=source_ids,
        )
        available_pending = [
            question_id for question_id in pending if question_id not in avoid
        ]
        self.shuffler(available_pending)
        if available_pending:
            return ReviewSelectionResult(
                items=(
                    ReviewItem(available_pending[0], ORIGINAL_CORRECTION),
                )
            )

        points = self.weak_knowledge_point_service.get_active_points(
            learner_id,
            chapter_ids=chapter_ids,
            source_ids=source_ids,
        )
        chapter_order = [point.chapter_id for point in points]
        self.shuffler(chapter_order)
        point_by_chapter = {point.chapter_id: point for point in points}
        for chapter_id in chapter_order:
            point = point_by_chapter[chapter_id]
            verified = set(point.verified_question_ids)
            candidates = [
                question.id
                for question in self.question_repository.get_filtered(
                    chapter_ids={chapter_id}
                )
                if question.id not in verified and question.id not in avoid
            ]
            self.shuffler(candidates)
            if candidates:
                return ReviewSelectionResult(
                    items=(
                        ReviewItem(
                            candidates[0],
                            TRANSFER_VERIFICATION,
                            chapter_id,
                        ),
                    )
                )

        # If no spacer exists, correcting the sole repeatedly-wrong original is
        # preferable to ending with an uncorrected question.
        if pending:
            self.shuffler(pending)
            return ReviewSelectionResult(
                items=(ReviewItem(pending[0], ORIGINAL_CORRECTION),)
            )

        shortages = self.weak_knowledge_point_service.shortage_data(
            learner_id,
            chapter_ids=chapter_ids,
            source_ids=source_ids,
        )
        for shortage in shortages:
            LOGGER.warning(
                'Chapter "%s" has only %s distinct questions for %s required verifications.',
                shortage["chapter_id"],
                shortage["available_question_count"],
                shortage["verification_target"],
            )
        return ReviewSelectionResult(shortages=tuple(shortages))

    def start(
        self,
        mode: QuizMode,
        learner_id: str,
        limit: int | None = None,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[str]:
        """Compatibility wrapper; routes use the explicit policy methods."""
        if mode is QuizMode.NORMAL:
            return list(
                self.start_normal(
                    limit,
                    chapter_ids=chapter_ids,
                    source_ids=source_ids,
                ).question_ids
            )
        return [
            item.question_id
            for item in self.start_review(
                learner_id,
                chapter_ids=chapter_ids,
                source_ids=source_ids,
            ).items
        ]

    def order_options(
        self, question: Question, seed: str, occurrence: int = 0
    ) -> list[Option]:
        """Return a stable shuffle for one occurrence of a question."""
        options = list(question.options)
        random.Random(f"{seed}:{question.id}:{occurrence}").shuffle(options)
        return options

    def answer(
        self,
        learner_id: str,
        question_id: str,
        mode: QuizMode,
        selected_answers: list[str],
    ) -> AnswerResult:
        """Grade and persist exactly one answer submitted by the route layer."""
        question = self.question_repository.get_by_id(question_id)
        if question is None:
            raise LookupError(f'Question "{question_id}" does not exist.')
        normalized = self.grading_service.normalize(question, selected_answers)
        is_correct = self.grading_service.grade(question, list(normalized))
        learning_update = self.wrong_question_service.record_attempt(
            learner_id=learner_id,
            question_id=question_id,
            mode=mode,
            selected_answers=normalized,
            is_correct=is_correct,
        )
        return AnswerResult(
            question=question,
            selected_answers=normalized,
            is_correct=is_correct,
            learning_update=learning_update,
        )


def _scope_signature(question_ids: list[str]) -> str:
    payload = json.dumps(sorted(question_ids), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_remaining_ids(
    raw_ids: object, eligible_ids: set[str]
) -> list[str]:
    if not isinstance(raw_ids, list):
        return []
    result = []
    seen = set()
    for question_id in raw_ids:
        if (
            isinstance(question_id, str)
            and question_id in eligible_ids
            and question_id not in seen
        ):
            result.append(question_id)
            seen.add(question_id)
    return result
