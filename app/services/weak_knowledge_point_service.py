"""Chapter-level weakness activation and review verification rules."""

import logging
from dataclasses import dataclass

from app.models import Chapter, Question, WeakKnowledgePoint
from app.repositories import (
    QuestionRepository,
    WeakKnowledgePointRepository,
    WrongQuestionRepository,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgePointUpdate:
    point: WeakKnowledgePoint
    newly_completed: bool = False


@dataclass(frozen=True)
class WeakKnowledgeSummary:
    point: WeakKnowledgePoint
    chapter: Chapter
    pending_wrong_count: int
    available_question_count: int
    verification_target: int

    @property
    def verification_count(self) -> int:
        return len(self.point.verified_question_ids)

    @property
    def insufficient_questions(self) -> bool:
        return self.available_question_count < self.verification_target


class WeakKnowledgePointService:
    """Keep chapter state separate from concrete wrong-question state."""

    def __init__(
        self,
        repository: WeakKnowledgePointRepository,
        question_repository: QuestionRepository,
        wrong_question_repository: WrongQuestionRepository,
        verification_target: int = 2,
    ) -> None:
        if verification_target < 1:
            raise ValueError("verification_target must be at least 1")
        self.repository = repository
        self.question_repository = question_repository
        self.wrong_question_repository = wrong_question_repository
        self.verification_target = verification_target

    def backfill_existing_wrong_questions(self) -> None:
        """Create missing weak chapters for databases predating this feature."""
        for record in self.wrong_question_repository.get_all():
            question = self.question_repository.get_by_id(record.question_id)
            if question is None:
                LOGGER.warning(
                    'Wrong question "%s" no longer exists during weak-point backfill.',
                    record.question_id,
                )
                continue
            for chapter_id in question.chapter_ids:
                self.repository.ensure_active(
                    record.learner_id, chapter_id, record.last_wrong_at
                )

    def activate_for_wrong_answer(
        self, learner_id: str, question: Question, timestamp: str
    ) -> tuple[WeakKnowledgePoint, ...]:
        points = []
        for chapter_id in question.chapter_ids:
            self.repository.activate_and_reset(
                learner_id, chapter_id, timestamp
            )
            point = self.repository.get_by_id(learner_id, chapter_id)
            if point is not None:
                points.append(point)
        return tuple(points)

    def record_review_correct(
        self, learner_id: str, question: Question, timestamp: str
    ) -> tuple[KnowledgePointUpdate, ...]:
        """Count one question ID once for every active chapter it belongs to."""
        updates = []
        for chapter_id in question.chapter_ids:
            point = self.repository.get_by_id(learner_id, chapter_id)
            if point is None:
                continue
            valid_ids = self._live_question_ids(chapter_id)
            verified = [
                question_id
                for question_id in point.verified_question_ids
                if question_id in valid_ids
            ]
            was_active = point.active or len(verified) < self.verification_target
            if not was_active:
                continue
            if question.id in valid_ids and question.id not in verified:
                verified.append(question.id)
            active = len(verified) < self.verification_target
            self.repository.save_verification(
                learner_id,
                chapter_id,
                tuple(verified),
                active,
                timestamp,
            )
            updated = self.repository.get_by_id(learner_id, chapter_id)
            if updated is not None:
                updates.append(
                    KnowledgePointUpdate(
                        point=updated,
                        newly_completed=was_active and not updated.active,
                    )
                )
        return tuple(updates)

    def get_active_points(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[WeakKnowledgePoint]:
        return [
            point
            for point in self._filtered_points(
                learner_id, chapter_ids=chapter_ids, source_ids=source_ids
            )
            if point.active
        ]

    def get_summaries(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[WeakKnowledgeSummary]:
        wrong_records = self.wrong_question_repository.get_all(learner_id)
        pending_by_chapter: dict[str, set[str]] = {}
        for record in wrong_records:
            if record.corrected:
                continue
            question = self.question_repository.get_by_id(record.question_id)
            if question is None:
                continue
            for chapter_id in question.chapter_ids:
                pending_by_chapter.setdefault(chapter_id, set()).add(question.id)

        summaries = []
        for point in self._filtered_points(
            learner_id, chapter_ids=chapter_ids, source_ids=source_ids
        ):
            chapter = self.question_repository.get_chapter(point.chapter_id)
            if chapter is None:
                continue
            sanitized = self._sanitized_point(point)
            summaries.append(
                WeakKnowledgeSummary(
                    point=sanitized,
                    chapter=chapter,
                    pending_wrong_count=len(
                        pending_by_chapter.get(point.chapter_id, set())
                    ),
                    available_question_count=len(
                        self._live_question_ids(point.chapter_id)
                    ),
                    verification_target=self.verification_target,
                )
            )
        return summaries

    def shortage_data(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
    ) -> list[dict[str, object]]:
        shortages = []
        for summary in self.get_summaries(
            learner_id, chapter_ids=chapter_ids, source_ids=source_ids
        ):
            if not summary.point.active:
                continue
            shortages.append(
                {
                    "chapter_id": summary.chapter.id,
                    "chapter_title": summary.chapter.title,
                    "verified_count": summary.verification_count,
                    "verification_target": summary.verification_target,
                    "available_question_count": summary.available_question_count,
                }
            )
        return shortages

    def reset(self, learner_id: str) -> int:
        return self.repository.delete_all_for_learner(learner_id)

    def reconcile_all(self, unusable_question_ids: set[str]) -> int:
        """Persistently strip unusable question IDs from every verification.

        Read paths already skip stale IDs in memory; this startup-time sweep
        makes the cleanup durable so deleted or grading-changed questions can
        never inflate a chapter's 0/2 → 2/2 progress forever.  Chapters that
        no longer exist in the bank are deliberately left untouched (their
        rows stay for a possible later return and are skipped when read).
        Returns the number of rows rewritten.
        """
        changed = 0
        for point in self.repository.get_all():
            if self.question_repository.get_chapter(point.chapter_id) is None:
                continue
            valid_ids = (
                self._live_question_ids(point.chapter_id) - unusable_question_ids
            )
            verified = tuple(
                question_id
                for question_id in point.verified_question_ids
                if question_id in valid_ids
            )
            active = point.active or len(verified) < self.verification_target
            if verified == point.verified_question_ids and active == point.active:
                continue
            self.repository.save_verification(
                point.learner_id,
                point.chapter_id,
                verified,
                active,
                point.updated_at,
            )
            changed += 1
        return changed

    def _filtered_points(
        self,
        learner_id: str,
        *,
        chapter_ids: set[str] | None,
        source_ids: set[str] | None,
    ) -> list[WeakKnowledgePoint]:
        result = []
        for point in self.repository.get_all(learner_id):
            chapter = self.question_repository.get_chapter(point.chapter_id)
            if chapter is None:
                LOGGER.warning(
                    'Weak chapter "%s" no longer exists in questions.json.',
                    point.chapter_id,
                )
                continue
            if chapter_ids is not None and point.chapter_id not in chapter_ids:
                continue
            if source_ids is not None and chapter.source_id not in source_ids:
                continue
            result.append(self._sanitized_point(point))
        return result

    def _sanitized_point(
        self, point: WeakKnowledgePoint
    ) -> WeakKnowledgePoint:
        valid_ids = self._live_question_ids(point.chapter_id)
        verified = tuple(
            question_id
            for question_id in point.verified_question_ids
            if question_id in valid_ids
        )
        active = point.active or len(verified) < self.verification_target
        if (
            verified == point.verified_question_ids
            and active == point.active
        ):
            return point
        return WeakKnowledgePoint(
            learner_id=point.learner_id,
            chapter_id=point.chapter_id,
            active=active,
            verified_question_ids=verified,
            last_wrong_at=point.last_wrong_at,
            updated_at=point.updated_at,
        )

    def _live_question_ids(self, chapter_id: str) -> set[str]:
        return {
            question.id
            for question in self.question_repository.get_filtered(
                chapter_ids={chapter_id}
            )
        }
