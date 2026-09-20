"""Per-question reconciliation between one course's bank and its learner history.

The raw-bytes fingerprint of a course's ``questions.json`` still detects *that*
the file changed, but it no longer decides what happens to learner data.  This
service diffs the freshly loaded bank against that course's permanent
``question_registry`` by stable ``question.id`` and only touches data that
truly lost its meaning:

- content-only edits (wording, translations, option order or text, new
  wrong options, section/pages, formatting) keep every learner record;
- chapter/source moves (`chapter_ids` or `source_id`) keep every learner
  record but count as a structural change: they advance the bank generation
  so sibling workers holding the old filing mapping are fenced off;
- grading-identity changes (type, correct answers, removed/renamed option
  IDs) clear exactly that question's attempts and correction/SRS state;
- questions that left the bank keep their attempts but silently lose their
  correction/SRS state, weak-point references, progress-queue entries and
  unfinished-exam slots;
- retired IDs stay reserved forever: reusing one for a grading-different
  question fails startup before anything is written.

Everything here is bound to a single ``course_id``.  A course A sync can never
scan, clear, retire or advance course B: registry bootstrap, historical-orphan
collection, tombstones, grading compatibility, targeted cleanup, weak/progress
reconciliation, unfinished-exam reconciliation, and both the placement and
catalogue baselines are all evaluated inside A's namespace, and only A's
generation row is written.  At most one generation bump happens per course per
publication, no matter how many structural changes it contains.

The whole reconciliation runs inside one ``BEGIN IMMEDIATE`` transaction, so
concurrent workers observe either the old or the new world, never a mixture,
and a second worker's diff simply finds nothing to do.  Inside that transaction
the caller can re-confirm the *publication* it preloaded, which closes the
"worker preloaded old files, then a newer publication landed" race.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.models import (
    Question,
    QuestionRegistryEntry,
    QuestionRegistryStatus,
    validate_course_id,
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
from .question_fingerprint import (
    catalogue_fingerprint,
    content_fingerprint,
    placement_fingerprint,
)
from .weak_knowledge_point_service import WeakKnowledgePointService

LOGGER = logging.getLogger(__name__)


class CoursePublicationChangedError(RuntimeError):
    """Raised when a worker's preloaded publication is no longer current.

    The caller must discard the preload, reload the course content, and retry:
    syncing the stale snapshot back into the database would resurrect content
    the operator already replaced.
    """

    def __init__(self, course_id: str) -> None:
        self.course_id = course_id
        super().__init__(
            f'Course "{course_id}" was published again while this worker was '
            "starting up; the preloaded snapshot was discarded."
        )



@dataclass(frozen=True)
class BankDiff:
    """The classified outcome of comparing one bank against the registry."""

    new_ids: tuple[str, ...] = ()
    content_changed_ids: tuple[str, ...] = ()
    grading_changed_ids: tuple[str, ...] = ()
    placement_changed_ids: tuple[str, ...] = ()
    deleted_ids: tuple[str, ...] = ()
    resurrected_ids: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_ids
            or self.content_changed_ids
            or self.grading_changed_ids
            or self.placement_changed_ids
            or self.deleted_ids
            or self.resurrected_ids
        )

    @property
    def structural(self) -> bool:
        """Whether the question set, grading identity or filing changed.

        Chapter/source moves count: they drive filtering, review selection and
        chapter progress, so sibling workers holding the old mapping must be
        fenced off even though no learner history is cleared.
        """
        return bool(
            self.new_ids
            or self.grading_changed_ids
            or self.placement_changed_ids
            or self.deleted_ids
            or self.resurrected_ids
        )

    @property
    def clears_learner_state(self) -> bool:
        """Whether applying this diff removes stored learner state."""
        return bool(self.unusable_ids)

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


def _placement_known(entry: QuestionRegistryEntry) -> bool:
    """Return whether the row recorded a chapter/source placement identity.

    Rows written before placement tracking (or the retired tombstones built
    from learner history) store nothing: the first bank version after the
    upgrade is adopted as the baseline instead of being reported as a move.
    """
    return bool(entry.placement_fingerprint)


def diff_questions(
    questions: list[Question],
    registry: dict[str, QuestionRegistryEntry],
) -> BankDiff:
    """Classify every question ID between the loaded bank and the registry."""
    new_ids: list[str] = []
    content_changed: list[str] = []
    grading_changed: list[str] = []
    placement_changed: list[str] = []
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
        elif _placement_known(entry) and entry.placement_fingerprint != (
            placement_fingerprint(question)
        ):
            # Moving a question between chapters/sources keeps its history but
            # changes what the bank means: sibling workers must be fenced off.
            placement_changed.append(question.id)
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
        placement_changed_ids=tuple(placement_changed),
        deleted_ids=tuple(sorted(deleted)),
        resurrected_ids=tuple(resurrected),
        violations=tuple(violations),
    )


def needs_placement_backfill(
    questions: list[Question],
    registry: dict[str, QuestionRegistryEntry],
) -> tuple[str, ...]:
    """Return live IDs whose registry row predates placement tracking.

    The sync adopts their current chapter/source mapping without a generation
    bump, so later moves can be detected against a real baseline.
    """
    return tuple(
        question.id
        for question in questions
        if (entry := registry.get(question.id)) is not None
        and entry.status is QuestionRegistryStatus.ACTIVE
        and not _placement_known(entry)
    )


class QuestionBankSyncService:
    """Reconcile one course's loaded question bank with its learner data."""

    def __init__(
        self,
        *,
        database: Database,
        course_id: str,
        state_repository: QuestionBankStateRepository,
        registry_repository: QuestionRegistryRepository,
        question_repository: QuestionRepository,
        attempt_repository: AttemptRepository,
        wrong_question_repository: WrongQuestionRepository,
        weak_knowledge_point_service: WeakKnowledgePointService,
        progress_repository: ProgressRepository,
        exam_service: ExamService,
    ) -> None:
        self.course_id = validate_course_id(course_id)
        self.database = database
        self.state_repository = state_repository
        self.registry_repository = registry_repository
        self.question_repository = question_repository
        self.attempt_repository = attempt_repository
        self.wrong_question_repository = wrong_question_repository
        self.weak_knowledge_point_service = weak_knowledge_point_service
        self.progress_repository = progress_repository
        self.exam_service = exam_service
        self._assert_scope()

    def _assert_scope(self) -> None:
        """Refuse to run when any dependency is bound to another course.

        A mis-wired service would otherwise reconcile one course's bank against
        another course's history — exactly the failure this refactor exists to
        prevent — so the mismatch is a loud startup error.
        """
        for name in (
            "state_repository",
            "registry_repository",
            "attempt_repository",
            "wrong_question_repository",
            "progress_repository",
        ):
            bound = getattr(getattr(self, name), "course_id", None)
            if bound != self.course_id:
                raise ValueError(
                    f'QuestionBankSyncService for "{self.course_id}" received a '
                    f'{name} bound to "{bound}".'
                )
        exam_bound = getattr(self.exam_service.exam_repository, "course_id", None)
        if exam_bound != self.course_id:
            raise ValueError(
                f'QuestionBankSyncService for "{self.course_id}" received an '
                f'exam service bound to "{exam_bound}".'
            )
        weak_bound = getattr(
            self.weak_knowledge_point_service.repository, "course_id", None
        )
        if weak_bound != self.course_id:
            raise ValueError(
                f'QuestionBankSyncService for "{self.course_id}" received a '
                f'weak-knowledge service bound to "{weak_bound}".'
            )


    def synchronize(
        self,
        bank_version: str,
        *,
        expected_publication: str | None = None,
        publication_identity: Callable[[], str] | None = None,
    ) -> int:
        """Reconcile this course's learner data once per bank change.

        The raw ``bank_version`` is recorded for diagnostics only.  The returned
        generation advances exclusively on structural changes of *this* course:
        the question set, a grading identity, a question's chapter/source
        placement, or the shape of the course catalogue.  Cosmetic edits —
        including source/chapter titles — never invalidate sibling workers and
        never touch learner data.

        When ``expected_publication`` and ``publication_identity`` are given,
        the on-disk publication is re-hashed *after* the write lock is taken.
        A mismatch means another publication landed while this worker was
        preloading, so the stale snapshot is discarded and
        :class:`CoursePublicationChangedError` is raised instead of being
        written back into the database.
        """
        now = srs.utc_now()
        questions = self.question_repository.get_all()
        catalogue = catalogue_fingerprint(
            self.question_repository.get_sources(),
            self.question_repository.get_chapters(),
        )
        with self.database.transaction():
            if expected_publication is not None and publication_identity is not None:
                if publication_identity() != expected_publication:
                    LOGGER.warning(
                        'Course "%s" was published again during startup; '
                        "discarding the preloaded question bank.",
                        self.course_id,
                    )
                    raise CoursePublicationChangedError(self.course_id)
            registry = self.registry_repository.get_all()
            state = self.state_repository.get_state()
            stored_catalogue = self.state_repository.get_catalogue_fingerprint()
            generation = state[1] if state else 0
            if not registry:
                orphans = self._bootstrap(questions, now)
                if orphans:
                    self._reconcile_learner_data(set(orphans), now)
                self.state_repository.save_state(bank_version, generation, catalogue)
                return generation

            diff = diff_questions(questions, registry)
            if diff.violations:
                raise QuestionBankError(
                    f'Course "{self.course_id}" reuses retired question IDs for '
                    f"different questions: {', '.join(diff.violations)}. Retired "
                    "IDs are reserved permanently; assign fresh IDs instead."
                )
            # A stored ``None`` means the shape predates catalogue tracking (or
            # this worker is the first after the upgrade): adopt it as the
            # baseline instead of bumping the generation for the upgrade.
            catalogue_baseline = stored_catalogue is None
            catalogue_changed = (
                stored_catalogue is not None and stored_catalogue != catalogue
            )
            backfill = needs_placement_backfill(questions, registry)
            if diff.has_changes or backfill:
                self._apply(diff, registry, questions, now, backfill_ids=backfill)
                LOGGER.info(
                    'Course "%s" bank reconciled: %d new, %d content-only, '
                    "%d grading-changed, %d placement-changed, %d deleted, "
                    "%d resurrected, %d placement-backfilled.",
                    self.course_id,
                    len(diff.new_ids),
                    len(diff.content_changed_ids),
                    len(diff.grading_changed_ids),
                    len(diff.placement_changed_ids),
                    len(diff.deleted_ids),
                    len(diff.resurrected_ids),
                    len(backfill),
                )
            if catalogue_changed:
                # Menus and filter validation are per-worker, so a changed
                # catalogue shape has to fence the sibling workers of this
                # course exactly like a question-set change, even though no
                # learner data is touched.
                LOGGER.info(
                    'Course "%s" catalogue changed: sources/chapters added, '
                    "removed, reordered or re-assigned; bumping the generation.",
                    self.course_id,
                )
            # The state row doubles as the diagnostic bank version and the
            # catalogue baseline, so it is (re)written when that baseline is
            # missing or moved — even though neither is a change to the bank.
            baseline_needs_recording = (
                catalogue_baseline or state is None or state[0] != bank_version
            )
            if (
                diff.has_changes
                or backfill
                or catalogue_changed
                or baseline_needs_recording
            ):
                if diff.structural or catalogue_changed:
                    generation += 1
                self.state_repository.save_state(bank_version, generation, catalogue)
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
        *,
        backfill_ids: tuple[str, ...] = (),
    ) -> None:
        """Persist registry transitions and reconcile affected learner data."""
        seen_at = now.isoformat()
        by_id = {question.id: question for question in questions}
        refresh_ids = (
            set(diff.content_changed_ids)
            | set(diff.grading_changed_ids)
            | set(diff.placement_changed_ids)
            | set(diff.resurrected_ids)
            | set(backfill_ids)
        )
        entries: list[QuestionRegistryEntry] = []
        for question_id in diff.new_ids:
            entries.append(self._entry_for(by_id[question_id], seen_at=seen_at))
        for question_id in sorted(refresh_ids):
            existing = registry.get(question_id)
            entries.append(
                self._entry_for(
                    by_id[question_id],
                    seen_at=seen_at,
                    first_seen_at=existing.first_seen_at if existing else None,
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
            # Non-destructive diffs (content-only edits, chapter/source moves
            # and placement backfills) can still reassign chapters; sweep the
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
            placement_fingerprint=placement_fingerprint(question),
            first_seen_at=first_seen_at or seen_at,
            last_seen_at=seen_at,
        )