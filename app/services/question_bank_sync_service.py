"""Per-question reconciliation between the loaded bank and learner history.

The raw-bytes fingerprint of ``questions.json`` still detects *that* the
file changed, but it no longer decides what happens to learner data.  This
service diffs the freshly loaded bank against the permanent
``question_registry`` by stable ``question.id`` and only touches data that
truly lost its meaning:

- content-only edits (wording, translations, option order or text, new
  wrong options, section/pages, formatting) keep every learner record;
- grading-identity changes (type, correct answers, removed/renamed option
  IDs) clear exactly that question's attempts and correction/SRS state;
- questions that left the bank keep their attempts but silently lose their
  correction/SRS state, weak-point references, progress-queue entries and
  unfinished-exam slots;
- retired IDs stay reserved forever: reusing one for a grading-different
  question fails startup before anything is written.

The whole reconciliation runs inside one ``BEGIN IMMEDIATE`` transaction,
so concurrent workers observe either the old or the new world, never a
mixture, and a second worker's diff simply finds nothing to do.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.models import (
    Question,
    QuestionRegistryEntry,
    QuestionRegistryStatus,
)
from app.repositories import (
    AttemptRepository,
    Database,
    ProgressRepository,
    QuestionBankError,
    QuestionBankStateRepository,
    QuestionRegistryRepository,
    QuestionRepository,
    WrongQuestionRepository,
)

from . import srs_service as srs
from .exam_service import ExamService
from .progress_state import is_valid_progress_state, reconcile_state
from .question_fingerprint import content_fingerprint
from .weak_knowledge_point_service import WeakKnowledgePointService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BankDiff:
    """The classified outcome of comparing one bank against the registry."""

    new_ids: tuple[str, ...] = ()
    content_changed_ids: tuple[str, ...] = ()
    grading_changed_ids: tuple[str, ...] = ()
    deleted_ids: tuple[str, ...] = ()
    resurrected_ids: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_ids
            or self.content_changed_ids
            or self.grading_changed_ids
            or self.deleted_ids
            or self.resurrected_ids
        )

    @property
    def structural(self) -> bool:
        """Whether the question set or any grading identity changed."""
        return bool(
            self.new_ids
            or self.grading_changed_ids
            or self.deleted_ids
            or self.resurrected_ids
        )

    @property
    def unusable_ids(self) -> set[str]:
        """Questions whose stored learner-facing state must be dropped."""
        return set(self.deleted_ids) | set(self.grading_changed_ids)


def _grading_compatible(
    entry: QuestionRegistryEntry, question: Question
) -> bool:
    """Return whether stored answers keep their meaning for this question.

    Adding a brand-new (wrong) option is compatible; changing the type, the
    correct-answer set, or removing/renaming an existing option ID is not.
    """
    if entry.question_type != question.question_type:
        return False
    if set(entry.correct_answers) != set(question.correct_answers):
        return False
    new_option_ids = {option.id for option in question.options}
    return not set(entry.option_ids) - new_option_ids


def _grading_identical(entry: QuestionRegistryEntry, question: Question) -> bool:
    """Return whether the grading identity matches exactly (resurrection)."""
    return (
        entry.question_type == question.question_type
        and set(entry.option_ids) == {option.id for option in question.options}
        and set(entry.correct_answers) == set(question.correct_answers)
    )


def diff_questions(
    questions: list[Question],
    registry: dict[str, QuestionRegistryEntry],
) -> BankDiff:
    """Classify every question ID between the loaded bank and the registry."""
    new_ids: list[str] = []
    content_changed: list[str] = []
    grading_changed: list[str] = []
    resurrected: list[str] = []
    violations: list[str] = []
    for question in questions:
        entry = registry.get(question.id)
        if entry is None:
            new_ids.append(question.id)
            continue
        if entry.status is QuestionRegistryStatus.RETIRED:
            # Tombstones without a recorded grading identity belong to IDs
            # only ever seen in learner history: adopt the first bank version
            # that shows up instead of rejecting it.
            if not entry.option_ids or _grading_identical(entry, question):
                resurrected.append(question.id)
            else:
                violations.append(question.id)
            continue
        if not _grading_compatible(entry, question):
            grading_changed.append(question.id)
        elif entry.content_fingerprint != content_fingerprint(question):
            content_changed.append(question.id)
    live_ids = {question.id for question in questions}
    deleted = [
        question_id
        for question_id, entry in registry.items()
        if entry.status is QuestionRegistryStatus.ACTIVE
        and question_id not in live_ids
    ]
    return BankDiff(
        new_ids=tuple(new_ids),
        content_changed_ids=tuple(content_changed),
        grading_changed_ids=tuple(grading_changed),
        deleted_ids=tuple(sorted(deleted)),
        resurrected_ids=tuple(resurrected),
        violations=tuple(violations),
    )


class QuestionBankSyncService:
    """Reconcile the loaded question bank with persistent learner data."""

    def __init__(
        self,
        *,
        database: Database,
        state_repository: QuestionBankStateRepository,
        registry_repository: QuestionRegistryRepository,
        question_repository: QuestionRepository,
        attempt_repository: AttemptRepository,
        wrong_question_repository: WrongQuestionRepository,
        weak_knowledge_point_service: WeakKnowledgePointService,
        progress_repository: ProgressRepository,
        exam_service: ExamService,
    ) -> None:
        self.database = database
        self.state_repository = state_repository
        self.registry_repository = registry_repository
        self.question_repository = question_repository
        self.attempt_repository = attempt_repository
        self.wrong_question_repository = wrong_question_repository
        self.weak_knowledge_point_service = weak_knowledge_point_service
        self.progress_repository = progress_repository
        self.exam_service = exam_service

    def synchronize(self, bank_version: str) -> int:
        """Reconcile learner data once per bank change; return the generation.

        The raw ``bank_version`` is recorded for diagnostics only.  The
        returned generation advances exclusively on structural changes, so
        cosmetic bank edits never invalidate sibling workers and never touch
        learner data.
        """
        now = srs.utc_now()
        questions = self.question_repository.get_all()
        with self.database.transaction():
            registry = self.registry_repository.get_all()
            state = self.state_repository.get_state()
            generation = state[1] if state else 0
            if not registry:
                orphans = self._bootstrap(questions, now)
                if orphans:
                    self._reconcile_learner_data(set(orphans), now)
                self.state_repository.save_state(bank_version, generation)
                return generation

            diff = diff_questions(questions, registry)
            if diff.violations:
                raise QuestionBankError(
                    "Question bank reuses retired question IDs for different "
                    f"questions: {', '.join(diff.violations)}. Retired IDs are "
                    "reserved permanently; assign fresh IDs instead."
                )
            if diff.has_changes:
                self._apply(diff, registry, questions, now)
                LOGGER.info(
                    "Question bank reconciled: %d new, %d content-only, "
                    "%d grading-changed, %d deleted, %d resurrected.",
                    len(diff.new_ids),
                    len(diff.content_changed_ids),
                    len(diff.grading_changed_ids),
                    len(diff.deleted_ids),
                    len(diff.resurrected_ids),
                )
            if diff.has_changes or state is None or state[0] != bank_version:
                if diff.structural:
                    generation += 1
                self.state_repository.save_state(bank_version, generation)
            return generation

    def _bootstrap(
        self, questions: list[Question], now: datetime
    ) -> list[str]:
        """Seed an empty registry, preserving every existing learner record.

        Databases written before the registry existed necessarily match the
        currently deployed bank (the previous byte-fingerprint regime wiped
        them on any change), so the loaded bank is adopted as the baseline
        without touching history.  Question IDs found only in learner tables
        are adopted as retired tombstones and reconciled like deletions.
        """
        seen_at = now.isoformat()
        self.registry_repository.upsert_many(
            [self._entry_for(question, seen_at=seen_at) for question in questions]
        )
        live_ids = {question.id for question in questions}
        orphans = sorted(self._collect_historical_question_ids() - live_ids)
        if orphans:
            self.registry_repository.upsert_many(
                [
                    QuestionRegistryRepository.tombstone_for(
                        question_id, retired_at=seen_at
                    )
                    for question_id in orphans
                ]
            )
            LOGGER.info(
                "Adopted %d orphaned question IDs from existing history: %s",
                len(orphans),
                ", ".join(orphans),
            )
        return orphans

    def _collect_historical_question_ids(self) -> set[str]:
        """Return every question ID referenced by any learner table."""
        ids: set[str] = set()
        ids |= self.attempt_repository.distinct_question_ids()
        ids |= self.wrong_question_repository.distinct_question_ids()
        ids |= self.exam_service.exam_repository.distinct_question_ids()
        for _, _, _, state in self.progress_repository.get_all():
            if not isinstance(state, dict):
                continue
            ids |= {
                question_id
                for question_id in state.get("question_ids", [])
                if isinstance(question_id, str)
            }
            ids |= {
                item["question_id"]
                for item in state.get("review_items", [])
                if isinstance(item, dict) and isinstance(item.get("question_id"), str)
            }
            ids |= {
                question_id
                for question_id in state.get("fairness_remaining_ids", [])
                if isinstance(question_id, str)
            }
        for point in self.weak_knowledge_point_service.repository.get_all():
            ids |= set(point.verified_question_ids)
        return ids

    def _apply(
        self,
        diff: BankDiff,
        registry: dict[str, QuestionRegistryEntry],
        questions: list[Question],
        now: datetime,
    ) -> None:
        """Persist registry transitions and reconcile affected learner data."""
        seen_at = now.isoformat()
        by_id = {question.id: question for question in questions}
        entries: list[QuestionRegistryEntry] = []
        for question_id in diff.new_ids:
            entries.append(self._entry_for(by_id[question_id], seen_at=seen_at))
        for question_id in (
            diff.content_changed_ids
            + diff.grading_changed_ids
            + diff.resurrected_ids
        ):
            entries.append(
                self._entry_for(
                    by_id[question_id],
                    seen_at=seen_at,
                    first_seen_at=registry[question_id].first_seen_at,
                )
            )
        if entries:
            self.registry_repository.upsert_many(entries)
        for question_id in diff.deleted_ids:
            self.registry_repository.retire(question_id, retired_at=seen_at)

        if diff.grading_changed_ids:
            deleted_attempts = self.attempt_repository.delete_for_questions(
                diff.grading_changed_ids
            )
            LOGGER.info(
                "Cleared %d attempts for grading-changed questions: %s",
                deleted_attempts,
                ", ".join(diff.grading_changed_ids),
            )
        if diff.unusable_ids:
            self._reconcile_learner_data(diff.unusable_ids, now)
        else:
            # Content-only diffs can still reassign chapters; sweep the
            # verification lists so stale references never linger.
            self.weak_knowledge_point_service.reconcile_all(set())

    def _reconcile_learner_data(
        self, unusable_ids: set[str], now: datetime
    ) -> None:
        """Drop every learner-facing state bound to unusable questions."""
        removed = self.wrong_question_repository.delete_for_questions(unusable_ids)
        if removed:
            LOGGER.info(
                "Silently cleared %d wrong-question/SRS rows for %d questions.",
                removed,
                len(unusable_ids),
            )
        self.weak_knowledge_point_service.reconcile_all(unusable_ids)
        self._reconcile_progress(unusable_ids)
        reconciled_exams = self.exam_service.reconcile_all_in_progress(
            unusable_ids, now=now
        )
        if reconciled_exams:
            LOGGER.info(
                "Reconciled %d unfinished exams against the changed bank.",
                reconciled_exams,
            )

    def _reconcile_progress(self, unusable_ids: set[str]) -> None:
        """Rewrite every unfinished round without its unusable questions."""
        usable = (
            lambda question_id: question_id not in unusable_ids
            and self.question_repository.has(question_id)
        )
        live_chapter = (
            lambda chapter_id: self.question_repository.get_chapter(chapter_id)
            is not None
        )
        for learner_id, mode, row_version, state in (
            self.progress_repository.get_all()
        ):
            if state is None or not is_valid_progress_state(state, mode):
                continue

            def lookup_correct(question_id: str) -> bool | None:
                attempt = self.attempt_repository.get_latest_attempt_for(
                    learner_id, question_id, mode
                )
                return attempt.is_correct if attempt is not None else None

            new_state, changed = reconcile_state(
                state,
                mode,
                is_usable=usable,
                lookup_correct=lookup_correct,
                is_live_chapter=live_chapter,
            )
            if changed:
                self.progress_repository.save(
                    learner_id, mode, row_version, new_state
                )

    @staticmethod
    def _entry_for(
        question: Question,
        *,
        seen_at: str,
        first_seen_at: str | None = None,
    ) -> QuestionRegistryEntry:
        """Build the active registry row for one loaded question."""
        return QuestionRegistryEntry(
            question_id=question.id,
            status=QuestionRegistryStatus.ACTIVE,
            question_type=question.question_type,
            option_ids=tuple(sorted(option.id for option in question.options)),
            correct_answers=tuple(sorted(question.correct_answers)),
            content_fingerprint=content_fingerprint(question),
            first_seen_at=first_seen_at or seen_at,
            last_seen_at=seen_at,
        )