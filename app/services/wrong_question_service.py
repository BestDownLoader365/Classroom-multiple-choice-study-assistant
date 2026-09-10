"""Wrong-question correction, chapter activation, and stale-record rules."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Attempt, Question, QuizMode, WrongQuestion
from app.repositories import (
    AttemptRepository,
    QuestionRepository,
    WeakKnowledgePointRepository,
    WrongQuestionRepository,
)
from .weak_knowledge_point_service import (
    KnowledgePointUpdate,
    WeakKnowledgePointService,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MistakeItem:
    """Pair learning state with its live in-memory question."""

    record: WrongQuestion
    question: Question
    selected_answers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LearningUpdate:
    """Question and chapter changes caused by one graded answer."""

    became_wrong_question: bool = False
    corrected_now: bool = False
    knowledge_updates: tuple[KnowledgePointUpdate, ...] = ()


class WrongQuestionService:
    """Coordinate attempts with question correction and chapter weakness."""

    def __init__(
        self,
        attempt_repository: AttemptRepository,
        wrong_question_repository: WrongQuestionRepository,
        question_repository: QuestionRepository,
        weak_knowledge_point_service: WeakKnowledgePointService | None = None,
    ) -> None:
        self.attempt_repository = attempt_repository
        self.wrong_question_repository = wrong_question_repository
        self.question_repository = question_repository
        self.weak_knowledge_point_service = (
            weak_knowledge_point_service
            or WeakKnowledgePointService(
                WeakKnowledgePointRepository(
                    wrong_question_repository.database
                ),
                question_repository,
                wrong_question_repository,
            )
        )

    def record_attempt(
        self,
        learner_id: str,
        question_id: str,
        mode: QuizMode,
        selected_answers: tuple[str, ...],
        is_correct: bool,
    ) -> LearningUpdate:
        """Persist one attempt and update wrong-question state when applicable."""
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = self.wrong_question_repository.get_by_id(
            learner_id, question_id
        )
        question = self.question_repository.get_by_id(question_id)
        self.attempt_repository.add(
            Attempt(
                learner_id=learner_id,
                question_id=question_id,
                mode=mode,
                selected_answers=selected_answers,
                is_correct=is_correct,
                answered_at=timestamp,
            )
        )

        if not is_correct:
            self.wrong_question_repository.record_wrong(
                learner_id=learner_id,
                question_id=question_id,
                timestamp=timestamp,
                reviewed=mode is QuizMode.REVIEW,
            )
            points = (
                self.weak_knowledge_point_service.activate_for_wrong_answer(
                    learner_id, question, timestamp
                )
                if question is not None
                else ()
            )
            return LearningUpdate(
                became_wrong_question=previous is None,
                knowledge_updates=tuple(
                    KnowledgePointUpdate(point=point) for point in points
                ),
            )
        elif mode is QuizMode.REVIEW:
            if previous is not None:
                self.wrong_question_repository.record_corrected(
                    learner_id=learner_id,
                    question_id=question_id,
                    timestamp=timestamp,
                )
            updates = (
                self.weak_knowledge_point_service.record_review_correct(
                    learner_id, question, timestamp
                )
                if question is not None
                else ()
            )
            return LearningUpdate(
                corrected_now=previous is not None and not previous.corrected,
                knowledge_updates=updates,
            )
        return LearningUpdate()

    def get_items(
        self,
        learner_id: str,
        corrected: bool | None = None,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[MistakeItem]:
        """Return live mistakes and skip IDs removed from ``questions.json``."""
        items: list[MistakeItem] = []
        latest_answers = self.attempt_repository.get_latest_incorrect_answers(learner_id)
        for record in self.wrong_question_repository.get_all(learner_id):
            question = self.question_repository.get_by_id(record.question_id)
            if question is None:
                LOGGER.warning(
                    'Wrong question "%s" no longer exists in questions.json.',
                    record.question_id,
                )
                continue
            if chapter_ids is not None and chapter_ids.isdisjoint(question.chapter_ids):
                continue
            if source_ids is not None and question.source_id not in source_ids:
                continue
            if corrected is None or record.corrected is corrected:
                items.append(
                    MistakeItem(
                        record=record,
                        question=question,
                        selected_answers=latest_answers.get(question.id, ()),
                    )
                )
        return items

    def get_other_items(self, learner_id: str) -> list[MistakeItem]:
        """Return other learners' valid records for read-only display."""
        items: list[MistakeItem] = []
        for record in self.wrong_question_repository.get_all():
            if record.learner_id == learner_id:
                continue
            question = self.question_repository.get_by_id(record.question_id)
            if question is None:
                LOGGER.warning(
                    'Wrong question "%s" no longer exists in questions.json.',
                    record.question_id,
                )
                continue
            items.append(MistakeItem(record=record, question=question))
        return items

    def get_uncorrected_question_ids(self, learner_id: str) -> list[str]:
        """Return only valid, currently uncorrected question IDs."""
        return [
            item.question.id
            for item in self.get_items(learner_id, corrected=False)
        ]

    def get_filtered_uncorrected_question_ids(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[str]:
        """Return valid pending correction IDs restricted to curriculum filters."""
        return [
            item.question.id
            for item in self.get_items(
                learner_id,
                corrected=False,
                chapter_ids=chapter_ids,
                source_ids=source_ids,
            )
        ]

    def get_record(
        self, learner_id: str, question_id: str
    ) -> WrongQuestion | None:
        """Return current review progress for immediate learner feedback."""
        return self.wrong_question_repository.get_by_id(learner_id, question_id)

    def get_stats(self, learner_id: str) -> dict[str, int]:
        """Return pending/corrected counts, excluding stale database rows."""
        items = self.get_items(learner_id)
        return {
            "pending": sum(not item.record.corrected for item in items),
            "corrected": sum(item.record.corrected for item in items),
        }

    def reset(self, learner_id: str) -> int:
        """Clear question and chapter learning state but preserve attempts."""
        deleted = self.wrong_question_repository.delete_all_for_learner(
            learner_id
        )
        self.weak_knowledge_point_service.reset(learner_id)
        return deleted
